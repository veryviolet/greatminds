"""``greatminds project`` — read-only view of ``coordination/PROJECT.md``.

Closes a protocol gap: the contract requires every agent to read
``coordination/PROJECT.md`` each tick, but ``PROJECT.md`` lives under
``coordination/`` and the "mutations via the CLI only" rule (plus
cwd ambiguity) left agents with no sanctioned CLI surface to obtain it.
This command prints it, resolving the coordination dir regardless of
cwd. Strictly read-only — no mutation, no FSM side effects.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_config_dir, find_project_dir
from greatminds.core.schema import inspect_schema_copy, load_schema_snapshot


@click.group(name="project",
             help="read-only project documentation and effective contract.")
def project() -> None:
    pass


@project.command(name="show",
                 help="print coordination/PROJECT.md (read-only).")
def project_show() -> None:
    coord = find_config_dir()
    p = coord / "PROJECT.md"
    if not p.is_file():
        raise GreatMindsError(
            f"PROJECT.md not found at {p} — run `greatminds setup` first")
    click.echo(p.read_text(encoding="utf-8"), nl=False)


@project.command(name="schema", help="print the effective schema, not the project mirror.")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              help="project whose generated schema copy should be inspected.")
@click.option("--json", "as_json", is_flag=True,
              help="include schema identity, document, and project-copy status.")
@click.option("--check", is_flag=True,
              help="check the project mirror; exit 2 on drift, missing, or unreadable copy.")
def project_schema(project_dir: Path | None, as_json: bool, check: bool) -> None:
    snapshot = load_schema_snapshot()
    root = project_dir.resolve() if project_dir else find_project_dir(strict=False)
    mirror = inspect_schema_copy(snapshot, root)
    if as_json:
        result = {"source": str(snapshot.source), "version": snapshot.version,
                  "sha256": snapshot.sha256, "project_copy": mirror}
        if not check:
            result["schema"] = snapshot.document
        click.echo(json.dumps(result, indent=2))
    elif check:
        click.echo(f"schema: {snapshot.source}\nsha256: {snapshot.sha256}")
        click.echo(f"project copy: {mirror['status']} ({mirror['path']})")
    else:
        click.echo(snapshot.text, nl=False)
    if check and mirror["status"] != "current":
        if not as_json:
            click.echo("Inspect the difference before refreshing generated files with "
                       "`greatminds setup`. Runtime policy comes from the schema above.")
        raise click.exceptions.Exit(2)
