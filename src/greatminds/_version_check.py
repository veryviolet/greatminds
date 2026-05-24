"""Version-drift detection helper.

Producer side: ``greatminds coordd`` writes ``<project>/coordination/.daemon_version``
with ``greatminds.__version__`` on startup (added in task 0008).

Consumer side (this module): on every greatminds CLI invocation,
compare the running CLI's version to the daemon-recorded version. On
mismatch, warn the USER to finish the upgrade with
``greatminds update --post-pip``. Agents (``$GREATMINDS_ROLE`` set)
get silent treatment on stdout/stderr but DO leave a paper trail in
``coordination/journal.ndjson`` (kind=warn) — they must not auto-restart
themselves, but the operator needs visibility into stale-version
agents during post-mortems.

Placed under ``src/greatminds/_version_check.py`` (NOT under
``cli/``) to keep the drift wiring importable from ``cli/main.py``
without circular-import games against the Click root group.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import __version__


DAEMON_VERSION_FILENAME = ".daemon_version"
JOURNAL_FILENAME = "journal.ndjson"


def _read_daemon_version(project_dir: Path) -> str | None:
    """Return the version line written by the daemon, or None if missing/unreadable."""
    p = project_dir / "coordination" / DAEMON_VERSION_FILENAME
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line or None


def check_drift(project_dir: Path) -> tuple[str, str | None] | None:
    """Compare CLI version to daemon-recorded version.

    Returns ``(cli_version, daemon_version_or_None)`` if a mismatch is
    detected, else ``None``. ``daemon_version`` is ``None`` when the
    daemon never wrote ``.daemon_version`` (e.g. fresh install before
    first daemon start).
    """
    daemon_version = _read_daemon_version(project_dir)
    if daemon_version is None:
        # We can't say "drift" without a recorded baseline, but the
        # absence itself is worth surfacing once the user is interactive.
        return (__version__, None)
    if daemon_version == __version__:
        return None
    return (__version__, daemon_version)


def _is_agent() -> bool:
    """True iff invoked from inside an agent (GREATMINDS_ROLE env set)."""
    return bool(os.environ.get("GREATMINDS_ROLE"))


def _log_drift_to_journal(
    project_dir: Path,
    cli_version: str,
    daemon_version: str | None,
) -> None:
    """Append a one-line kind=warn record to ``coordination/journal.ndjson``.

    Used on the agent path (``$GREATMINDS_ROLE`` set) to leave a paper
    trail for stale-version agents without polluting stdout/stderr.
    All errors are swallowed — logging must never block.
    """
    try:
        journal = project_dir / "coordination" / JOURNAL_FILENAME
        if not journal.parent.is_dir():
            return  # not a greatminds project; don't create coordination/
        record = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": os.environ.get("GREATMINDS_ROLE", "UNKNOWN"),
            "kind": "warn",
            "reason": "version_drift",
            "cli_version": cli_version,
            "daemon_version": daemon_version,
            "hint": "greatminds update --post-pip",
        }
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 — logging must never block
        pass


def emit_drift_warning(
    cli_version: str,
    daemon_version: str | None,
    *,
    file=None,
    project_dir: Path | None = None,
) -> None:
    """Emit the drift advisory. Never blocks.

    Routing:
      - **Agent path** (``$GREATMINDS_ROLE`` set): silent on stdout/stderr
        but append one ``kind=warn`` line to
        ``<project_dir>/coordination/journal.ndjson`` for the operator's
        paper trail.
      - **USER path** (``$GREATMINDS_ROLE`` unset): print a WARNING to
        ``file`` (defaults to current ``sys.stderr``; lazy resolve so
        pytest's capsys works).

    The ``project_dir`` argument is required to locate journal.ndjson on
    the agent path. It defaults to ``Path.cwd()``. ``file`` is the
    stderr-equivalent for the USER path; ignored for agents.
    """
    if _is_agent():
        # Silent on tty; log to journal for paper trail.
        pd = project_dir if project_dir is not None else Path.cwd()
        _log_drift_to_journal(pd, cli_version, daemon_version)
        return
    stream = file if file is not None else sys.stderr
    if daemon_version is None:
        print(
            "WARNING: greatminds daemon version unknown — run "
            "`greatminds update --post-pip` if you just installed/upgraded.",
            file=stream,
        )
        return
    print(
        f"WARNING: greatminds CLI is version {cli_version} but daemon "
        f"(and possibly agents) are still on {daemon_version}. "
        "Finish the upgrade with:",
        file=stream,
    )
    print("    greatminds update --post-pip", file=stream)


def _is_greatminds_project(project_dir: Path) -> bool:
    """A directory is a greatminds project iff it has a ``coordination/`` dir.

    Used to scope drift warnings: running greatminds from a non-project
    cwd (e.g. ``$HOME``) should not nag about a missing ``.daemon_version``,
    since the user isn't in a project context at all.
    """
    return (project_dir / "coordination").is_dir()


def maybe_warn(project_dir: Path | None = None) -> None:
    """Convenience: run drift check + emit warning in one call.

    Defaults ``project_dir`` to cwd. Always safe to call — never raises,
    never blocks the parent command. Skips silently if ``project_dir``
    is not a greatminds project (no ``coordination/`` dir).
    """
    try:
        pd = project_dir or Path.cwd()
        if not _is_greatminds_project(pd):
            return  # not a greatminds project — no drift signal applicable
        result = check_drift(pd)
        if result is None:
            return
        cli_version, daemon_version = result
        emit_drift_warning(cli_version, daemon_version, project_dir=pd)
    except Exception:  # noqa: BLE001 — drift check must never crash the parent CLI
        pass
