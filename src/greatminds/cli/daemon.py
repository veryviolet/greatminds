"""greatminds daemon — per-project coordination daemon supervision.

Wraps ``systemctl --user`` to manage instances of the
``greatminds-daemon@<project>.service`` template unit. Each greatminds
project on the host gets ONE daemon instance keyed by its
registered project name (or the project directory name by default),
so multiple projects on the same user can run their daemons
concurrently without colliding on a global unit name.

Subcommands::

    greatminds daemon install [--name NAME] [--project-dir DIR]
    greatminds daemon start    [--project NAME] [--project-dir DIR]
    greatminds daemon stop     [--project NAME] [--project-dir DIR]
    greatminds daemon restart  [--project NAME] [--project-dir DIR]
    greatminds daemon status   [--project NAME] [--project-dir DIR]
    greatminds daemon list

``install`` is idempotent: it writes the template unit if missing and
adds the ``{name → project_dir}`` entry to the per-user registry at
``~/.config/greatminds/projects.json``.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
import yaml

from greatminds.core.paths import (
    coord_yaml_path,
    find_canon_dir,
    project_env_file,
)
from greatminds.core.schema import load_schema_snapshot
from greatminds.cli._colors import err, info, ok, warn


REGISTRY_DIR = Path.home() / ".config" / "greatminds"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"
AGENT_ENV_DIR = REGISTRY_DIR / "agent-env"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TEMPLATE_UNIT_NAME = "greatminds-daemon@.service"
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


def _current_user_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001
        return Path.home()


def _agent_tool_env(env: dict[str, str]) -> dict[str, str]:
    """Env for non-interactive agent CLI probes.

    Claude Code stores first-party credentials under ``$HOME/.claude`` and is
    commonly installed in ``$HOME/.local/bin``. A daemon/subprocess must set
    both explicitly instead of relying on interactive shell inheritance.
    """
    home = _current_user_home()
    out = dict(env)
    out["HOME"] = str(home)
    path_parts = [
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
    ]
    path_parts.extend((out.get("PATH") or os.defpath).split(":"))
    path_parts.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    seen: set[str] = set()
    out["PATH"] = ":".join(
        p for p in path_parts if p and not (p in seen or seen.add(p)))
    return out


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
    home = _current_user_home()
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
    """Register a project without redirecting an existing service identity."""
    _validate_project_name(name)
    reg = load_registry()
    if name in reg and Path(reg[name]).resolve() != project_dir.resolve():
        raise click.ClickException("project name is registered to a different directory")
    reg[name] = str(project_dir.resolve())
    save_registry(reg)


def lookup_project_dir(name: str) -> Path | None:
    """Reverse-resolve registry: name → project_dir."""
    v = load_registry().get(name)
    return Path(v) if isinstance(v, str) and v else None


# ---------------------------------------------------------------------------
# Project identity is independent of agent/window configuration.
# ---------------------------------------------------------------------------


def _validate_project_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise click.ClickException("project name must be 1–80 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit")
    return name


def _resolve_project_name(project: str | None,
                          project_dir: Path | None) -> str:
    if project:
        name = _validate_project_name(project)
        registered = lookup_project_dir(name)
        if project_dir is not None and registered is not None and registered.resolve() != project_dir.resolve():
            raise click.ClickException("project name is registered to a different directory")
        return name
    from greatminds.core.paths import find_project_dir
    pd = (project_dir.resolve() if project_dir is not None else
          find_project_dir(Path.cwd(), strict=False, use_env=False))
    names = sorted(name for name, root in load_registry().items() if Path(root).resolve() == pd)
    if len(names) > 1:
        raise click.ClickException("project has multiple registered names; pass --project explicitly")
    name = _validate_project_name(names[0] if names else pd.name)
    registered = lookup_project_dir(name)
    if registered is not None and registered.resolve() != pd:
        raise click.ClickException("directory name is already registered to another project; choose an explicit name")
    return name


# ---------------------------------------------------------------------------
# Common daemon template
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
    home = _current_user_home()
    dirs.append(str(home / ".local" / "bin"))
    dirs.append(str(home / ".npm-global" / "bin"))
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
    home_val = str(_current_user_home())
    if home_val and "Environment=HOME=" not in body:
        body = body.replace(
            "[Service]\n", f"[Service]\nEnvironment=HOME={home_val}\n", 1)
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
    .greatminds/PROJECT.env, then the private captured agent env.
    """
    env = dict(os.environ)
    env.update(_parse_env_file(project_env_file(project_dir)))
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


