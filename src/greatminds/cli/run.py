"""Operator snapshots and harness-neutral result submission for ACP runs."""

import json
import os
import time
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


@run.command("command")
@click.argument("command_id")
@click.option("--request-id")
@click.option("--wait", "wait_seconds", default=0.0, type=click.FloatRange(min=0))
def command(command_id, request_id, wait_seconds):
    """Request a configured command from the daemon; optionally wait for its receipt."""
    from greatminds.runtime.commands import CommandService, UNRESOLVED

    run_id, token = os.environ.get("GREATMINDS_RUN_ID"), os.environ.get("GREATMINDS_RUN_TOKEN")
    if not run_id or not token:
        raise GreatMindsError("command execution requires an assigned run credential", exit_code=3)
    service = CommandService(RunStore(project_runtime_dir(find_project_dir())))
    receipt = service.request(run_id, command_id, token=token, request_id=request_id)
    deadline = time.monotonic() + wait_seconds
    while receipt["status"] in UNRESOLVED - {"needs_recovery"} and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        receipt = service.get(receipt["id"])
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))


@run.command("command-status")
@click.argument("request_id")
def command_status(request_id):
    """Inspect an executed command and its output artifact paths."""
    from greatminds.runtime.commands import CommandService
    service = CommandService(RunStore(project_runtime_dir(find_project_dir())))
    click.echo(json.dumps(service.get(request_id), ensure_ascii=False, indent=2))


@run.command("command-resolve")
@click.argument("request_id")
@click.option("--reason", required=True)
def command_resolve(request_id, reason):
    """Acknowledge an uncertain command after inspection; grant no passing evidence."""
    from greatminds.runtime.commands import CommandService
    if os.environ.get("GREATMINDS_RUN_ID"):
        raise GreatMindsError("uncertain command resolution requires the operator", exit_code=3)
    service = CommandService(RunStore(project_runtime_dir(find_project_dir())))
    click.echo(json.dumps(service.resolve(request_id, reason=reason), ensure_ascii=False, indent=2))


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
