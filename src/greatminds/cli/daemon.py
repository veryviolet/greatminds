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
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


REGISTRY_DIR = Path.home() / ".config" / "greatminds"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
TEMPLATE_UNIT_NAME = "greatminds-daemon@.service"
LEGACY_UNIT_NAME = "coordd.service"


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
    try:
        src = find_canon_dir() / "systemd" / TEMPLATE_UNIT_NAME
        if src.is_file():
            body = src.read_text(encoding="utf-8")
            return body.replace("__GREATMINDS_BIN__", exec_cmd)
    except Exception:  # noqa: BLE001
        pass
    return (
        "[Unit]\n"
        "Description=greatminds coordination daemon for project %i\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_cmd} coordd --project %i\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


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

    if wrote_unit:
        _systemctl("daemon-reload")
        ok(f"template unit installed at {SYSTEMD_USER_DIR / TEMPLATE_UNIT_NAME}")
    else:
        info("template unit already present, no rewrite")
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
    _run_verb("start", _resolve_project_name(project, project_dir))


@daemon.command("stop", short_help="stop the daemon for a project")
@_project_options
def stop_cmd(project: str | None, project_dir: Path | None) -> None:
    _run_verb("stop", _resolve_project_name(project, project_dir))


@daemon.command("restart", short_help="restart the daemon for a project")
@_project_options
def restart_cmd(project: str | None, project_dir: Path | None) -> None:
    _run_verb("restart", _resolve_project_name(project, project_dir))


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
