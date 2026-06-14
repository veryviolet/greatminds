"""greatminds daemon — per-project coordination daemon supervision.

Wraps ``systemctl --user`` to manage instances of the
``greatminds-daemon@<project>.service`` template unit. Each greatminds
project on the host gets ONE daemon instance keyed by its
``coord.yaml: session`` name (or an explicit ``--project NAME`` flag),
so multiple projects on the same user can run their daemons
concurrently without colliding on a global unit name.

Subcommands::

    greatminds daemon install [--name NAME] [--project-dir DIR]
    greatminds daemon start    [--project NAME] [--project-dir DIR]
    greatminds daemon stop     [--project NAME] [--project-dir DIR]
    greatminds daemon restart  [--project NAME] [--project-dir DIR]
    greatminds daemon status   [--project NAME] [--project-dir DIR]
    greatminds daemon list
    greatminds daemon migrate  [--yes]

``install`` is idempotent: it writes the template unit if missing and
adds the ``{name → project_dir}`` entry to the per-user registry at
``~/.config/greatminds/projects.json``. ``migrate`` removes the deprecated
singleton ``coordd.service`` if present.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


REGISTRY_DIR = Path.home() / ".config" / "greatminds"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"
AGENT_ENV_DIR = REGISTRY_DIR / "agent-env"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TEMPLATE_UNIT_NAME = "greatminds-daemon@.service"
LEGACY_UNIT_NAME = "coordd.service"
# 0320 (0311 Phase 3a): one codex app-server per fleet, hosting all the
# fleet's driven codex worker threads. Installed + enabled only when the
# project's coord.yaml has codex roles with schema lifecycle == driven.
APPSERVER_TEMPLATE_UNIT_NAME = "greatminds-appserver@.service"

AGENT_ENV_NAMES = {
    # Claude direct / proxy / model-router auth.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    # Claude Code alternate providers.
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_HOST_AUTH_REFRESH_TIMEOUT_MS",
    "CLAUDE_BRIDGE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
}


def _resolved_greatminds_exec() -> str:
    """Pick the ExecStart command for the systemd template unit.

    Mirrors `setup.py:_greatminds_bin()` (task 0002): resolves the
    currently-running greatminds binary via shutil.which, normalises to
    absolute path. Falls back to ``<sys.executable> -m greatminds.cli.main``
    when no console script is on PATH (e.g. a bare-pip module install).
    Per-project venv installs (uv add greatminds) put the binary at
    ``<project>/.venv/bin/greatminds`` — picking it up here makes
    `daemon install` work without requiring a global ~/.local/bin/greatminds.
    """
    found = shutil.which("greatminds")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m greatminds.cli.main"


def _resolve_tool_exec(tool: str) -> str | None:
    """Resolve a user-installed tool from non-login and login-shell paths."""
    found = shutil.which(tool)
    if found:
        return str(Path(found))
    try:
        cp = subprocess.run(
            ["bash", "-lc", f"command -v {shlex.quote(tool)} 2>/dev/null"],
            capture_output=True, text=True, timeout=10,
        )
        for line in reversed((cp.stdout or "").splitlines()):
            cand = line.strip()
            if cand and Path(cand).exists():
                return cand
    except Exception:  # noqa: BLE001
        pass
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / tool,
        home / ".npm-global" / "bin" / tool,
        Path("/usr/local/bin") / tool,
        Path("/usr/bin") / tool,
    ]
    nvm = home / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        candidates.extend(sorted(nvm.glob(f"*/bin/{tool}"), reverse=True))
    for cand in candidates:
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _resolved_codex_exec() -> str | None:
    """0320: resolve the codex binary for the app-server unit's ExecStart.

    Mirrors ``_resolved_greatminds_exec`` but for codex: an nvm /
    npm-global install puts codex at e.g.
    ``~/.nvm/versions/node/<v>/bin/codex`` which is NOT on systemd's
    minimal PATH. We resolve the absolute path via ``shutil.which`` so
    the unit's ExecStart works regardless of PATH. Returns None when
    codex is not installed — the caller then skips the app-server unit
    (a fleet with no codex binary cannot host codex driven roles).
    """
    found = _resolve_tool_exec("codex")
    return str(Path(found).resolve()) if found else None


def _resolved_node_exec() -> str | None:
    """0320-iter2: resolve the absolute ``node`` interpreter for the
    app-server unit's ExecStart.

    codex's shebang is the RELATIVE ``#!/usr/bin/env node``; systemd
    --user runs with a minimal PATH that lacks the nvm node bin dir, so
    ``ExecStart=<codex.js> …`` fails with ``env: node: No such file or
    directory`` (status=127, restart loop — the GATE failure). We name
    node explicitly (``<node> <codex.js> …``) so the env-node shebang is
    bypassed. Returns None when node is not on the install-time PATH —
    the caller then skips the app-server unit."""
    found = _resolve_tool_exec("node")
    return str(Path(found).resolve()) if found else None


def appserver_socket_path(project: str) -> Path:
    """0320: the per-fleet codex app-server UNIX socket path.

    Single source of truth for both the systemd unit (which uses the
    ``%t/greatminds-appserver-%i.sock`` literal, %t = $XDG_RUNTIME_DIR)
    and the Phase-3b coordd driver (0321), which connects here to issue
    ``thread/start`` / ``turn/start``. Resolves %t to $XDG_RUNTIME_DIR,
    falling back to ``/run/user/<uid>`` then ``/tmp`` so the convention
    is stable across both sides.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        try:
            runtime = f"/run/user/{os.getuid()}"
        except AttributeError:  # pragma: no cover — non-POSIX
            runtime = "/tmp"
    return Path(runtime) / f"greatminds-appserver-{project}.sock"


