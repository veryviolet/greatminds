"""Tiny shared utilities — ISO-8601 timestamps, prog-name, die().

These are shared so that bug-fixes propagate to every entry-point at once.
``die()`` raises :class:`greatminds.core.errors.GreatMindsError` so click's
top-level handler picks it up uniformly. Callers don't need to know about
exit codes — they pass an int, click formats the message and exits.
"""

from __future__ import annotations

import os
import sys
import time

from greatminds.core.errors import GreatMindsError

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


def die(code: int, msg: str, *, prog: str | None = None) -> "GreatMindsError":
    """Raise :class:`GreatMindsError(msg, exit_code=code)`.

    Historical callers wrote ``die(2, "bad value")`` expecting an exit-2.
    Click catches ``GreatMindsError`` (a ``ClickException``) at the top
    level, prints ``"Error: <msg>"`` to stderr, and exits with ``code``.
    The ``prog`` argument is accepted for backward compatibility but
    ignored — click owns the message format now.
    """
    del prog  # accepted for compat, click owns the prefix
    raise GreatMindsError(msg, exit_code=code)
