"""Upgrade Greatminds and refresh the current ACP project.

Installed services are refreshed with try-restart, which preserves inactive
services. Updating never installs a service or starts a native agent frontend.
Use --post-pip to repeat project refresh without installing the package.
"""
from __future__ import annotations

import json
import os
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


def _greatminds_argv() -> list[str]:
    """Use the current environment, preserving interpreter symlinks and spaces."""
    return [sys.executable, "-m", "greatminds.cli.main"]


def _installed_version_fresh() -> str | None:
    """The ACTUALLY-installed greatminds version, read in a FRESH
    subprocess of the venv interpreter.

    In-process ``importlib.metadata`` (hence ``greatminds.__version__``)
    is resolved at import time and goes STALE right after a ``uv sync`` /
    pip upgrade in the same process — the running code is still the old
    module, and even the dist-info read can be cached. A fresh subprocess
    reads the new ``.dist-info``. Returns None on failure."""
    cp = subprocess.run(
        [sys.executable, "-c",
         "import importlib.metadata as m; print(m.version('greatminds'))"],
        capture_output=True, text=True,
    )
    v = (cp.stdout or "").strip()
    return v or None


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
    current (caller still runs the project refresh phase
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

    # 0299: uv needs a second pass to actually pull the new wheel
    # into the venv after the lock refresh. Other env managers do
    # this implicitly in their `update` command.
    if setup.env_type == "uv":
        info("==> uv sync")
        cp2 = subprocess.run(["uv", "sync"], capture_output=True, text=True)
        if cp2.returncode != 0:
            err("uv sync failed after lock refresh:")
            if cp2.stderr:
                click.echo(cp2.stderr, nl=False, err=True)
            raise click.exceptions.Exit(cp2.returncode)

    # VERIFY the upgrade ACTUALLY landed — never print a fake "✓". The
    # upgrade subprocess returning 0 is not proof the venv changed (a
    # running daemon holding the venv, a foreign activated env, or a uv
    # pass that didn't sync the active env can leave the OLD version in
    # place while the command exits 0). Read the real installed version
    # in a fresh subprocess; for uv, force one reinstall pass if it
    # lagged, then fail loudly rather than silently needing a 2nd run.
    got = _installed_version_fresh()
    if got != latest and setup.env_type == "uv":
        warn(f"    installed still {got} after sync — forcing reinstall")
        subprocess.run(["uv", "sync", "--reinstall-package", "greatminds"],
                       capture_output=True, text=True)
        got = _installed_version_fresh()
    if got != latest:
        err(f"upgrade did not take effect: installed={got!r}, expected "
            f"{latest!r}. Re-run, or check for a process holding the venv "
            f"(a running daemon) or a foreign activated virtualenv "
            f"(deactivate it, then re-run).")
        raise click.exceptions.Exit(1)
    ok(f"    ✓ installed greatminds {got} (verified)")
    return True


def _self_replace_to_post_pip(project_name: str | None = None) -> None:
    argv = _greatminds_argv() + ["update", "--post-pip"]
    if project_name:
        argv += ["--project", project_name]
    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        warn(f"os.execv fallback (subprocess): {exc}")
        cp = subprocess.run(argv)
        raise click.exceptions.Exit(cp.returncode)


def _refresh_project(project_name: str | None) -> None:
    from greatminds.cli import daemon
    from greatminds.core.paths import find_project_dir
    from greatminds.runtime.bootstrap import bootstrap
    if project_name:
        daemon._validate_project_name(project_name)
        root = daemon.lookup_project_dir(project_name)
        if root is None:
            raise click.ClickException("project is not registered")
        root = root.resolve()
        names = [project_name]
    else:
        root = find_project_dir(Path.cwd(), use_env=False)
        names = sorted(name for name, path in daemon.load_registry().items()
                       if Path(path).resolve() == root)
    info("==> refreshing ACP project state...")
    bootstrap(root)
    template = daemon.SYSTEMD_USER_DIR / daemon.TEMPLATE_UNIT_NAME
    for name in names:
        dropin = daemon._project_dropin_dir(name) / "10-project-env.conf"
        if not template.is_file() or not dropin.is_file():
            info(f"==> service {name} is not installed; skipping service refresh")
            continue
        daemon._refresh_units_before_restart(name, root)
        daemon._checked_systemctl("try-restart", daemon._instance_unit(name))
        ok(f"service {name} refreshed; inactive state preserved")
    if not names:
        info("==> no registered service; restart a foreground daemon manually if needed")


@click.command(
    "update",
    short_help="upgrade package and refresh ACP project",
    help=__doc__,
)
@click.option("--post-pip", "post_pip", is_flag=True,
              help="skip package installation; refresh project and installed services.")
@click.option("--check", "check", is_flag=True,
              help="report what would change; no actions.")
@click.option("--dry-run", "dry_run", is_flag=True,
              help="alias of --check.")
@click.option("--major", is_flag=True,
              help="allow major-version bump (default: refuse).")
@click.option("--project", "project_name", default=None,
              help="registered project name (default: current project).")
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
            # in the freshly-installed binary (which has the updated project logic).
            _self_replace_to_post_pip(project_name)
            return  # pragma: no cover — execv replaces the process
        # Package already current → NO self-replace needed (this binary is
        # the right version). Fall through to run the same project refresh
        # + restart phase in-process, so `update` ALWAYS reconciles the
        # project config to the installed version, not just the package.
        info("==> reconciling project config to installed version...")

    _refresh_project(project_name)
    # Fresh read — in-process __version__ is stale right after a same-run
    # upgrade (it reflects the OLD module the process imported at start).
    ok(f"==> done: greatminds at {_installed_version_fresh() or __version__}")


if __name__ == "__main__":
    update()
