"""Inspect dependency and task-readiness gates used by the daemon.

Reports ready tasks, missing or impossible dependencies, cycles, live-role holds
and pending operations through the shared maintenance service. Inspection never
moves tasks or starts agents. The daemon applies authorized system resumes.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from greatminds.cli._colors import info
from greatminds.cli.chat import terminal_text
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.maintenance import MaintenanceService
from greatminds.runtime.store import RunStore


@click.command(short_help="report dependency, cycle and task-readiness findings", help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing .greatminds/ (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
@click.option("--quiet", is_flag=True, help="suppress no-findings output")
@click.option("--json", "as_json", is_flag=True, help="versioned dependency/controller findings")
def wake_check(project_dir: Path | None, canon_dir: Path | None, quiet: bool, as_json: bool = False) -> None:
    project = find_project_dir(project_dir, strict=False, use_env=project_dir is None)
    runtime = project_runtime_dir(project)
    if not runtime.is_dir():
        raise click.ClickException(f"runtime directory not found: {runtime}")
    try:
        report = MaintenanceService(RunStore(runtime), load_schema_snapshot(canon_dir)).inspect()
    except GreatMindsError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for item in report["tasks"].values():
            click.echo(terminal_text(f"{item['task_id']}: {item['status']} → {item['resume_to']}"))
            for reason in item["reasons"]:
                click.echo("  " + json.dumps(reason, ensure_ascii=True))
        if not report["tasks"] and not quiet:
            info("no blocked tasks")
