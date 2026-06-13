"""Read-only watchdog for the coordination filesystem.

Reports:
  - orphaned intent files older than ``schema.watchdog.intent_orphan_seconds``
  - tasks in active queues older than ``task_stale_in_active_queue_seconds``
  - tasks in review queues older than ``task_stale_in_review_queue_seconds``
  - registry entries whose ``pid`` is no longer alive

ARCHITECT-REVIEWER is expected to run this at the start of every tick and
follow up on flagged items. The watchdog never moves files or alters state.

Exit code: 0 always (informational tool). ``--quiet`` only prints sections
with findings.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


# heartbeat is NOT a watchdog concern. It is an in-flight-turn hang
# signal owned by coordd (escalates to MAINTAINER while a run-lock is
# held); the watchdog never scans heartbeat age. Persistent-process
# liveness here is the dead-pid registry scan below.
DEFAULT_THRESHOLDS = {
    "intent_orphan_seconds": 300,
    "task_stale_in_active_queue_seconds": 86400,
    "task_stale_in_review_queue_seconds": 43200,
}

REVIEW_QUEUES = {"feature_review", "feature_docs_review"}


def load_schema(canon_dir: Path) -> dict:
    schema_path = canon_dir / "schema.yaml"
    if not schema_path.exists():
        return {}
    try:
        return yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _read_json_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


@click.command(
    short_help="report orphan intents / dead pids / stale tasks / orphan worktrees",
    help=__doc__,
)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing coordination/ (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data directory (default: packaged greatminds.data)")
@click.option("--quiet", is_flag=True, help="only print sections with findings")
def watchdog(project_dir: Path | None, canon_dir: Path | None, quiet: bool) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    if project_dir.name == "coordination" and (project_dir / "stand_requests").is_dir():
        coord = project_dir
    else:
        coord = project_dir / "coordination"
    if not coord.is_dir():
        err(f"error: {coord} not found")
        raise click.exceptions.Exit(1)

    schema = load_schema(canon_dir)
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

    # ---- Dead agent pids (registry says alive, /proc disagrees)
    registry_dir = coord / ".agent_registry"
    dead_pids: list[tuple[str, int, str]] = []
    if registry_dir.is_dir():
        for f in sorted(registry_dir.glob("*.json")):
            try:
                reg = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pid = reg.get("pid")
            try:
                pid_int = int(pid) if pid is not None else None
            except (TypeError, ValueError):
                continue
            if pid_int is None:
                continue
            try:
                os.kill(pid_int, 0)
            except (ProcessLookupError, PermissionError):
                dead_pids.append((f.stem, pid_int, str(reg.get("started_at", "?"))))
    if dead_pids:
        findings += len(dead_pids)
        warn(f"DEAD AGENT PIDS ({len(dead_pids)}):")
        for role, pid, started in dead_pids:
            warn(f"  {role}: pid={pid} dead (started_at: {started})")
        click.echo()
    elif not quiet:
        info("agent pids: all alive")

    # ---- 0387: wedged agents (alive pid but stuck at a pre-agent /
    # auth prompt — codex sign-in / "Login timed out" / folder-trust).
    # These pass the dead-pid scan (pid alive, input_sock present) yet
    # silently ignore every queued wake, so a user_feedback task can
    # sit untriaged forever while status reads "alive". Surface them in
    # the operator's recovery sweep so the auth wedge is unambiguous.
    wedged: list[tuple[str, str]] = []
    if registry_dir.is_dir():
        from greatminds.cli.agent import (
            collect_agent_status, _PANE_WEDGE_STATES,
        )
        for f in sorted(registry_dir.glob("*.json")):
            try:
                rec = collect_agent_status(coord, f.stem)
            except Exception:
                continue
            if rec.get("alive") and rec.get("pane_state") in _PANE_WEDGE_STATES:
                wedged.append((rec["role"], rec["pane_state"]))
    if wedged:
        findings += len(wedged)
        warn(f"WEDGED AGENTS ({len(wedged)}):")
        for role, state in wedged:
            warn(f"  {role}: alive but {state} — pane at pre-agent prompt "
                 f"(reauth / restart needed; queued wakes are ignored)")
        click.echo()
    elif not quiet:
        info("agent panes: 0 wedged")

    # ---- Stuck driven turns
    #
    # coordd owns active hang escalation, but watchdog is the operator's
    # read-only sweep. A stale driven lock with pending marker / no fresh log
    # must be visible here too, otherwise the dashboard can say "running" and
    # the external audit says "all clear" while a queue is actually blocked.
    hang_threshold = ((schema.get("heartbeat") or {})
                      .get("hang_threshold_seconds") or 300)
    try:
        hang_threshold = float(hang_threshold)
    except (TypeError, ValueError):
        hang_threshold = 300.0
    stuck_turns: list[tuple[str, float, dict, bool, bool]] = []
    locks_dir = coord / ".locks"
    if locks_dir.is_dir():
        for f in sorted(locks_dir.glob("driven-*.lock")):
            age = now - f.stat().st_mtime
            if age <= hang_threshold:
                continue
            role = f.name[len("driven-"):-len(".lock")]
            meta = _read_json_file(f)
            pending = (locks_dir / f"driven-{role}.pending").is_file()
            log_ok = False
            log_path = meta.get("log_path")
            if isinstance(log_path, str) and log_path:
                try:
                    log_ok = Path(log_path).stat().st_mtime >= f.stat().st_mtime
                except OSError:
                    log_ok = False
            stuck_turns.append((role, age, meta, pending, log_ok))

    if stuck_turns:
        findings += len(stuck_turns)
        warn(f"STUCK DRIVEN TURNS ({len(stuck_turns)}, "
             f"threshold {fmt_age(hang_threshold)}):")
        for role, age, meta, pending, log_ok in stuck_turns:
            driver = meta.get("driver") or "unknown-driver"
            log_path = meta.get("log_path")
            log_name = Path(log_path).name if isinstance(log_path, str) and log_path else "no-log"
            bits = [
                f"{role}: lock {fmt_age(age)} old",
                f"driver={driver}",
                f"log={log_name}",
            ]
            if pending:
                bits.append("pending")
            if not log_ok:
                bits.append("no fresh turn log")
            warn("  " + " · ".join(bits))
        click.echo()
    elif not quiet:
        info("driven turns: 0 stuck")

    # ---- Driven retry/backoff state
    #
    # Failed driven turns are retried by coordd after backoff. The scheduler's
    # in-memory state is mirrored to .locks so watchdog can explain "nothing is
    # running" as either a planned retry wait or a stopped/escalated failure.
    retry_rows: list[tuple[str, dict]] = []
    if locks_dir.is_dir():
        for f in sorted(locks_dir.glob("driven-*.retry.json")):
            role = f.name[len("driven-"):-len(".retry.json")]
            meta = _read_json_file(f)
            if meta:
                retry_rows.append((role, meta))

    if retry_rows:
        findings += len(retry_rows)
        warn(f"DRIVEN RETRIES ({len(retry_rows)}):")
        for role, meta in retry_rows:
            klass = meta.get("klass") or "error"
            attempts = meta.get("attempts") or 0
            bits = [f"{role}: {klass} attempt {attempts}"]
            if meta.get("escalated"):
                bits.append("auto-retry stopped")
            else:
                try:
                    remaining = float(meta.get("next_at_epoch") or 0.0) - now
                except (TypeError, ValueError):
                    remaining = 0.0
                bits.append("due now" if remaining <= 0
                            else f"next in {fmt_age(remaining)}")
            detail = meta.get("detail")
            if isinstance(detail, str) and detail.strip():
                bits.append(detail.strip()[:120])
            warn("  " + " · ".join(bits))
        click.echo()
    elif not quiet:
        info("driven retries: 0")

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
        for f in sorted(d.glob("*.md")):
            if f.name == "_TEMPLATE.md":
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
    try:
        from greatminds.cli import worktree as wt_mod
        policy = wt_mod.load_worktree_policy()
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
        pass

    if orphan_worktrees:
        findings += len(orphan_worktrees)
        warn(f"ORPHAN WORKTREES ({len(orphan_worktrees)}):")
        for name in orphan_worktrees:
            warn(f"  .worktrees/{name} (no active task — "
                 f"run `greatminds worktree prune`)")
        click.echo()
    elif not quiet:
        info("worktrees: 0 orphans")

    if findings == 0 and not quiet:
        ok("\nAll clear.")


if __name__ == "__main__":
    watchdog()  # allows `python -m greatminds.cli.watchdog --help`
