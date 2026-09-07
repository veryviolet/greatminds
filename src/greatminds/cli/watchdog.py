"""Read-only watchdog for the coordination filesystem.

Reports:
  - orphaned intent files older than ``schema.watchdog.intent_orphan_seconds``
  - tasks in active queues older than ``task_stale_in_active_queue_seconds``
  - tasks in review queues older than ``task_stale_in_review_queue_seconds``
  - ACP runs waiting for authentication/input, failed, or missing their process

This operator inspection never moves files, alters state, or requires an LLM turn.

Exit code: 0 for inspection findings; nonzero for project/schema errors. ``--quiet`` only prints sections
with findings.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.core.errors import GreatMindsError
from greatminds.cli._colors import err, info, ok, warn


from greatminds.domain.filesystem_health import (
    REVIEW_QUEUES, thresholds as health_thresholds, orphan_intents, stale_tasks as inspect_stale_tasks,
    orphan_worktrees as inspect_orphan_worktrees,
)


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


@click.command(
    short_help="report ACP holds, stale tasks, orphan intents and worktrees",
    help=__doc__,
)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing .greatminds/ (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data directory (default: packaged greatminds.data)")
@click.option("--quiet", is_flag=True, help="only print sections with findings")
def watchdog(project_dir: Path | None, canon_dir: Path | None, quiet: bool) -> None:
    project_dir = find_project_dir(project_dir, strict=False, use_env=project_dir is None)
    coord = project_runtime_dir(project_dir)
    if not coord.is_dir():
        err(f"error: {coord} not found")
        raise click.exceptions.Exit(1)

    try:
        schema = load_schema_snapshot(canon_dir).document
    except GreatMindsError as exc:
        raise click.ClickException(str(exc)) from exc
    thresholds = health_thresholds(schema)

    now = time.time()
    findings = 0

    # ---- Orphaned intents
    threshold = thresholds["intent_orphan_seconds"]
    intents_observed = True
    try:
        orphans = [(row['name'], row['age_seconds']) for row in orphan_intents(coord, schema, now=now)]
    except Exception:
        orphans = []
        intents_observed = False
        findings += 1
        warn("Intent observation unavailable; inspect filesystem access and watchdog policy")
    if orphans:
        findings += len(orphans)
        warn(f"ORPHANED INTENTS ({len(orphans)}, threshold {fmt_age(threshold)}):")
        for name, age in orphans:
            warn(f"  {name}: {fmt_age(age)} old")
        click.echo()
    elif intents_observed and not quiet:
        info(f"intent/: 0 orphans (threshold {fmt_age(threshold)})")

    # ACP lifecycle and process liveness are separate observations.
    from greatminds.runtime.observation import agent_rows
    from greatminds.runtime.config import load_execution_config
    from greatminds.cli.chat import terminal_text
    try:
        config = load_execution_config(project_dir / 'coordination/execution.yaml',
                                       roles=set(schema['roles']))
        blocked_runs = [row for row in agent_rows(project_dir, config=config)
                        if row['state'] in {'waiting_auth', 'waiting_input', 'failed', 'interrupted'}
                        or (row['state'] == 'running' and row['alive'] is not True)]
    except Exception:
        findings += 1
        warn("ACP run observation unavailable; inspect execution configuration and runtime state")
    else:
        findings += len(blocked_runs)
        for row in blocked_runs:
            warn(terminal_text(f"ACP {row['role']} [{row['binding_id']}]: {row['state']} alive={row['alive']} {row['reason']}"))
        if not blocked_runs and not quiet:
            info("ACP runs: no observed authentication/input/failure holds")

    # ---- Stale tasks per queue
    active_threshold = thresholds["task_stale_in_active_queue_seconds"]
    review_threshold = thresholds["task_stale_in_review_queue_seconds"]
    tasks_observed = True
    try:
        stale_tasks = [(row['queue'], row['name'], row['age_seconds'])
                       for row in inspect_stale_tasks(coord, schema, now=now)]
    except Exception:
        stale_tasks = []
        tasks_observed = False
        findings += 1
        warn("Task observation unavailable; inspect filesystem access and watchdog policy")

    if stale_tasks:
        findings += len(stale_tasks)
        warn(f"STALE TASKS ({len(stale_tasks)}):")
        for queue, name, age in stale_tasks:
            t = review_threshold if queue in REVIEW_QUEUES else active_threshold
            warn(f"  {queue}/{name}: {fmt_age(age)} old (threshold {fmt_age(t)})")
        click.echo()
    elif tasks_observed and not quiet:
        info("active queues: 0 stale tasks")

    # Worktree inspection follows effective queue kinds, including parking queues.
    orphan_worktrees = []
    worktrees_observed = True
    try:
        orphan_worktrees = inspect_orphan_worktrees(project_dir, coord, schema)
    except Exception:
        findings += 1
        worktrees_observed = False
        warn("Worktree observation unavailable; inspect worktree policy and filesystem access")

    if orphan_worktrees:
        findings += len(orphan_worktrees)
        warn(f"ORPHAN WORKTREES ({len(orphan_worktrees)}):")
        for row in orphan_worktrees:
            warn(f"  {row['path']} (no active task — "
                 f"run `greatminds worktree prune`)")
        click.echo()
    elif worktrees_observed and not quiet:
        info("worktrees: 0 orphans")

    if findings == 0 and not quiet:
        ok("\nAll clear.")


if __name__ == "__main__":
    watchdog()  # allows `python -m greatminds.cli.watchdog --help`