def has_driven_claude_roles(project_dir: Path) -> bool:
    lifecycles = _schema_lifecycles(project_dir)
    doc = _safe_yaml(coord_yaml_path(project_dir))
    if not doc:
        return False
    for win in (doc.get("windows") or []):
        if not isinstance(win, dict):
            continue
        if (win.get("tool") or "").lower() != "claude":
            continue
        role = (win.get("role") or "").upper()
        if role and lifecycles.get(role) == "driven":
            return True
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
    env_file = project_env_file(project_dir)
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


def _schema_lifecycles(project_dir: Path) -> dict[str, str]:
    """Use the same installed contract as coordd and task validation.

    Project mirrors can be stale after an upgrade and must not change which
    services are installed. Explicit canon overrides still apply to all paths.
    """
    del project_dir  # Retain the helper signature for existing callers.
    doc = load_schema_snapshot(find_canon_dir()).document
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


# ---------------------------------------------------------------------------
# systemctl wrapper
# ---------------------------------------------------------------------------


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException("systemctl did not finish within 30 seconds; inspect service state before retrying") from exc
    except OSError as exc:
        raise click.ClickException(f"cannot run systemctl: {exc}") from exc


def _checked_systemctl(*args: str) -> subprocess.CompletedProcess:
    from greatminds.cli.chat import terminal_text
    cp = _systemctl(*args)
    if cp.returncode:
        detail = terminal_text((cp.stderr or cp.stdout or "").strip())[:500]
        raise click.ClickException(f"systemctl --user {' '.join(args)} failed (rc={cp.returncode}): {detail}")
    return cp


def _instance_unit(name: str) -> str:
    return f"greatminds-daemon@{_validate_project_name(name)}.service"


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
              help="project name (default: registry or project directory name)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root (default: cwd)")
def install_cmd(name: str | None, project_dir: Path | None) -> None:
    from greatminds.core.paths import find_project_dir
    pd = project_dir.resolve() if project_dir else find_project_dir(Path.cwd(), strict=False, use_env=False)
    resolved = _resolve_project_name(name, pd)

    wrote_unit = install_template_unit()
    register_project(resolved, pd)
    captured_env = capture_agent_env(resolved)
    _warn_missing_agent_env_for_claude(resolved, pd)
    # Wire the fleet's PROJECT.env into the daemon (and every driven agent
    # it spawns) as a systemd EnvironmentFile — the single clean injection.
    wrote_dropin = install_project_dropin(resolved, pd)

    # Reload even on an identical retry: the previous manager reload may have failed.
    _checked_systemctl("daemon-reload")
    if wrote_unit:
        ok(f"template unit installed at {SYSTEMD_USER_DIR / TEMPLATE_UNIT_NAME}")
    else:
        info("template unit already present, no rewrite")
    if wrote_dropin:
        ok("PROJECT.env wired into daemon env "
           f"(EnvironmentFile=-{project_env_file(pd)})")
    if captured_env:
        ok("agent auth/session env captured for daemon "
           f"(0600 {_agent_env_file(resolved)})")
    ok(f"project '{resolved}' registered → {pd}")

    instance = _instance_unit(resolved)
    _checked_systemctl("enable", instance)
    ok(f"{instance} enabled for the user manager's default target")

    info(f"next: `greatminds daemon start --project {resolved}`")