# ---------------------------------------------------------------------------
# Registry I/O (per-user, NOT per-project — one user may run several projects)
# ---------------------------------------------------------------------------


def load_registry() -> dict[str, str]:
    if not REGISTRY_PATH.is_file():
        return {}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str)}


def save_registry(reg: dict[str, str]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(reg, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def register_project(name: str, project_dir: Path) -> None:
    """Insert/overwrite ``{name → str(project_dir.resolve())}``."""
    reg = load_registry()
    reg[name] = str(project_dir.resolve())
    save_registry(reg)


def lookup_project_dir(name: str) -> Path | None:
    """Reverse-resolve registry: name → project_dir."""
    v = load_registry().get(name)
    return Path(v) if isinstance(v, str) and v else None


# ---------------------------------------------------------------------------
# coord.yaml session-name resolution
# ---------------------------------------------------------------------------


def _read_session_from_coord_yaml(project_dir: Path) -> str | None:
    for p in (project_dir / "coord.yaml",
              project_dir / "coordination" / "coord.yaml"):
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            v = data.get("session")
            if isinstance(v, str) and v:
                return v
    return None


def _resolve_project_name(project: str | None,
                          project_dir: Path | None) -> str:
    if project:
        return project
    pd = (project_dir or Path.cwd()).resolve()
    name = _read_session_from_coord_yaml(pd)
    if name:
        return name
    err("--project not given and coord.yaml has no `session:` key")
    raise click.exceptions.Exit(2)


# ---------------------------------------------------------------------------
# Template unit + legacy detection
# ---------------------------------------------------------------------------


def _clean_daemon_path(exec_cmd: str) -> str:
    """A MINIMAL, deliberate PATH for the daemon unit — NOT the operator's
    raw shell PATH (which drags in cuda / flutter / plugin bins / another
    project's ``.venv-coord``). The daemon needs exactly: the project's
    own venv bin (greatminds + its ansible), the dirs of the resolved
    agent tools (node / claude / codex — typically nvm + ``~/.local/bin``),
    and the standard system dirs. Resolved once, at install time."""
    import shutil

    dirs: list[str] = []
    # 1. the project's OWN venv bin (from ExecStart) — its ansible-playbook
    #    and greatminds, ahead of everything else.
    try:
        first = exec_cmd.split()[0]
        dirs.append(str(Path(first).resolve().parent))
    except Exception:  # noqa: BLE001
        pass
    # 2. dirs of the agent tools, resolved via which / the login shell.
    #    NOT ansible — the project venv above provides it; resolving it
    #    here risks pulling in a cross-project venv (the .venv-coord bug).
    for tool in ("node", "claude", "codex"):
        p = shutil.which(tool)
        if not p:
            try:
                cp = subprocess.run(
                    ["bash", "-lc", f"command -v {tool} 2>/dev/null"],
                    capture_output=True, text=True, timeout=10)
                for line in reversed((cp.stdout or "").splitlines()):
                    cand = line.strip()
                    if cand and Path(cand).exists():
                        p = cand
                        break
            except Exception:  # noqa: BLE001
                p = None
        if p and Path(p).exists():
            # the dir where the command was FOUND (a ~/.local/bin symlink,
            # an nvm bin) — NOT the symlink's resolved target dir, which
            # may be a versions/ parent with no invokable entry.
            dirs.append(str(Path(p).parent))
    # 3. standard system dirs.
    dirs += ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
             "/usr/bin", "/sbin", "/bin"]
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return ":".join(out)


def _template_unit_body() -> str:
    """Compose the systemd template unit with the resolved ExecStart path.

    The shipped canon copy under ``src/greatminds/data/systemd/`` uses a
    placeholder ``__GREATMINDS_BIN__`` that we substitute at install time
    with the actual greatminds binary path (per
    ``_resolved_greatminds_exec``). This avoids the 203/EXEC failure
    reported by 0030 from EXPLORER's avatar dogfood: a uv-style
    per-project venv install put the binary at
    ``<project>/.venv/bin/greatminds``, NOT at the canon-template's
    ``%h/.local/bin/greatminds``.

    Falls back to an inline body when the canon file is missing.
    """
    exec_cmd = _resolved_greatminds_exec()
    body = None
    try:
        src = find_canon_dir() / "systemd" / TEMPLATE_UNIT_NAME
        if src.is_file():
            body = src.read_text(encoding="utf-8").replace(
                "__GREATMINDS_BIN__", exec_cmd)
    except Exception:  # noqa: BLE001
        body = None
    if body is None:
        body = (
            "[Unit]\n"
            "Description=greatminds coordination daemon for project %i\n"
            "After=default.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_cmd} coordd --project %i\n"
            # 0346: always (not on-failure) — coordd exits 0 on SIGTERM, so
            # on-failure left a killed coordd dead. always resurrects it
            # after an external kill/crash; a commanded `systemctl stop` is
            # still honoured.
            "Restart=always\n"
            "RestartSec=2\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    # 1.6.3: bake a CLEAN, minimal PATH into the unit (the systemd-user
    # default PATH lacks nvm / ~/.local/bin, so the daemon's driven turns
    # couldn't find codex / claude / node). NOT the operator's raw shell
    # PATH — that dragged in cuda / flutter / plugin bins / another
    # project's .venv-coord. `_clean_daemon_path` resolves exactly: the
    # project's own venv bin (greatminds + its ansible), the agent tool
    # dirs, and the standard system dirs.
    path_val = _clean_daemon_path(exec_cmd)
    if path_val and "Environment=PATH=" not in body:
        body = body.replace(
            "[Service]\n", f"[Service]\nEnvironment=PATH={path_val}\n", 1)
    return body


def install_template_unit() -> bool:
    """Idempotent: write the template unit if missing. Returns True if new.

    NOTE: the rendered body is computed at every call from the currently
    running greatminds binary, so re-running ``greatminds daemon install``
    from a different venv overwrites a stale unit. We still skip the
    write when the file exists AND its current contents already match
    the freshly-rendered body — avoids gratuitous mtime churn and
    daemon-reload triggers.
    """
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    dest = SYSTEMD_USER_DIR / TEMPLATE_UNIT_NAME
    body = _template_unit_body()
    if dest.is_file():
        try:
            if dest.read_text(encoding="utf-8") == body:
                return False
        except OSError:
            pass
    dest.write_text(body, encoding="utf-8")
    return True


def _project_dropin_dir(name: str) -> Path:
    return SYSTEMD_USER_DIR / f"{_instance_unit(name)}.d"


def _agent_env_file(name: str) -> Path:
    return AGENT_ENV_DIR / f"{name}.env"


def _selected_agent_env() -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k in AGENT_ENV_NAMES and v
    }
    host_auth_name = env.get("CLAUDE_CODE_HOST_AUTH_ENV_VAR")
    if host_auth_name and host_auth_name in os.environ:
        host_auth_value = os.environ.get(host_auth_name)
        if host_auth_value:
            env[host_auth_name] = host_auth_value
    return env


def capture_agent_env(name: str) -> bool:
    """Persist tool auth/session env from the invoking user shell.

    systemd user services do not inherit the interactive shell that made
    ``claude -p`` or provider-backed Claude work. The project-level
    ``PROJECT.env`` remains the fleet config source; this private per-session
    file carries only machine auth/session variables for agent tools.

    If the current shell has none of the allowlisted variables, leave any
    existing file untouched. That keeps a bare SSH maintenance command from
    erasing auth captured earlier from a real operator shell.
    """
    env = _selected_agent_env()
    if not env:
        return False
    AGENT_ENV_DIR.mkdir(parents=True, exist_ok=True)
    target = _agent_env_file(name)
    body = "".join(f"{k}={shlex.quote(v)}\n" for k, v in sorted(env.items()))
    old = target.read_text(encoding="utf-8") if target.is_file() else None
    if old == body:
        return False
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return True


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse the simple KEY=VALUE files greatminds writes for systemd.

    PROJECT.env may be edited by users, so this is deliberately forgiving:
    unknown lines are ignored and shell-style quoting is accepted.
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            parts = shlex.split(line, comments=True, posix=True)
        except ValueError:
            parts = [line]
        for part in parts:
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            if key:
                env[key] = val
    return env


def _daemon_candidate_env(name: str, project_dir: Path) -> dict[str, str]:
    """Environment a daemon-started driven subprocess should see.

    Mirrors the systemd drop-in order: current process env, then
    coordination/PROJECT.env, then the private captured agent env.
    """
    env = dict(os.environ)
    env.update(_parse_env_file(project_dir / "coordination" / "PROJECT.env"))
    env.update(_parse_env_file(_agent_env_file(name)))
    return env


def _claude_oauth_credential_diagnostic(env: dict[str, str]) -> str | None:
    """Return a concise diagnostic for Claude Code OAuth credential state."""
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    path = root / ".credentials.json"
    if not path.is_file():
        return f"Claude credentials file missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return f"Claude credentials file unreadable: {path} ({exc})"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return f"Claude credentials file has no claudeAiOauth block: {path}"
    expires_at = oauth.get("expiresAt")
    refresh_len = len(oauth.get("refreshToken") or "")
    access_len = len(oauth.get("accessToken") or "")
    expired = False
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        expired = int(expires_at) <= int(time.time() * 1000)
    if expired and refresh_len == 0:
        return (
            "Claude OAuth credentials are expired and have no refresh token "
            f"({path}); run `claude setup-token` or `claude auth login` as the "
            "same OS user, then restart the greatminds daemon."
        )
    if access_len == 0:
        return (
            f"Claude OAuth credentials have no access token ({path}); run "
            "`claude setup-token` or `claude auth login` as the same OS user."
        )
    return None


def _drop_stale_claude_snapshot_env(env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    host_auth_name = out.get("CLAUDE_CODE_HOST_AUTH_ENV_VAR")
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_BRIDGE_OAUTH_TOKEN",
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
        "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
        "CLAUDE_CODE_HOST_AUTH_REFRESH_TIMEOUT_MS",
    ):
        out.pop(name, None)
    if host_auth_name:
        out.pop(host_auth_name, None)
    return out


def has_driven_claude_roles(project_dir: Path) -> bool:
    lifecycles = _schema_lifecycles(project_dir)
    for p in (project_dir / "coord.yaml",
              project_dir / "coordination" / "coord.yaml"):
        doc = _safe_yaml(p)
        if not doc:
            continue
        for win in (doc.get("windows") or []):
            if not isinstance(win, dict):
                continue
            if (win.get("tool") or "").lower() != "claude":
                continue
            role = (win.get("role") or "").upper()
            if role and lifecycles.get(role) == "driven":
                return True
        return False
    return False


def _warn_missing_agent_env_for_claude(name: str, project_dir: Path) -> None:
    if not has_driven_claude_roles(project_dir):
        return
    if _selected_agent_env() or _agent_env_file(name).is_file():
        return
    warn(
        "no Claude/provider auth env captured for this driven-Claude fleet; "
        "if `claude -p` only works in a specific interactive shell, run "
        "`greatminds daemon start` from that shell or check "
        "`greatminds daemon doctor` before relying on driven Claude turns."
    )


def install_project_dropin(name: str, project_dir: Path) -> bool:
    """Per-instance drop-in giving the daemon — and every driven agent it
    spawns, which inherit its process env — the fleet's ``PROJECT.env`` as a
    systemd ``EnvironmentFile``. This is the single, clean injection point:
    coordd + all driven turns see PROJECT.env and the private captured
    machine auth/session env as real environment variables. The shared
    template can't carry either path, so they live in the instance drop-in.

    The leading ``-`` makes the file optional: a fleet with no PROJECT.env
    yet (or before setup writes it) simply gets no extra env, no failure.
    Returns True when the drop-in was written/changed.
    """
    env_file = project_dir / "coordination" / "PROJECT.env"
    agent_env_file = _agent_env_file(name)
    body = (
        "[Service]\n"
        f"EnvironmentFile=-{env_file}\n"
        f"EnvironmentFile=-{agent_env_file}\n"
    )
    d = _project_dropin_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "10-project-env.conf"
    if target.is_file():
        try:
            if target.read_text(encoding="utf-8") == body:
                return False
        except OSError:
            pass
    target.write_text(body, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 0320 (0311 Phase 3a): codex app-server template unit
# ---------------------------------------------------------------------------


def _appserver_unit_body() -> str | None:
    """Compose the app-server template unit with resolved paths.

    Returns None when codex OR node is not installed (no runnable
    ExecStart). The shipped canon copy uses ``__NODE_BIN__`` /
    ``__NODE_DIR__`` / ``__CODEX_BIN__`` placeholders substituted here.

    0320-iter2: ExecStart names node EXPLICITLY (``<node> <codex.js>
    …``) and sets ``Environment=PATH`` with node's dir first. codex's
    shebang is the relative ``#!/usr/bin/env node`` and systemd --user
    has no node on PATH → the iter-1 ``ExecStart=<codex.js> …`` failed
    with status=127. Naming the absolute node bypasses that shebang.
    """
    codex_exec = _resolved_codex_exec()
    node_exec = _resolved_node_exec()
    if codex_exec is None or node_exec is None:
        return None
    node_dir = str(Path(node_exec).parent)
    try:
        src = find_canon_dir() / "systemd" / APPSERVER_TEMPLATE_UNIT_NAME
        if src.is_file():
            return (src.read_text(encoding="utf-8")
                    .replace("__NODE_BIN__", node_exec)
                    .replace("__NODE_DIR__", node_dir)
                    .replace("__CODEX_BIN__", codex_exec))
    except Exception:  # noqa: BLE001
        pass
    return (
        "[Unit]\n"
        "Description=greatminds codex app-server for project %i\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"Environment=PATH={node_dir}:/usr/local/bin:/usr/bin:/bin:"
        "%h/.local/bin\n"
        f"ExecStart={node_exec} {codex_exec} app-server --listen "
        "unix://%t/greatminds-appserver-%i.sock\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_appserver_unit() -> bool | None:
    """Idempotent: write the app-server template unit if missing/stale.

    Returns True if newly written, False if already up to date, None
    when codex is unavailable (unit not installable — caller skips).
    Same skip-on-identical-body semantics as ``install_template_unit``.
    """
    body = _appserver_unit_body()
    if body is None:
        return None
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    dest = SYSTEMD_USER_DIR / APPSERVER_TEMPLATE_UNIT_NAME
    if dest.is_file():
        try:
            if dest.read_text(encoding="utf-8") == body:
                return False
        except OSError:
            pass
    dest.write_text(body, encoding="utf-8")
    return True


def _appserver_instance_unit(name: str) -> str:
    return f"greatminds-appserver@{name}.service"


def _schema_lifecycles(project_dir: Path) -> dict[str, str]:
    """Read ``roles[<ROLE>].lifecycle`` from the project's schema.yaml
    (preferred) or the packaged canon schema (fallback). Role keys are
    upper-cased for case-insensitive matching against coord.yaml roles."""
    for p in (project_dir / "coordination" / "schema.yaml",
              project_dir / "schema.yaml"):
        doc = _safe_yaml(p)
        if doc:
            break
    else:
        doc = None
    if doc is None:
        try:
            doc = _safe_yaml(find_canon_dir() / "schema.yaml")
        except Exception:  # noqa: BLE001
            doc = None
    out: dict[str, str] = {}
    for role, spec in ((doc or {}).get("roles") or {}).items():
        if isinstance(role, str) and isinstance(spec, dict):
            lc = spec.get("lifecycle")
            if isinstance(lc, str):
                out[role.upper()] = lc
    return out


def _safe_yaml(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def has_driven_codex_roles(project_dir: Path) -> bool:
    """0320: True iff the project's coord.yaml has at least one window
    with ``tool: codex`` whose schema lifecycle == 'driven'.

    Gates the app-server unit install/enable: a fleet only needs the
    codex app-server once any codex role is actually driven (Phase 3).
    Through Phase 2e the codex roles are still loop-mode, so this stays
    False and the unit is not installed."""
    lifecycles = _schema_lifecycles(project_dir)
    for p in (project_dir / "coord.yaml",
              project_dir / "coordination" / "coord.yaml"):
        doc = _safe_yaml(p)
        if not doc:
            continue
        for win in (doc.get("windows") or []):
            if not isinstance(win, dict):
                continue
            if (win.get("tool") or "").lower() != "codex":
                continue
            role = (win.get("role") or "").upper()
            if role and lifecycles.get(role) == "driven":
                return True
        return False  # coord.yaml found but no driven codex window
    return False


def detect_legacy_coordd() -> bool:
    """Return True iff the deprecated singleton ``coordd.service`` is enabled.

    Consumed by both ``greatminds daemon install`` (refuses to proceed)
    and ``greatminds update`` (task 0009, prompts auto-migration).
    """
    try:
        cp = subprocess.run(
            ["systemctl", "--user", "is-enabled", LEGACY_UNIT_NAME],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return False
    return cp.returncode == 0


# ---------------------------------------------------------------------------
# systemctl wrapper
# ---------------------------------------------------------------------------


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True,
    )


def _instance_unit(name: str) -> str:
    return f"greatminds-daemon@{name}.service"


def _run_verb(verb: str, name: str, *, expect_zero: bool = True) -> int:
    cp = _systemctl(verb, _instance_unit(name))
    if cp.stdout:
        click.echo(cp.stdout, nl=False)
    if cp.stderr:
        click.echo(cp.stderr, nl=False, err=True)
    if expect_zero and cp.returncode:
        raise click.exceptions.Exit(cp.returncode)
    return cp.returncode


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("daemon", short_help="manage per-project coordination daemon",
             help=__doc__)
def daemon() -> None:
    pass


@daemon.command("install", short_help="install template unit + register project")
@click.option("--name", "name", default=None,
              help="project name (default: coord.yaml `session`)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root (default: cwd)")
def install_cmd(name: str | None, project_dir: Path | None) -> None:
    pd = (project_dir or Path.cwd()).resolve()
    resolved = name or _read_session_from_coord_yaml(pd)
    if not resolved:
        err("--name not given and coord.yaml has no `session:` key")
        raise click.exceptions.Exit(2)

    if detect_legacy_coordd():
        warn("legacy singleton `coordd.service` detected on this host.")
        info("Run `greatminds daemon migrate --yes` to remove it, then retry,")
        info("or do it manually:")
        info("  systemctl --user stop coordd.service")
        info("  systemctl --user disable coordd.service")
        info(f"  rm {SYSTEMD_USER_DIR / LEGACY_UNIT_NAME}")
        info("  systemctl --user daemon-reload")
        raise click.exceptions.Exit(2)

    wrote_unit = install_template_unit()
    register_project(resolved, pd)
    captured_env = capture_agent_env(resolved)
    _warn_missing_agent_env_for_claude(resolved, pd)
    # Wire the fleet's PROJECT.env into the daemon (and every driven agent
    # it spawns) as a systemd EnvironmentFile — the single clean injection.
    wrote_dropin = install_project_dropin(resolved, pd)

    if wrote_unit or wrote_dropin:
        _systemctl("daemon-reload")
    if wrote_unit:
        ok(f"template unit installed at {SYSTEMD_USER_DIR / TEMPLATE_UNIT_NAME}")
    else:
        info("template unit already present, no rewrite")
    if wrote_dropin:
        ok("PROJECT.env wired into daemon env "
           f"(EnvironmentFile=-{pd / 'coordination' / 'PROJECT.env'})")
    if captured_env:
        ok("agent auth/session env captured for daemon "
           f"(0600 {_agent_env_file(resolved)})")
    ok(f"project '{resolved}' registered → {pd}")

    # 0307: enable the per-project instance unit so it lives under
    # default.target.wants/ and survives KDE logout / shutdown.
    # Pre-0307 the template install never ran ``systemctl --user
    # enable`` → ``is-enabled`` stayed ``disabled; preset: enabled``
    # → coordd was torn down with default.target on logout.
    # ``enable`` is idempotent — re-installs reruns safely.
    instance = _instance_unit(resolved)
    enable_cp = _systemctl("enable", instance)
    if enable_cp.returncode == 0:
        ok(f"{instance} enabled (survives logout / shutdown)")
    else:
        warn(
            f"`systemctl --user enable {instance}` failed (rc="
            f"{enable_cp.returncode}); coordd may not restart after "
            f"logout. Stderr: {(enable_cp.stderr or '').strip()[:200]}"
        )

    # 0320 (0311 Phase 3a): if this fleet has driven codex roles, install
    # + enable the per-fleet codex app-server unit alongside coordd. It
    # hosts the codex worker threads that the Phase-3b driver (0321)
    # drives via the app-server protocol. Gated on driven codex roles so
    # claude-only / pre-Phase-3 fleets never get the extra unit.
    if has_driven_codex_roles(pd):
        wrote_app = install_appserver_unit()
        if wrote_app is None:
            warn(
                "  driven codex roles present but `codex` is not on PATH "
                "— skipping app-server unit. Install codex, then re-run "
                "`greatminds daemon install`."
            )
        else:
            app_instance = _appserver_instance_unit(resolved)
            if wrote_app:
                _systemctl("daemon-reload")
                ok(f"app-server unit installed at "
                   f"{SYSTEMD_USER_DIR / APPSERVER_TEMPLATE_UNIT_NAME}")
            else:
                info("app-server unit already present, no rewrite")
            app_enable = _systemctl("enable", app_instance)
            if app_enable.returncode == 0:
                ok(f"{app_instance} enabled (survives logout / shutdown)")
            else:
                warn(
                    f"`systemctl --user enable {app_instance}` failed "
                    f"(rc={app_enable.returncode}). Stderr: "
                    f"{(app_enable.stderr or '').strip()[:200]}"
                )

    info(f"next: `greatminds daemon start --project {resolved}`")


@daemon.command("repair",
                short_help="0307: ensure existing daemon instance is "
                           "systemctl-enabled (one-shot for pre-0307 "
                           "fleets that installed without --enable)")
@click.option("--name", "name", default=None,
              help="project name (default: coord.yaml `session`)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root (default: cwd)")
def repair_cmd(name: str | None, project_dir: Path | None) -> None:
    """0307: idempotent ``systemctl --user enable`` for the project's
    daemon instance. Pre-0307 ``daemon install`` skipped the enable
    step → the unit was torn down with default.target on KDE
    logout. Existing fleets need this one-time repair to recover
    survive-logout behavior."""
    pd = (project_dir or Path.cwd()).resolve()
    resolved = name or _read_session_from_coord_yaml(pd)
    if not resolved:
        err("--name not given and coord.yaml has no `session:` key")
        raise click.exceptions.Exit(2)
    instance = _instance_unit(resolved)
    cp = _systemctl("enable", instance)
    if cp.returncode == 0:
        ok(f"{instance} enabled (survives logout / shutdown)")
    else:
        err(
            f"`systemctl --user enable {instance}` failed "
            f"(rc={cp.returncode}). Stderr: "
            f"{(cp.stderr or '').strip()[:300]}"
        )
        raise click.exceptions.Exit(cp.returncode)


@daemon.command("migrate", short_help="remove legacy singleton coordd.service")
@click.option("--yes", is_flag=True,
              help="confirm removal of legacy coordd.service")
def migrate_cmd(yes: bool) -> None:
    if not detect_legacy_coordd():
        info("no legacy coordd.service detected — nothing to migrate")
        return
    if not yes:
        warn("refusing to remove legacy unit without --yes")
        info("Run: `greatminds daemon migrate --yes` to confirm.")
        raise click.exceptions.Exit(2)
    _systemctl("stop", LEGACY_UNIT_NAME)
    _systemctl("disable", LEGACY_UNIT_NAME)
    legacy = SYSTEMD_USER_DIR / LEGACY_UNIT_NAME
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError as exc:
            warn(f"could not remove {legacy}: {exc}")
    _systemctl("daemon-reload")
    ok("legacy coordd.service removed.")


def _project_options(fn):
    """Decorator: share --project / --project-dir between verbs."""
    fn = click.option("--project-dir",
                      type=click.Path(file_okay=False, path_type=Path),
                      default=None,
                      help="project root (default: cwd)")(fn)
    fn = click.option("--project", default=None,
                      help="project name (default: coord.yaml session)")(fn)
    return fn


@daemon.command("start", short_help="start the daemon for a project")
@_project_options
def start_cmd(project: str | None, project_dir: Path | None) -> None:
    name = _resolve_project_name(project, project_dir)
    if capture_agent_env(name):
        info("agent auth/session env refreshed before daemon start")
    pd = project_dir.resolve() if project_dir else lookup_project_dir(name)
    if pd is not None:
        _warn_missing_agent_env_for_claude(name, pd)
    _run_verb("start", name)


@daemon.command("stop", short_help="stop the daemon for a project")
@_project_options
def stop_cmd(project: str | None, project_dir: Path | None) -> None:
    _run_verb("stop", _resolve_project_name(project, project_dir))


def _refresh_units_before_restart(name: str,
                                  project_dir: Path | None) -> bool:
    """issue #13: re-render + rewrite the on-disk systemd units so a restart
    always comes up with the CURRENT baked PATH.

    A template unit rendered before PATH-baking (pre-1.6.3) — or rendered
    when a tool was not yet resolvable — carries no ``Environment=PATH=``,
    so a plain ``systemctl restart`` brings coordd up with systemd's bare
    --user PATH and it can no longer exec claude / codex / ansible-playbook
    (driven turns + stand deploys break). ``install_template_unit`` /
    ``install_project_dropin`` / ``install_appserver_unit`` are idempotent
    (they skip the write when the on-disk body already matches the freshly
    rendered one), so when the units are already current this is a no-op.
    Returns True if any unit changed (the caller then ``daemon-reload``s)."""
    changed = install_template_unit()
    pd = project_dir.resolve() if project_dir else lookup_project_dir(name)
    if pd is not None:
        if install_project_dropin(name, pd):
            changed = True
        if has_driven_codex_roles(pd) and install_appserver_unit():
            changed = True
    capture_agent_env(name)
    if changed:
        _systemctl("daemon-reload")
    return changed


@daemon.command("restart", short_help="restart the daemon for a project")
@_project_options
def restart_cmd(project: str | None, project_dir: Path | None) -> None:
    name = _resolve_project_name(project, project_dir)
    # issue #13: refresh the on-disk unit (re-bake PATH) before restarting,
    # so a stale pre-PATH unit self-heals instead of restarting coordd with
    # systemd's bare PATH (which cannot exec claude / codex / ansible).
    if _refresh_units_before_restart(name, project_dir):
        info("daemon units refreshed (PATH re-baked) before restart")
    pd = project_dir.resolve() if project_dir else lookup_project_dir(name)
    if pd is not None:
        _warn_missing_agent_env_for_claude(name, pd)
    _run_verb("restart", name)


def _claude_headless_probe(name: str, project_dir: Path,
                           timeout_sec: float) -> tuple[bool, str]:
    env = _drop_stale_claude_snapshot_env(
        _daemon_candidate_env(name, project_dir))
    credential_diag = _claude_oauth_credential_diagnostic(env)
    claude_argv = [
        "claude", "-p",
        "Reply with OK only. This is a greatminds daemon doctor probe.",
        "--strict-mcp-config",
        "--permission-mode", "auto",
        "--output-format", "json",
    ]
    argv = ["bash", "-lc", shlex.join(claude_argv)]
    started = time.monotonic()
    try:
        cp = subprocess.run(
            argv, cwd=str(project_dir), env=env,
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except FileNotFoundError:
        return False, "`claude` is not on the daemon PATH"
    except subprocess.TimeoutExpired:
        return False, f"`claude -p` did not finish within {timeout_sec:.0f}s"

    elapsed = time.monotonic() - started
    stdout = cp.stdout or ""
    stderr = cp.stderr or ""
    try:
        obj = json.loads(stdout)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and obj.get("is_error"):
        status = obj.get("api_error_status")
        result = str(obj.get("result") or "").strip()
        if status in (401, 403) or "auth" in result.lower():
            suffix = f" {credential_diag}" if credential_diag else ""
            return (
                False,
                f"Claude auth failed in daemon-equivalent env "
                f"(status={status}, rc={cp.returncode}, {elapsed:.1f}s): "
                f"{result[:240]}.{suffix}",
            )
        return (
            False,
            f"Claude returned an error in daemon-equivalent env "
            f"(status={status}, rc={cp.returncode}, {elapsed:.1f}s): "
            f"{result[:240]}",
        )
    if cp.returncode != 0:
        detail = (stderr or stdout).strip().replace("\n", " ")
        return (
            False,
            f"`claude -p` exited rc={cp.returncode} in daemon-equivalent env: "
            f"{detail[:240]}",
        )
    return True, f"Claude headless probe succeeded in {elapsed:.1f}s"


@daemon.command("doctor", short_help="check daemon agent runtime env")
@_project_options
@click.option("--timeout-sec", type=float, default=60.0, show_default=True,
              help="headless Claude probe timeout")
def doctor_cmd(project: str | None, project_dir: Path | None,
               timeout_sec: float) -> None:
    """Check whether daemon-driven agent tools work in daemon-equivalent env."""
    name = _resolve_project_name(project, project_dir)
    pd = project_dir.resolve() if project_dir else lookup_project_dir(name)
    if pd is None:
        err(f"project '{name}' is not registered; pass --project-dir")
        raise click.exceptions.Exit(2)

    click.echo(f"project: {name}")
    click.echo(f"root: {pd}")
    agent_env = _agent_env_file(name)
    if agent_env.is_file():
        ok(f"agent env file present: {agent_env}")
    else:
        warn(f"agent env file missing: {agent_env}")
    if has_driven_claude_roles(pd):
        ok("driven Claude roles detected")
        ok_probe, detail = _claude_headless_probe(name, pd, timeout_sec)
        if ok_probe:
            ok(detail)
        else:
            err(detail)
            raise click.exceptions.Exit(1)
    else:
        info("no driven Claude roles detected")


@daemon.command("status", short_help="show daemon status for a project")
@_project_options
def status_cmd(project: str | None, project_dir: Path | None) -> None:
    # systemctl status exits 3 for inactive — informational, not an error.
    _run_verb("status",
              _resolve_project_name(project, project_dir),
              expect_zero=False)


@daemon.command("list", short_help="list all registered projects + active state")
def list_cmd() -> None:
    reg = load_registry()
    if not reg:
        info("(no projects registered — run `greatminds daemon install`)")
        return
    for name in sorted(reg):
        pdir = reg[name]
        cp = _systemctl("is-active", _instance_unit(name))
        state = (cp.stdout or "").strip() or "unknown"
        click.echo(f"  {name:<24}  {state:<10}  {pdir}")
