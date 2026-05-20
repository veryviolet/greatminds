"""Tiny shared utilities — error exit, UTC ISO timestamps, prog-name detection.

These are kept here (not duplicated per CLI module) so that bug-fixes propagate
to every entry-point at once. Originally they were forked in nearly every
``/opt/coordination/bin/*`` Python script with slightly different signatures
(``die(code, msg)`` vs ``die(msg, code=1)``; per-script ``prog:`` prefix). This
module normalises them.
"""

from __future__ import annotations

import os
import sys
import time

# UTC, second precision — matches the format used in every task-file
# ``opened_at``, ``timestamp`` and journal entry. Don't change without
# migrating every existing file.
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    """Current time as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, second precision)."""
    return time.strftime(ISO_FMT, time.gmtime())


def prog_name() -> str:
    """Best-effort program name for diagnostic output.

    For ``greatminds-task --help`` this returns ``greatminds-task`` (taken
    from ``sys.argv[0]`` basename). When run as a module / library it returns
    a shorter ``greatminds`` fallback.
    """
    arg0 = sys.argv[0] if sys.argv else ""
    base = os.path.basename(arg0)
    if base and base != "__main__.py":
        return base
    return "greatminds"


def die(code: int, msg: str, *, prog: str | None = None) -> None:
    """Print ``"<prog>: <msg>"`` to stderr and exit with ``code``.

    Behaves like ``sys.exit(code)`` after the message — callers that need the
    type checker to see ``die`` as ``NoReturn`` may follow with
    ``raise SystemExit`` (mypy quirk).
    """
    p = prog or prog_name()
    print(f"{p}: {msg}", file=sys.stderr)
    sys.exit(code)