@daemon.command("repair",
                short_help="ensure the daemon instance is systemctl-enabled")
@click.option("--name", "name", default=None,
              help="project name (default: registry or project directory name)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root (default: cwd)")
def repair_cmd(name: str | None, project_dir: Path | None) -> None:
    """Idempotently enable the project's daemon instance."""
    from greatminds.core.paths import find_project_dir
    pd = project_dir.resolve() if project_dir else find_project_dir(Path.cwd(), strict=False, use_env=False)
    resolved = _resolve_project_name(name, pd)
    instance = _instance_unit(resolved)
    _checked_systemctl("enable", instance)
    ok(f"{instance} enabled for the user manager's default target")


def _project_options(fn):
    """Decorator: share --project / --project-dir between verbs."""
    fn = click.option("--project-dir",
                      type=click.Path(file_okay=False, path_type=Path),
                      default=None,
                      help="project root (default: cwd)")(fn)
    fn = click.option("--project", default=None,
                      help="project name (default: registry or project directory name)")(fn)
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
    """Refresh the common daemon unit and project environment before restart."""
    changed = install_template_unit()
    pd = project_dir.resolve() if project_dir else lookup_project_dir(name)
    if pd is not None:
        if install_project_dropin(name, pd):
            changed = True
    capture_agent_env(name)
    _checked_systemctl("daemon-reload")
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


@daemon.command("doctor", short_help="check ACP configuration without launching agents")
@_project_options
@click.option("--json", "as_json", is_flag=True, help="Machine-readable static checks")
def doctor_cmd(project: str | None, project_dir: Path | None,
               as_json: bool) -> None:
    """Check declared ACP prerequisites; does not verify login or protocol support."""
    from greatminds.runtime.observation import configuration
    from greatminds.cli.chat import terminal_text

    if project_dir is not None:
        pd = project_dir.resolve()
    elif project:
        pd = lookup_project_dir(project)
        if pd is None:
            raise click.ClickException("project is not registered; pass --project-dir")
    else:
        from greatminds.core.paths import find_project_dir
        pd = find_project_dir(Path.cwd(), use_env=False)
    _, config = configuration(pd)
    name = _resolve_project_name(project, pd)
    env = _daemon_candidate_env(name, pd)
    checks = []
    for agent in config.agents:
        effective = dict(env)
        effective.update({dest: env[ref] for dest, ref in agent.environment if ref in env})
        executable = agent.argv[0]
        # Relative executables/PATH entries depend on each binding's workspace.
        # Do not resolve them against the operator's unrelated current directory.
        if os.path.isabs(executable):
            available = Path(executable).is_file() and os.access(executable, os.X_OK)
        elif "/" in executable:
            available = False
        else:
            path = os.pathsep.join(part for part in effective.get("PATH", os.defpath).split(os.pathsep)
                                   if os.path.isabs(part))
            available = shutil.which(executable, path=path) is not None
        missing = [key for key in agent.required_env if not env.get(key)]
        checks.append({"agent": agent.id, "executable_available": available,
                       "missing_required_env": missing,
                       "ready": available and not missing})
    result = {"project": str(pd), "transport": "acp", "verification": "static",
              "environment": "current process plus project and captured environment files",
              "agents": checks, "ready": all(row["ready"] for row in checks)}
    if as_json:
        click.echo(json.dumps(result, sort_keys=True))
    else:
        click.echo(terminal_text(f"ACP configuration: {pd}"))
        for row in checks:
            click.echo(terminal_text(f"{row['agent']}: executable={'found' if row['executable_available'] else 'unresolved'}; "
                                    f"missing environment={', '.join(row['missing_required_env']) or 'none'}"))
        click.echo("Static checks only; authentication and ACP capabilities require a live session.")
    if not result["ready"]:
        raise click.exceptions.Exit(1)


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
