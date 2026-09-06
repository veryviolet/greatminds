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
import shutil
import subprocess
import sys
from pathlib import Path

import click

from greatminds.core.paths import (
    find_canon_dir,
    project_env_file,
)
from greatminds.cli._colors import info, ok


REGISTRY_DIR = Path.home() / ".config" / "greatminds"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"
AGENT_ENV_DIR = REGISTRY_DIR / "agent-env"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TEMPLATE_UNIT_NAME = "greatminds-daemon@.service"


def _current_user_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001
        return Path.home()


def _greatminds_argv() -> tuple[str, ...]:
    """Resolve the launcher without splitting paths or following interpreter symlinks."""
    found = shutil.which("greatminds")
    if found:
        return (str(Path(found).absolute()),)
    return (str(Path(sys.executable).absolute()), "-m", "greatminds.cli.main")


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


def _clean_daemon_path(executable: str) -> str:
    """Stable generic baseline; project EnvironmentFiles can explicitly set PATH."""
    home = _current_user_home()
    dirs = [str(Path(executable).absolute().parent), str(home / ".local/bin"),
            str(home / ".npm-global/bin"), "/usr/local/sbin", "/usr/local/bin",
            "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    # A colon in a directory cannot be represented as a PATH component.
    return ":".join(dict.fromkeys(directory for directory in dirs if ":" not in directory))


def _template_unit_body() -> str:
    """Render the common ACP service with literal launcher and environment paths."""
    from greatminds.core.service_environment import unit_word
    argv = _greatminds_argv()
    literal_argv = ((":" + argv[0], *argv[1:])
                    if any("$" in arg for arg in argv) else argv)
    exec_cmd = " ".join(unit_word(arg) for arg in literal_argv)
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
    path_val = _clean_daemon_path(argv[0])
    body = body.replace("[Service]\n", "[Service]\nEnvironment=" +
                        unit_word("PATH=" + path_val) + "\nEnvironment=" +
                        unit_word("HOME=" + str(_current_user_home())) + "\n", 1)
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


def capture_agent_env(name: str, project_dir: Path | None = None) -> bool:
    """Capture only environment references declared by ACP agents and commands.

    Preserve still-declared captured values absent from a maintenance shell;
    remove references no longer present in the execution contract.
    """
    _validate_project_name(name)
    project_dir = project_dir or lookup_project_dir(name)
    if project_dir is None or not (project_dir / "coordination/execution.yaml").exists():
        return False
    from greatminds.runtime.observation import configuration
    _, config = configuration(project_dir)
    names = set()
    for definition in (*config.agents, *config.commands):
        names.update(definition.required_env)
        names.update(ref for _, ref in definition.environment)
    target = _agent_env_file(name)
    previous = _parse_env_file(target)
    env = {key: previous[key] for key in names if key in previous}
    env.update({key: os.environ[key] for key in names if key in os.environ})
    if not env and not target.exists():
        return False
    from greatminds.core.service_environment import encode_environment
    try:
        body = encode_environment(env)
    except (ValueError, UnicodeError) as exc:
        raise click.ClickException("captured environment contains invalid text") from exc
    old = target.read_bytes() if target.is_file() else None
    if old == body.encode("utf-8"):
        target.chmod(0o600)
        return False
    from greatminds.core.storage import atomic_bytes
    atomic_bytes(target, body.encode("utf-8"))
    return True


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read systemd EnvironmentFile values without evaluating shell syntax."""
    from greatminds.core.service_environment import decode_environment
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise click.ClickException("cannot read environment file") from exc
    try:
        return decode_environment(text)
    except ValueError as exc:
        raise click.ClickException("invalid environment file syntax") from exc


def _daemon_candidate_env(name: str, project_dir: Path) -> dict[str, str]:
    """Environment a daemon-started driven subprocess should see.

    Mirrors the systemd drop-in order: current process env, then
    .greatminds/PROJECT.env, then the private captured agent env.
    """
    env = dict(os.environ)
    env.update(_parse_env_file(project_env_file(project_dir)))
    env.update(_parse_env_file(_agent_env_file(name)))
    return env


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
    from greatminds.core.service_environment import unit_word
    env_file = unit_word("-" + str(project_env_file(project_dir)))
    agent_env_file = unit_word("-" + str(_agent_env_file(name)))
    body = (
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        f"EnvironmentFile={agent_env_file}\n"
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
    captured_env = capture_agent_env(resolved, pd)
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
    if capture_agent_env(name, project_dir):
        info("agent auth/session env refreshed before daemon start")
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
    capture_agent_env(name, pd)
    _checked_systemctl("daemon-reload")
    return changed


@daemon.command("restart", short_help="restart the daemon for a project")
@_project_options
def restart_cmd(project: str | None, project_dir: Path | None) -> None:
    name = _resolve_project_name(project, project_dir)
    # Refresh service configuration before asking the manager to restart.
    if _refresh_units_before_restart(name, project_dir):
        info("daemon units refreshed (PATH re-baked) before restart")
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
