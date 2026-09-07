"""Project documentation, effective contracts and explicit execution presets."""
from __future__ import annotations

import json
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_config_dir, find_project_dir
from greatminds.core.schema import inspect_schema_copy, load_schema_snapshot
from greatminds.runtime.config import load_execution_config


@click.group(name="project",
             help="project documentation, effective contracts and execution presets.")
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


@project.command(name="execution", help="validate and show the ACP execution contract (read-only).")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path),
              help="defaults to coordination/execution.yaml in the current project.")
def project_execution(config_path: Path | None) -> None:
    from dataclasses import asdict

    source = config_path or find_config_dir() / "execution.yaml"
    schema = load_schema_snapshot()
    config = load_execution_config(source, roles=set(schema.document.get("roles", {})))
    click.echo(json.dumps({"version": 1, "source": str(source.resolve()),
                           "sha256": config.sha256, "schema_sha256": schema.sha256,
                           "execution": asdict(config)}, indent=2))


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
            click.echo("Inspect the difference against `greatminds project schema` before "
                       "replacing the mirror. Setup preserves existing mirrors. "
                       "Runtime policy comes from the schema above.")
        raise click.exceptions.Exit(2)


@project.command(name="presets", help="list available ACP role rosters without changing the project.")
def project_presets():
    from greatminds.runtime.presets import catalog
    click.echo(json.dumps({"version": 1, "presets": catalog()}, indent=2))


@project.command(name="preset", help="preview or explicitly apply a role roster to an existing ACP manifest.")
@click.argument("name", type=click.Choice(["local", "ui", "docs", "deployed", "full"]))
@click.option("--agent", required=True, help="Existing agent manifest ID to use for the selected roles.")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--apply", is_flag=True, help="Atomically write the previewed bindings; preserve existing custom bindings.")
def project_preset(name, agent, project_dir, apply):
    from greatminds.runtime.presets import configure_preset
    root = project_dir.resolve() if project_dir else find_project_dir()
    click.echo(json.dumps(configure_preset(root, name, agent, apply=apply), indent=2))
