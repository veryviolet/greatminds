"""greatminds update — single-command upgrade of CLI + daemon + agents.

Three moving parts to keep in sync after a release:

  1. The package itself in the venv where ``greatminds`` is installed
     (typically the project's ``.venv/`` from ``uv add greatminds`` or
     ``pip install greatminds``).
  2. The systemd-user daemon ``greatminds-daemon@<project>.service``
     (per-project, refactored in task 0008).
  3. The tmux agents (restarted via ``greatminds restart`` from 0001).

This command runs all three in one pass, with a self-replacement step
(``os.execv``) between phase 1 and the rest so the new code drives the
post-pip phases. ``--post-pip`` is the idempotent recovery mode used
both by the self-replace dispatch and by users whose package version
got out ahead of the daemon (e.g. raw ``pip install --upgrade``).

CLI surface::

    greatminds update                  # full path: pip + daemon + agents
    greatminds update --post-pip       # skip pip; daemon + agents only
    greatminds update --check          # report would-be changes, no actions
    greatminds update --dry-run        # alias of --check
    greatminds update --major          # allow major-version bump
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click

from greatminds import __version__
from greatminds.cli._colors import err, info, ok, warn


PYPI_JSON_URL = "https://pypi.org/pypi/greatminds/json"


# ---------------------------------------------------------------------------
# Version handling
# ---------------------------------------------------------------------------


def _parse_semver(v: str) -> tuple[int, int, int]:
    """Loose semver parse: take leading int.int.int, ignore pre-release suffix."""
    parts = v.split("-", 1)[0].split("+", 1)[0].split(".")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return (0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])  # type: ignore[return-value]


def _fetch_latest_pypi_version() -> str:
    """Fetch ``greatminds`` latest version string from PyPI's JSON API."""
    req = urllib.request.Request(
        PYPI_JSON_URL,
        headers={"User-Agent": f"greatminds-update/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        err(f"could not reach PyPI: {exc}")
        raise click.exceptions.Exit(2)
    except json.JSONDecodeError as exc:
        err(f"PyPI returned non-JSON: {exc}")
        raise click.exceptions.Exit(2)
    info_block = data.get("info") if isinstance(data, dict) else None
    version = info_block.get("version") if isinstance(info_block, dict) else None
    if not isinstance(version, str) or not version:
        err("PyPI JSON missing `info.version`")
        raise click.exceptions.Exit(2)
    return version


def _is_major_bump(current: str, latest: str) -> bool:
    c_major = _parse_semver(current)[0]
    l_major = _parse_semver(latest)[0]
    return l_major > c_major


# ---------------------------------------------------------------------------
# Greatminds binary discovery (for the os.execv self-replacement)
# ---------------------------------------------------------------------------


def _greatminds_bin() -> str:
    """Resolve the absolute path to the `greatminds` console script,
    normalized to absolute (see also cli/setup.py:_greatminds_bin).
    """
    found = shutil.which("greatminds")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m greatminds.cli.main"


# ---------------------------------------------------------------------------
# Step runners
# ---------------------------------------------------------------------------


def _upgrade_command_for_env(env_type: str | None,
                              project_dir: Path) -> list[str]:
    """0299: pick the correct upgrade command per env manager.

    Pre-0299 ``update`` always called ``<py> -m pip install --upgrade
    greatminds`` regardless of env manager. Under uv that broke the
    lock invariant: pip wrote 1.3.9 into the venv, ``uv.lock`` still
    pinned 1.3.0, the next ``uv run`` snapped back. Silent infinite
    loop.

    For each detected env_type return the binary + args that update
    THE LOCKFILE (the source of truth) so the next activation picks
    up the new version. ``venv`` / ``external-venv`` / None fall
    back to the pre-0299 pip path — those have no lock to maintain.
    """
    if env_type == "uv":
        # Two-step: refresh the lock entry, then sync the venv.
        return ["uv", "lock", "--upgrade-package", "greatminds"]
    if env_type == "poetry":
        return ["poetry", "update", "--directory", str(project_dir),
                "greatminds"]
    if env_type == "pixi":
        return ["pixi", "update", "--manifest-path",
                str(project_dir / "pixi.toml"), "greatminds"]
    if env_type == "conda":
        return ["conda", "update", "-y", "greatminds"]
    # venv / external-venv / override / None → pip
    return [sys.executable, "-m", "pip", "install", "--upgrade",
            "greatminds"]


def _step_pip_upgrade(major: bool) -> bool:
    """Run the env-appropriate upgrade command.

    Returns True if an upgrade was actually performed (caller must
    self-replace into the new binary), False if the package was already
    current (caller still runs the config-migration + restart phase
    in-process — config can be stale even when the package is current).
    """
    current = __version__
    info(f"==> current: greatminds {current}")
    latest = _fetch_latest_pypi_version()
    info(f"==> latest on PyPI: {latest}")

    if _parse_semver(latest) <= _parse_semver(current):
        ok("already up to date (package); will still reconcile config")
        return False

    if _is_major_bump(current, latest) and not major:
        err(
            f"major upgrade {current.split('.')[0]}.x → "
            f"{latest.split('.')[0]}.0 may require config migration; "
            "re-run with --major to acknowledge."
        )
        raise click.exceptions.Exit(2)

    # 0299: branch on env manager so we update the lock file, not
    # just the venv binary.
    from greatminds.core.env import detect as detect_env_setup
    setup = detect_env_setup(Path.cwd())
    info(f"==> env: {setup.env_type or 'system'} ({setup.source})")

    cmd = _upgrade_command_for_env(setup.env_type, Path.cwd())
    info(f"==> upgrading package... {current} → {latest}")
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        err(f"upgrade command failed: {' '.join(cmd)}")
        if cp.stderr:
            click.echo(cp.stderr, nl=False, err=True)
        raise click.exceptions.Exit(cp.returncode)
    ok(f"    ✓ {' '.join(cmd[:3])}… ({current} → {latest})")

    # 0299: uv needs a second pass to actually pull the new wheel
    # into the venv after the lock refresh. Other env managers do
    # this implicitly in their `update` command.
    if setup.env_type == "uv":
        sync_cmd = ["uv", "sync"]
        info(f"==> {' '.join(sync_cmd)}")
        cp2 = subprocess.run(sync_cmd, capture_output=True, text=True)
        if cp2.returncode != 0:
            err("uv sync failed after lock refresh:")
            if cp2.stderr:
                click.echo(cp2.stderr, nl=False, err=True)
            raise click.exceptions.Exit(cp2.returncode)
        ok("    ✓ uv sync")

    return True


def _self_replace_to_post_pip() -> None:
    """os.execv into the freshly-installed binary with --post-pip."""
    new_bin = _greatminds_bin()
    if " -m " in new_bin:
        # Fallback `python -m greatminds.cli.main` form: split into argv.
        argv = new_bin.split() + ["update", "--post-pip"]
        try:
            os.execv(argv[0], argv)
        except OSError as exc:
            warn(f"os.execv fallback (subprocess): {exc}")
            cp = subprocess.run(argv)
            raise click.exceptions.Exit(cp.returncode)
        return
    try:
        os.execv(new_bin, [new_bin, "update", "--post-pip"])
    except OSError as exc:
        warn(f"os.execv fallback (subprocess): {exc}")
        cp = subprocess.run([new_bin, "update", "--post-pip"])
        raise click.exceptions.Exit(cp.returncode)


def _step_migrate_project_config() -> None:
    """Bring the project's on-disk config to the installed version:
    canon refresh + coord.yaml driven-model migration + legacy-artifact
    removal. Without this, ``update`` bumped the package but left a stale
    coord.yaml (old all-paned window model), refreshed neither canon nor
    queues — the package/config drift the operator hit as a 'bug'."""
    from greatminds.cli.migrate import run_migration
    info("==> migrating project config to the new version...")
    run_migration(Path.cwd(), run_setup=True)


def _step_migrate_legacy_coordd() -> None:
    """If the old singleton coordd.service is still enabled, retire it."""
    from greatminds.cli.daemon import detect_legacy_coordd, _systemctl, SYSTEMD_USER_DIR, LEGACY_UNIT_NAME

    if not detect_legacy_coordd():
        return
    info("==> migrating legacy coordd.service...")
    _systemctl("stop", LEGACY_UNIT_NAME)
    _systemctl("disable", LEGACY_UNIT_NAME)
    legacy = SYSTEMD_USER_DIR / LEGACY_UNIT_NAME
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError as exc:
            warn(f"    could not remove {legacy}: {exc}")
    _systemctl("daemon-reload")
    ok("    ✓ coordd.service stopped + disabled + removed")


def _step_ensure_template_unit_installed() -> None:
    """0202: install the per-session template unit if missing.

    Migration gap: pre-0008 fleets had ``coordd.service`` (legacy
    singleton). 0008 introduced ``greatminds-daemon@<session>.service``
    (template unit). ``greatminds update`` removed the legacy unit
    but assumed the template unit was already installed — fresh
    pre-0008 fleets had it absent, so the subsequent restart step
    failed and operators had to hand-run ``greatminds daemon install``
    to recover.

    Now ``greatminds update`` detects the missing template and runs
    ``greatminds daemon install`` automatically before the restart
    step. Idempotent: already-installed unit → skip without warning.
    """
    from greatminds.cli.daemon import SYSTEMD_USER_DIR, TEMPLATE_UNIT_NAME

    template = SYSTEMD_USER_DIR / TEMPLATE_UNIT_NAME
    if template.is_file():
        return  # already installed; nothing to do

    info("==> template unit not found; running daemon install to "
         "migrate to per-session daemon model")
    new_bin = _greatminds_bin().split()
    cp = subprocess.run(new_bin + ["daemon", "install"])
    if cp.returncode != 0:
        err(
            "daemon install failed; check `systemctl --user enable "
            "greatminds-daemon@<name>.service` and re-run "
            "`greatminds update --post-pip`. Ensure systemd-user is "
            "enabled for this account (loginctl enable-linger)."
        )
        raise click.exceptions.Exit(cp.returncode)
    ok("    ✓ template unit installed")


def _step_restart_daemon(project_name: str | None) -> None:
    """Invoke `greatminds daemon restart` via the freshly-installed CLI."""
    new_bin = _greatminds_bin().split()  # may be `<py> -m greatminds.cli.main`
    cmd = new_bin + ["daemon", "restart"]
    if project_name:
        cmd += ["--project", project_name]
    info(f"==> restarting daemon...")
    cp = subprocess.run(cmd)
    if cp.returncode != 0:
        err(
            "daemon restart failed; check `systemctl --user status "
            "greatminds-daemon@<name>` and re-run `greatminds update --post-pip`."
        )
        raise click.exceptions.Exit(cp.returncode)
    ok("    ✓ daemon active")


def _tmux_session_present(session: str | None) -> bool:
    """0299: check whether the project's tmux session exists.

    Returns False when:
      - ``session`` is None (no coord.yaml or no session field).
      - ``tmux`` is not on PATH.
      - ``tmux has-session -t <session>`` returns non-zero (session
        not running).

    Caller skips the agent-restart phase entirely when this is
    False — USER may have intentionally killed the session before
    running ``greatminds update``; resurrecting it would be hostile.
    """
    if not session:
        return False
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    try:
        cp = subprocess.run(
            [tmux, "has-session", "-t", session],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return cp.returncode == 0


def _resolve_session_from_coord_yaml() -> str | None:
    """Best-effort: read ``coord.yaml`` for the session name. None
    if absent / malformed."""
    import yaml as _yaml
    cy = Path.cwd() / "coord.yaml"
    if not cy.is_file():
        return None
    try:
        doc = _yaml.safe_load(cy.read_text(encoding="utf-8")) or {}
    except (OSError, _yaml.YAMLError):
        return None
    sess = doc.get("session")
    return str(sess).strip() if isinstance(sess, str) and sess.strip() else None


def _step_restart_agents() -> None:
    """Invoke `greatminds restart` to refresh tmux agents — but
    only if the tmux session was already running before ``update``.

    0299: ``update`` MUST NOT start a tmux session that wasn't up
    when the operator invoked it. USER may have killed the session
    deliberately (debugging, paused fleet, etc.); spinning the
    agents back up would be hostile + create surprise PIDs.
    """
    session = _resolve_session_from_coord_yaml()
    if not _tmux_session_present(session):
        info(
            f"==> tmux session {session!r} absent; skipping agent "
            "restart (re-run `greatminds launch --target tmux` "
            "when you want the fleet back up)"
        )
        return

    new_bin = _greatminds_bin().split()
    cmd = new_bin + ["restart"]
    info("==> restarting tmux agents...")
    cp = subprocess.run(cmd)
    if cp.returncode != 0:
        err(
            "agent restart failed; check `tmux a -t <session>` and "
            "re-run `greatminds update --post-pip`."
        )
        raise click.exceptions.Exit(cp.returncode)
    ok("    ✓ agents up")


# ---------------------------------------------------------------------------
# Click command
# ---------------------------------------------------------------------------


@click.command(
    "update",
    short_help="upgrade greatminds (pip + daemon + agents) in one shot",
    help=__doc__,
)
@click.option("--post-pip", "post_pip", is_flag=True,
              help="skip pip; only run daemon + agent restarts "
                   "(idempotent recovery mode).")
@click.option("--check", "check", is_flag=True,
              help="report what would change; no actions.")
@click.option("--dry-run", "dry_run", is_flag=True,
              help="alias of --check.")
@click.option("--major", is_flag=True,
              help="allow major-version bump (default: refuse).")
@click.option("--project", "project_name", default=None,
              help="project name for `greatminds daemon restart` (default: "
                   "coord.yaml session).")
def update(post_pip: bool, check: bool, dry_run: bool, major: bool,
           project_name: str | None) -> None:
    is_check = check or dry_run

    if is_check:
        current = __version__
        latest = _fetch_latest_pypi_version()
        info(f"==> current: greatminds {current}")
        info(f"==> latest on PyPI: {latest}")
        if _parse_semver(latest) <= _parse_semver(current):
            ok("already up to date")
            return
        if _is_major_bump(current, latest) and not major:
            warn(f"would refuse: major bump {current} → {latest} "
                 "(re-run with --major to acknowledge).")
            return
        info(f"would upgrade: {current} → {latest}")
        return

    if not post_pip:
        # Phase 1: pip step (in the OLD binary).
        bumped = _step_pip_upgrade(major)
        if bumped:
            # Phase 2: self-replace via os.execv → continues as `--post-pip`
            # in the freshly-installed binary (which has the new migration).
            _self_replace_to_post_pip()
            return  # pragma: no cover — execv replaces the process
        # Package already current → NO self-replace needed (this binary is
        # the right version). Fall through to run the same config-migration
        # + restart phase in-process, so `update` ALWAYS reconciles the
        # project config to the installed version, not just the package.
        info("==> reconciling project config to installed version...")

    # --post-pip phase (idempotent; reached via self-replace, explicit
    # --post-pip, or the already-current fall-through above).
    # Project-config migration FIRST so the daemon + agents below start
    # on the migrated config (new coord.yaml model, refreshed canon).
    _step_migrate_project_config()
    _step_migrate_legacy_coordd()
    _step_ensure_template_unit_installed()  # 0202: fill the migration gap
    _step_restart_daemon(project_name)
    _step_restart_agents()
    ok(f"==> done: greatminds at {__version__}")


if __name__ == "__main__":
    update()
