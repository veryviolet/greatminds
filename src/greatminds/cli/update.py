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


def _step_pip_upgrade(major: bool) -> str:
    """Run pip upgrade; return the just-installed version string."""
    current = __version__
    info(f"==> current: greatminds {current}")
    latest = _fetch_latest_pypi_version()
    info(f"==> latest on PyPI: {latest}")

    if _parse_semver(latest) <= _parse_semver(current):
        ok("already up to date")
        raise click.exceptions.Exit(0)

    if _is_major_bump(current, latest) and not major:
        err(
            f"major upgrade {current.split('.')[0]}.x → "
            f"{latest.split('.')[0]}.0 may require config migration; "
            "re-run with --major to acknowledge."
        )
        raise click.exceptions.Exit(2)

    info(f"==> upgrading package... {current} → {latest}")
    pip_cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "greatminds"]
    cp = subprocess.run(pip_cmd, capture_output=True, text=True)
    if cp.returncode != 0:
        err("pip install failed:")
        if cp.stderr:
            click.echo(cp.stderr, nl=False, err=True)
        raise click.exceptions.Exit(cp.returncode)
    ok(f"    ✓ pip install --upgrade greatminds ({current} → {latest})")
    return latest


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


def _step_restart_agents() -> None:
    """Invoke `greatminds restart` to refresh tmux agents."""
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
        new_version = _step_pip_upgrade(major)
        # Phase 2: self-replace via os.execv → continues as `--post-pip`.
        _self_replace_to_post_pip()
        # If execv ever returns, something went very wrong.
        return  # pragma: no cover

    # --post-pip phase (idempotent; called by self-replace or by user).
    _step_migrate_legacy_coordd()
    _step_restart_daemon(project_name)
    _step_restart_agents()
    ok(f"==> done: greatminds at {__version__}")


if __name__ == "__main__":
    update()
