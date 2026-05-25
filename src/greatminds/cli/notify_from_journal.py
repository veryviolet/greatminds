#!/usr/bin/env python3
"""Replay recent journal.ndjson lines and write inbox wake-up messages.

Called from a PostToolUse hook (claude/cursor). It is idempotent — a state
file tracks the last processed offset so duplicate invocations are safe.

For each new journal entry, looks up which role should be woken via
schema.yaml (the role whose `claims_from` contains the destination queue
gets a wake message). Special-cases:

- to=stand_done   → notify TESTER for each task in evidence_for.
- to=verified     → notify ARCHITECT-REVIEWER (so it runs wake_check).

Usage:
    notify_from_journal [--project-dir <dir>] [--canon-dir <dir>] [--once]

By default scans only new lines since the last invocation. `--once`
processes every line (useful for backfill).

Exit code is always 0 — failure to notify should never block the
producer's tick.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir

STATE_FILENAME = ".notify_state.json"
INBOX_TEMPLATE = """---
to_role: {to_role}
from_role: {from_role}
task_ref: {task}
asked_at: {ts}
answered_at: null
kind: wake
---

Transition in journal: {from_q} -> {to_q} (reason: {reason}).
Check your queue / inbox / run bin/wake_check or bin/gate_check as
appropriate, then continue the tick.
"""


def resolve_coord(project_dir: Path) -> Path:
    if project_dir.name == "coordination" and (project_dir / "journal.ndjson").is_file():
        return project_dir
    return project_dir / "coordination"


def load_schema(canon_dir: Path) -> dict:
    p = canon_dir / "schema.yaml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def queue_owner(schema: dict, queue: str) -> str | None:
    q = (schema.get("queues") or {}).get(queue)
    if isinstance(q, dict):
        owner = q.get("owner")
        if isinstance(owner, str):
            return owner
    return None


def claimers_of(schema: dict, queue: str) -> list[str]:
    out: list[str] = []
    for role, meta in (schema.get("roles") or {}).items():
        if isinstance(meta, dict):
            claims = meta.get("claims_from") or []
            if isinstance(claims, list) and queue in claims:
                out.append(role)
    return out


def find_stand_evidence_targets(coord: Path, stand_task_file: str) -> list[tuple[str, str]]:
    """For a stand_done file, return list of (product_task_id, queue_currently_in).

    Reads the stand_done/<file>.md and extracts evidence_for, then locates
    each referenced product task in the queues.
    """
    fname = stand_task_file
    if not fname.endswith(".md"):
        fname = fname + ".md"
    path = coord / "stand_done" / fname
    if not path.is_file():
        # journal stores task ids without .md and without queue prefix
        # try with -<slug>.md or full id
        candidates = list((coord / "stand_done").glob(f"{stand_task_file}*.md"))
        if not candidates:
            return []
        path = candidates[0]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    evidence_for: list[str] = []
    for raw in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            sr = data.get("stand_result")
            if isinstance(sr, dict):
                ef = sr.get("evidence_for") or []
                if isinstance(ef, list):
                    evidence_for.extend([e for e in ef if isinstance(e, str)])
    # For each evidence target, find which queue it's in
    targets: list[tuple[str, str]] = []
    for tid in evidence_for:
        for q in (coord.iterdir()):
            if not q.is_dir():
                continue
            for f in q.glob("*.md"):
                if f.name == "_TEMPLATE.md":
                    continue
                if f.stem == tid:
                    targets.append((tid, q.name))
                    break
    return targets


def determine_wakeups(schema: dict, coord: Path, entry: dict) -> list[tuple[str, str, str]]:
    """Return list of (to_role, reason, task) wake-ups for a journal entry."""
    to_q = entry.get("to") or ""
    from_q = entry.get("from") or ""
    task = entry.get("task") or ""
    reason = entry.get("reason") or ""
    out: list[tuple[str, str, str]] = []

    if to_q == "stand_done":
        # find product tasks needing this evidence; notify the role that owns
        # the queue they currently sit in
        for product_task, product_queue in find_stand_evidence_targets(coord, task):
            for role in claimers_of(schema, product_queue):
                out.append((role, f"stand evidence for {product_task} ready", product_task))
        return out

    # normal: whoever claims from to_q gets notified
    for role in claimers_of(schema, to_q):
        out.append((role, reason or f"new task in {to_q}", task))

    if to_q == "verified":
        # blocked tasks may unblock — wake REVIEWER for wake_check
        out.append(("ARCHITECT-REVIEWER", f"{task} verified — re-run wake_check", task))

        # If there is any active review_sessions/<id>.md, EXPLORER and
        # ARCHITECT-PLANNER may have iterations blocked on this task.
        # claimers_of("verified") is empty, so without this branch they'd
        # never be notified about verified tasks.
        sessions_dir = coord / "review_sessions"
        if sessions_dir.is_dir():
            has_sessions = any(
                f.suffix == ".md" and f.name != "_TEMPLATE.md"
                for f in sessions_dir.iterdir()
                if f.is_file()
            )
            if has_sessions:
                out.append(("EXPLORER",
                            f"{task} verified — re-check review sessions",
                            task))
                out.append(("ARCHITECT-PLANNER",
                            f"{task} verified — review session may be impacted",
                            task))

    return out


def write_inbox(coord: Path, to_role: str, task: str, from_q: str, to_q: str, reason: str) -> None:
    role_dir_name = to_role.lower()
    inbox = coord / "inbox" / role_dir_name
    inbox.mkdir(parents=True, exist_ok=True)
    fname = f"wake-{int(time.time())}-{task}-{to_q}.md"
    path = inbox / fname
    if path.exists():
        return
    body = INBOX_TEMPLATE.format(
        to_role=to_role,
        from_role="notify_from_journal",
        task=task,
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        from_q=from_q,
        to_q=to_q,
        reason=reason.replace("\n", " ").strip()[:200],
    )
    path.write_text(body, encoding="utf-8")


@click.command(name="notify-journal",
               short_help="replay journal.ndjson → write inbox wake messages",
               help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
@click.option("--once", is_flag=True,
              help="replay ALL lines (backfill); default: only new lines")
def notify_journal(project_dir: Path | None, canon_dir: Path | None, once: bool) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    coord = resolve_coord(project_dir)
    journal = coord / "journal.ndjson"
    if not journal.is_file():
        return

    state_path = coord / STATE_FILENAME
    last_offset = 0
    if not once and state_path.is_file():
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            last_offset = int(st.get("offset", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            last_offset = 0

    try:
        with journal.open("rb") as f:
            f.seek(last_offset)
            new_bytes = f.read()
            new_offset = f.tell()
    except OSError:
        return

    schema = load_schema(canon_dir)
    if not schema:
        return

    for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        # 0152: suppress self-wake. If a role's own action (e.g. PLANNER
        # appending a triage block or running ``greatminds plan``)
        # writes journal lines whose claimer is the same role, we'd
        # bombard PLANNER's own inbox with wake-*.md files for actions
        # they just performed. They already know — they did it. The
        # stop-decide hook then nudges them with stale self-wakes for
        # the next ~N ticks. Other interested roles (REVIEWER for
        # transitions touching their queues, etc.) still get notified.
        # System events (actor='', e.g. notify-journal backfill) wake
        # all targets — there's no self to subtract.
        actor = (entry.get("actor") or "").strip().upper()
        for to_role, reason, task in determine_wakeups(schema, coord, entry):
            if actor and actor == to_role.upper():
                continue
            try:
                write_inbox(
                    coord,
                    to_role=to_role,
                    task=task,
                    from_q=entry.get("from") or "",
                    to_q=entry.get("to") or "",
                    reason=reason,
                )
            except OSError:
                continue

    if not once:
        try:
            state_path.write_text(json.dumps({"offset": new_offset}), encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    notify_journal()
