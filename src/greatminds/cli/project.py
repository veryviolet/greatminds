"""``greatminds project`` — read-only view of ``coordination/PROJECT.md``.

Closes a protocol gap: the contract requires every agent to read
``coordination/PROJECT.md`` each tick, but ``PROJECT.md`` lives under
``coordination/`` and the "mutations via the CLI only" rule (plus
cwd ambiguity) left agents with no sanctioned CLI surface to obtain it.
This command prints it, resolving the coordination dir regardless of
cwd. Strictly read-only — no mutation, no FSM side effects.
"""
from __future__ import annotations

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir


@click.group(name="project",
             help="read-only view of project docs (PROJECT.md).")
def project() -> None:
    pass


@project.command(name="show",
                 help="print coordination/PROJECT.md (read-only).")
def project_show() -> None:
    coord = find_coord_dir()
    p = coord / "PROJECT.md"
    if not p.is_file():
        raise GreatMindsError(
            f"PROJECT.md not found at {p} — run `greatminds setup` first")
    click.echo(p.read_text(encoding="utf-8"), nl=False)
