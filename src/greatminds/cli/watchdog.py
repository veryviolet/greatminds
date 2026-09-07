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


DEFAULT_THRESHOLDS = {
    "intent_orphan_seconds": 300,
    "task_stale_in_active_queue_seconds": 86400,
    "task_stale_in_review_queue_seconds": 43200,
}

REVIEW_QUEUES = {"feature_review", "feature_docs_review"}


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
    thresholds = {**DEFAULT_THRESHOLDS, **(schema.get("watchdog") or {})}
    queues = schema.get("queues") or {}

    now = time.time()
    findings = 0

    # ---- Orphaned intents
    threshold = thresholds["intent_orphan_seconds"]
    intent_dir = coord / "intent"
    orphans: list[tuple[str, float]] = []
    if intent_dir.is_dir():
        for f in sorted(intent_dir.glob("*.json")):
            age = now - f.stat().st_mtime
            if age > threshold:
                orphans.append((f.name, age))
    if orphans:
        findings += len(orphans)
        warn(f"ORPHANED INTENTS ({len(orphans)}, threshold {fmt_age(threshold)}):")
        for name, age in orphans:
            warn(f"  {name}: {fmt_age(age)} old")
        click.echo()
    elif not quiet:
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
    stale_tasks: list[tuple[str, str, float]] = []

    for queue_name, queue_meta in queues.items():
        if not isinstance(queue_meta, dict):
            continue
        if queue_meta.get("kind") != "active":
            continue
        d = coord / queue_name
        if not d.is_dir():
            continue
        threshold = review_threshold if queue_name in REVIEW_QUEUES else active_threshold
        for f in sorted([*d.glob("*.yaml"), *d.glob("*.md")]):
            if f.stem == "_TEMPLATE":
                continue
            age = now - f.stat().st_mtime
            if age > threshold:
                stale_tasks.append((queue_name, f.name, age))

    if stale_tasks:
        findings += len(stale_tasks)
        warn(f"STALE TASKS ({len(stale_tasks)}):")
        for queue, name, age in stale_tasks:
            t = review_threshold if queue in REVIEW_QUEUES else active_threshold
            warn(f"  {queue}/{name}: {fmt_age(age)} old (threshold {fmt_age(t)})")
        click.echo()
    elif not quiet:
        info("active queues: 0 stale tasks")

    # ---- 0185: orphan worktree sweep
    #
    # A worktree at <base_path>/<task-id>/ whose task_id is no longer
    # in any active queue is an orphan — left behind by an aborted
    # mv, a crashed agent, or pre-cutover state. Report (don't auto-
    # prune) so the operator sees the count + can run
    # `greatminds worktree prune` deliberately.
    orphan_worktrees: list[str] = []
    worktrees_observed = True
    try:
        from greatminds.cli import worktree as wt_mod
        policy = wt_mod.load_worktree_policy(project_dir, schema_document=schema)
        base = project_dir / policy.base_path
        if base.is_dir():
            active_ids: set[str] = set()
            for q in coord.iterdir():
                if not q.is_dir() or q.name.startswith("."):
                    continue
                if q.name in ("verified", "archive", "stand_done",
                              "inbox", "intent"):
                    continue
                for f in q.iterdir():
                    if f.suffix in (".yaml", ".md"):
                        active_ids.add(f.stem)
                        if len(f.stem) > 4:
                            active_ids.add(f.stem[:4])
            for child in sorted(base.iterdir()):
                if child.is_dir() and child.name not in active_ids:
                    orphan_worktrees.append(child.name)
    except Exception:
        findings += 1
        worktrees_observed = False
        warn("Worktree observation unavailable; inspect worktree policy and filesystem access")

    if orphan_worktrees:
        findings += len(orphan_worktrees)
        warn(f"ORPHAN WORKTREES ({len(orphan_worktrees)}):")
        for name in orphan_worktrees:
            warn(f"  {base / name} (no active task — "
                 f"run `greatminds worktree prune`)")
        click.echo()
    elif worktrees_observed and not quiet:
        info("worktrees: 0 orphans")

    if findings == 0 and not quiet:
        ok("\nAll clear.")


if __name__ == "__main__":
    watchdog()  # allows `python -m greatminds.cli.watchdog --help`
