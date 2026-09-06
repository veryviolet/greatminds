"""Operator snapshots and harness-neutral result submission for ACP runs."""

import json
import os
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.store import ResultEnvelope, RunStore


@click.group()
def run():
    """Inspect and control daemon-owned ACP execution."""


@run.command("status")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
def status(project_dir):
    """Print the versioned run snapshot and assignment reasons as JSON."""
    from greatminds.runtime.daemon import assignments

    project = project_dir.resolve() if project_dir else find_project_dir()
    store = RunStore(project_runtime_dir(project))
    snapshot = store.snapshot()
    source = project / "coordination" / "execution.yaml"
    snapshot["assignments"] = []
    if source.is_file():
        schema = load_schema_snapshot()
        config = load_execution_config(source, roles=set(schema.document["roles"]))
        snapshot["assignments"] = [{"task_id": task.task_id, "binding": binding.id,
                                    "role": binding.role, "reason": reason}
                                   for binding, task, reason in assignments(store, config, schema)]
    click.echo(json.dumps(snapshot, ensure_ascii=False, indent=2))


@run.command("pause")
def pause():
    """Pause new ACP dispatch; existing runs can finish."""
    RunStore(project_runtime_dir(find_project_dir())).set_paused(True)
    click.echo("ACP dispatch paused")


@run.command("resume")
def resume():
    """Resume ACP dispatch."""
    RunStore(project_runtime_dir(find_project_dir())).set_paused(False)
    click.echo("ACP dispatch resumed")


@run.command("cancel")
@click.argument("run_id")
def cancel(run_id):
    """Request bounded cancellation by the ACP daemon."""
    control = RunStore(project_runtime_dir(find_project_dir())).request_control(run_id, "cancel")
    click.echo(json.dumps(control))


@run.command("retry")
@click.argument("run_id")
def retry(run_id):
    """Authorize another attempt after a terminal or waiting run."""
    control = RunStore(project_runtime_dir(find_project_dir())).request_control(run_id, "retry")
    click.echo(json.dumps(control))


@run.command("submit")
@click.option("--file", "source", required=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
def submit(source):
    """Receive a typed result using the assigned run's environment credential."""
    run_id, token = os.environ.get("GREATMINDS_RUN_ID"), os.environ.get("GREATMINDS_RUN_TOKEN")
    if not run_id or not token:
        raise GreatMindsError("result submission requires an assigned run credential", exit_code=3)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        envelope = ResultEnvelope(**document)
    except (OSError, TypeError, ValueError) as exc:
        raise GreatMindsError(f"invalid result envelope: {exc}", exit_code=2) from exc
    if envelope.run_id != run_id:
        raise GreatMindsError("result run does not match assigned run", exit_code=3)
    receipt = RunStore(project_runtime_dir(find_project_dir())).receive_result(envelope, token=token)
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))
