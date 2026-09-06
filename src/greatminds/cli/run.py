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
from greatminds.runtime.store import RunStore


@click.group()
def run():
    """Inspect and control daemon-owned ACP execution."""


@run.command("status")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
def status(project_dir):
    """Print the versioned run snapshot and assignment reasons as JSON."""
    from greatminds.runtime.observation import snapshot
    project = project_dir.resolve() if project_dir else find_project_dir()
    click.echo(json.dumps(snapshot(project), ensure_ascii=False, indent=2))


@run.command("events")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--after", type=click.IntRange(min=0), default=0, help="Exclusive event sequence cursor.")
@click.option("--limit", type=click.IntRange(min=1, max=10000), default=200)
@click.option("--follow", is_flag=True, help="Continue emitting new events as JSON Lines.")
@click.option("--interval", type=click.FloatRange(min=0.2), default=1.0)
def events(project_dir, after, limit, follow, interval):
    """Read the durable ACP event stream without launching an executor."""
    project = project_dir.resolve() if project_dir else find_project_dir()
    store = RunStore(project_runtime_dir(project))
    try:
        while True:
            batch = [event for event in store.snapshot()["events"] if event["sequence"] > after][:limit]
            for event in batch:
                click.echo(json.dumps(event, ensure_ascii=True, sort_keys=True))
                after = event["sequence"]
            if not follow:
                return
            if len(batch) < limit:
                time.sleep(interval)
    except KeyboardInterrupt:
        return


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
@click.option("--output-limit", default=8192, type=click.IntRange(min=0, max=65536))
def command(command_id, request_id, wait_seconds, output_limit):
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
    receipt["output_preview"] = service.output_preview(receipt["id"], limit=output_limit)
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))


@run.command("command-status")
@click.argument("request_id")
@click.option("--output-limit", default=8192, type=click.IntRange(min=0, max=65536))
def command_status(request_id, output_limit):
    """Inspect an executed command and its output artifact paths."""
    from greatminds.runtime.commands import CommandService
    service = CommandService(RunStore(project_runtime_dir(find_project_dir())))
    receipt = service.get(request_id)
    run_id, token = os.environ.get("GREATMINDS_RUN_ID"), os.environ.get("GREATMINDS_RUN_TOKEN")
    if run_id or token:
        if not run_id or not token:
            raise GreatMindsError("command inspection requires the assigned run credential", exit_code=3)
        service.store.authorize(run_id, token)
        if receipt["run_id"] != run_id:
            raise GreatMindsError("command belongs to another run", exit_code=3)
    receipt["output_preview"] = service.output_preview(request_id, limit=output_limit)
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))


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


@run.command("repair")
@click.option("--operation", "operation_id", required=True)
@click.option("--abandon", is_flag=True, help="cancel an uncommitted intent after inspection")
@click.option("--reason", help="required when abandoning an intent")
def repair(operation_id, abandon, reason):
    """Request bounded reconciliation of an incomplete system operation."""
    from greatminds.domain.maintenance import MaintenanceService
    if os.environ.get("GREATMINDS_RUN_ID"):
        raise GreatMindsError("system operation repair requires the operator", exit_code=3)
    service = MaintenanceService(RunStore(project_runtime_dir(find_project_dir())), load_schema_snapshot())
    result = service.abandon(operation_id, reason=reason) if abandon else service.request_repair(operation_id)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@run.command("submit")
@click.option("--file", "source",
              type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "inline_json", help="Submit a JSON decision directly without creating a file")
def submit(source, inline_json):
    """Receive a typed result using the assigned run's environment credential."""
    run_id, token = os.environ.get("GREATMINDS_RUN_ID"), os.environ.get("GREATMINDS_RUN_TOKEN")
    if not run_id or not token:
        raise GreatMindsError("result submission requires an assigned run credential", exit_code=3)
    if (source is None) == (inline_json is None):
        raise GreatMindsError("provide exactly one of --json or --file", exit_code=2)
    if inline_json is not None and len(inline_json.encode()) > 65536:
        raise GreatMindsError("inline result exceeds 64 KiB; use --file", exit_code=2)
    try:
        document = json.loads(inline_json if inline_json is not None else source.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise GreatMindsError(f"invalid result envelope: {exc}", exit_code=2) from exc
    receipt = RunStore(project_runtime_dir(find_project_dir())).submit_decision(document, run_id=run_id, token=token)
    click.echo(json.dumps(receipt, ensure_ascii=False, indent=2))


@run.command("permission")
@click.argument("request_id")
@click.option("--option", "option_id", help="Choose an option ID from this exact live request")
def permission(request_id, option_id):
    """Inspect a permission request, or answer it for the live ACP callback."""
    from greatminds.runtime.permissions import PermissionService
    if option_id is not None and os.environ.get("GREATMINDS_RUN_ID"):
        raise GreatMindsError("permission decisions require the operator", exit_code=3)
    service = PermissionService(RunStore(project_runtime_dir(find_project_dir())))
    result = service.get(request_id) if option_id is None else service.answer(request_id, option_id)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@run.command("contract")
@click.option("--schema", "full_schema", is_flag=True, help="Show this run's pinned schema instead of its assigned context")
def contract(full_schema):
    """Read the assigned context or frozen schema using the run credential."""
    from greatminds.core.schema import SchemaSnapshot
    from greatminds.runtime.context import context_document
    from greatminds.runtime.store import Claim
    run_id, token = os.environ.get("GREATMINDS_RUN_ID"), os.environ.get("GREATMINDS_RUN_TOKEN")
    if not run_id or not token:
        raise GreatMindsError("contract inspection requires an assigned run credential", exit_code=3)
    store = RunStore(project_runtime_dir(find_project_dir()))
    assigned = store.authorize(run_id, token)
    frozen = store.contracts(run_id)["schema"]
    schema = SchemaSnapshot(store.directory / "contracts" / f"schema-{assigned['schema_sha256']}.json",
                            frozen["text"], assigned["schema_sha256"])
    result = schema.document if full_schema else context_document(store, Claim(assigned, token), schema)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
