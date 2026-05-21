"""Single shared exception type for the greatminds CLI surface.

Every place that wants to fail with a user-visible message raises
:class:`GreatMindsError`. Click catches it at the top level, prints
``"Error: <message>"`` to stderr, and exits with ``exit_code``.

Exit code convention (matches the legacy ``die(code, msg)`` pattern):

  1  usage / generic
  2  validation failure
  3  permission denied
  4  fs / atomicity failure (intent / mv / journal half-state)
"""

from __future__ import annotations

import click


class GreatMindsError(click.ClickException):
    """User-facing error raised anywhere inside the greatminds CLI.

    Callers pass the exit code at the raise site so the intent is local:

        raise GreatMindsError("bad value", exit_code=2)

    Click owns the message format (``Error: <msg>``) and the actual
    process exit.
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code
