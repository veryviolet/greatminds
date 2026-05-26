#!/usr/bin/env python3
"""greatminds stand — stand_request stream wrapper around task.

Two subcommands:

  request   create a new stand_request task in coordination/stand_requests/.
            validates that each id in --evidence-for actually exists in
            an active queue (a task that will benefit from the run).
            On success: claim by STAND-KEEPER picks it up automatically.

  result    append stand_result block to a stand_request already in
            stand_wip/, then mv to stand_done/. STAND-KEEPER only.
            Validates result / stand_status / profile enums.

Caller role from ``$GREATMINDS_ROLE`` (no ``--as`` override).

Implementation note: this module calls into ``greatminds.cli.task`` via
direct function imports (``create_task``, ``move_task``, ``append_block``)
— no subprocess between modules of the same package.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir
from greatminds.cli.task import (
    _split_multivalue,
    append_block,
    create_task,
    move_task,
)


# Queues where a product task can still be "active" (i.e. wanting evidence).
# Active queues that a stand's --evidence-for argument may reference.
# 0149: ``review_sessions`` joined the allow-list. The product pipeline
# queues are the original set (a stand records evidence for in-flight
# work); review-session tasks (e.g. ``0007-explorer-...``) are also
# legitimate evidence targets — STAND-KEEPER's run of a smoke/deploy
# stand is evidence that EXPLORER's scenario found nothing fresh, and
# the pre-0149 validator rejected them as ``not in any active queue``.
EVIDENCE_FOR_ACTIVE_QUEUES = (
    "feature_inbox",
    "feature_plan",
    "feature_dev",
    "feature_ui_dev",
    "feature_docs",
    "feature_test",
    "feature_docs_review",
    "feature_review",
    "feature_blocked",
    "review_sessions",
)


def task_exists_in_active(coord: Path, task_id: str) -> bool:
    """``True`` if a task with this id (or id-prefix) sits in any active queue."""
    for q in EVIDENCE_FOR_ACTIVE_QUEUES:
        for ext in (".yaml", ".md"):
            if (coord / q / f"{task_id}{ext}").is_file():
                return True
        qd = coord / q
        if qd.is_dir():
            for f in qd.iterdir():
                if f.is_file() and (
                    f.stem == task_id or f.stem.startswith(task_id + "-")
                ):
                    return True
    return False


_REQUEST_TYPES = ["deploy", "restart", "rebuild", "smoke",
                  "remote_sync", "gpu_check", "teardown"]
_PROFILES = ["full-deploy", "vite-dev"]


@click.group(help="stand_request stream — request a stand op, record result")
def stand() -> None:
    pass


@stand.command(name="request")
@click.option("--request-type", "request_type", required=True,
              type=click.Choice(_REQUEST_TYPES))
@click.option("--profile", required=True, type=click.Choice(_PROFILES))
@click.option("--title", required=True)
@click.option("--hosts", multiple=True, callback=_split_multivalue,
              help="list of hosts; repeat the flag or comma-separate values")
@click.option("--evidence-for", "evidence_for", multiple=True,
              callback=_split_multivalue,
              help="task ids that will use this stand's evidence; "
                   "repeat or comma-separate")
@click.option("--description", default=None, help="literal | @file | -")
@click.option("--priority", default=None,
              type=click.Choice(["low", "normal", "high"]))
@click.option("--reason", default=None, help="journal reason")
def stand_request(request_type, profile, title, hosts, evidence_for,
                  description, priority, reason) -> None:
    coord = find_coord_dir()

    # 0185: when evidence_for points at a worktree-era task, resolve
    # each task's worktree path so STAND-KEEPER's rsync source is the
    # per-task tree, not the lock-era shared main worktree. Stand
    # requests fire from feature_test BEFORE REVIEWER merges, so main
    # HEAD does not yet carry the task's code — sourcing from main
    # would verify stale code and produce false stand_done evidence.
    worktree_sources: dict[str, str] = {}
    if evidence_for:
        try:
            from greatminds.cli import worktree as wt_mod
            policy = wt_mod.load_worktree_policy()
            project_dir = coord.parent
            for tid in evidence_for:
                p = policy.worktree_path_for(project_dir, tid)
                if p.exists():
                    worktree_sources[tid] = str(p)
        except Exception:
            pass

    if evidence_for:
        missing = [tid for tid in evidence_for
                   if not task_exists_in_active(coord, tid)]
        if missing:
            raise GreatMindsError(
                f"evidence-for ids not in any active queue: {missing}",
                exit_code=2,
            )

    target_path = create_task(
        stream="stand",
        title=title,
        request_type=request_type,
        profile=profile,
        hosts=hosts,
        evidence_for=evidence_for,
        description=description,
        priority=priority,
        reason=reason,
    )
    # 0233: resolve target_commit from evidence_for[0]'s impl block.
    # STAND-KEEPER's deploy must `git checkout <target_commit>` so the
    # stand tests the EXACT commit the impl describes — not whatever
    # main HEAD became between filing and SK-run. Stored on the
    # stand_request file; PROJECT.md's deploy recipe reads it.
    target_commit: str | None = None
    if evidence_for:
        try:
            target_commit = _resolve_target_commit_from_evidence(
                coord, evidence_for[0],
            )
        except Exception:
            target_commit = None

    # 0185 + 0233: enrich the request file with worktree_sources and
    # target_commit so SK's deploy recipe has everything it needs in
    # one place. Best-effort; CLI continues even if patch fails.
    if worktree_sources or target_commit:
        import yaml as _yaml
        try:
            doc = _yaml.safe_load(
                target_path.read_text(encoding="utf-8")) or {}
            if worktree_sources:
                doc["worktree_sources"] = worktree_sources
            if target_commit:
                doc["target_commit"] = target_commit
            target_path.write_text(
                _yaml.safe_dump(doc, sort_keys=False),
                encoding="utf-8",
            )
        except (OSError, _yaml.YAMLError):
            pass  # best-effort; SK can fall back to `worktree path` CLI
    click.echo(f"created {target_path}")


def _resolve_target_commit_from_evidence(coord: Path,
                                          evidence_task_id: str) -> str | None:
    """0233: look up ``evidence_task_id``'s implementation.base_commit.

    Walks the coordination tree via the unified find_task helper,
    loads the task file, finds the latest implementation block, and
    returns its ``base_commit`` (or None if any link is missing).
    Falls back to plan.base_commit when no impl block exists yet
    (e.g., the request is for a docs/research task that skips impl).
    """
    try:
        from greatminds.cli.task import find_task, load_task
    except ImportError:
        return None
    located = find_task(coord, evidence_task_id)
    if located is None:
        return None
    path, _queue = located
    try:
        data = load_task(path)
    except Exception:
        return None
    blocks = data.get("blocks") or []
    # Latest implementation block wins.
    for block in reversed(blocks):
        if isinstance(block, dict) and block.get("kind") == "implementation":
            bc = block.get("base_commit")
            if isinstance(bc, str) and bc.strip():
                return bc.strip()
    # Fallback to latest plan block's base_commit.
    for block in reversed(blocks):
        if isinstance(block, dict) and block.get("kind") == "plan":
            bc = block.get("base_commit")
            if isinstance(bc, str) and bc.strip():
                return bc.strip()
    return None


@stand.command(name="result")
@click.argument("task_id", metavar="ID")
@click.option("--result", required=True,
              type=click.Choice(["ok", "partial", "fail"]))
@click.option("--status", required=True,
              type=click.Choice(["READY", "DEGRADED", "DOWN", "BLOCKED"]))
@click.option("--commit", required=True)
@click.option("--profile", required=True, type=click.Choice(_PROFILES))
@click.option("--notes", default=None, help="literal | @file | -")
@click.option("--reason", default=None, help="journal reason for mv")
def stand_result(task_id, result, status, commit, profile,
                 notes, reason) -> None:
    coord = find_coord_dir()

    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        raise GreatMindsError(
            "only STAND-KEEPER may produce stand_result", exit_code=3
        )

    wip = coord / "stand_wip"
    in_wip = (
        (wip / f"{task_id}.yaml").is_file()
        or (wip / f"{task_id}.md").is_file()
        or any(
            f.stem == task_id or f.stem.startswith(task_id + "-")
            for f in wip.glob("*.yaml")
        )
        or any(
            f.stem == task_id or f.stem.startswith(task_id + "-")
            for f in wip.glob("*.md")
        )
    )
    if not in_wip:
        raise GreatMindsError(f"task {task_id} not in stand_wip/")

    append_block(
        task_id=task_id,
        kind="stand_result",
        fields={
            "result": result,
            "stand_status": status,
            "commit": commit,
            "profile": profile,
        },
        body=notes,
    )
    move_task(task_id=task_id, to_queue="stand_done", reason=reason)
    click.echo(f"recorded stand_result and moved {task_id} → stand_done")


def _allowed_profiles() -> list[str]:
    """0244: read ``stand.profiles_allowed`` from schema. Default to
    the plan-documented enum if absent (defensive)."""
    try:
        import yaml as _yaml
        from greatminds.core.paths import find_canon_dir
        doc = _yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except Exception:
        return ["full-deploy", "vite-dev", "smoke-only"]
    stand_doc = (doc.get("stand") or {}).get("resource") or {}
    profiles = stand_doc.get("profiles_allowed")
    if not isinstance(profiles, list) or not profiles:
        return ["full-deploy", "vite-dev", "smoke-only"]
    return [str(p) for p in profiles]


def _holder_role() -> str:
    """Caller's role for lease bookkeeping."""
    role = (os.environ.get("GREATMINDS_ROLE") or "").strip().upper()
    if not role:
        raise GreatMindsError(
            "stand lease requires GREATMINDS_ROLE to be set; the lease "
            "tracks who holds the stand for inbox-info dispatch."
        )
    return role


def _file_inbox_info(coord: Path, to_role: str, body: str,
                     task_ref: str = "") -> None:
    """0244: file an inbox info-message to ``to_role`` from STAND-KEEPER
    (the role responsible for ``ready`` transitions). Best-effort:
    failure does NOT block the state transition — the state file is
    the FSM source-of-truth; inbox messages are a notification layer.

    Shells out to ``greatminds inbox send`` so the journal entry +
    heartbeat side-effects fire through the normal CLI path."""
    try:
        cp = subprocess.run(
            [sys.executable, "-m", "greatminds.cli.main",
             "inbox", "send", to_role,
             "--kind", "info",
             "--body", body]
            + (["--task", task_ref] if task_ref else []),
            cwd=str(coord.parent),
            env={**os.environ, "GREATMINDS_ROLE": "STAND-KEEPER"},
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            click.echo(
                f"  (warn) inbox-info to {to_role} failed: "
                f"{cp.stderr.strip()[:200]}",
                err=True,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


@stand.command(name="lease")
@click.option("--task", "task_id", required=True,
              help="product-task id this lease serves")
@click.option("--worktree", required=True,
              help="path to the worktree SK will deploy from")
@click.option("--profile", required=True,
              help="deploy profile enum (schema.stand.resource."
                   "profiles_allowed)")
@click.option("--ttl-seconds", type=int, default=None,
              help="override ttl (default: schema lease.ttl_seconds_default)")
def stand_lease(task_id: str, worktree: str, profile: str,
                 ttl_seconds: int | None) -> None:
    """0244 (Phase 2 of 0242): request a lease on the singleton stand.

    Behavior:
    - On state=free: transitions free→preparing(lease_id); SK picks
      up the new lease on its next tick.
    - On state≠free: enqueues the request; the lease becomes active
      when SK releases the current one.

    Returns the freshly-minted lease_id (UUID4) as the LAST line of
    stdout. Callers (test scripts, agents) capture this token; only
    the holder may release.
    """
    from greatminds.cli import stand_state as ss

    holder = _holder_role()
    allowed = _allowed_profiles()
    if profile not in allowed:
        raise GreatMindsError(
            f"--profile {profile!r} not in schema.stand.resource."
            f"profiles_allowed: {allowed}",
            exit_code=2,
        )

    # Read schema's default ttl.
    if ttl_seconds is None:
        try:
            import yaml as _yaml
            from greatminds.core.paths import find_canon_dir
            doc = _yaml.safe_load(
                (find_canon_dir() / "schema.yaml").read_text(
                    encoding="utf-8")) or {}
            lease_cfg = ((doc.get("stand") or {}).get("resource") or {}
                         ).get("lease") or {}
            ttl_seconds = int(lease_cfg.get("ttl_seconds_default") or 14400)
        except Exception:
            ttl_seconds = 14400

    new_lease_id = uuid.uuid4().hex
    coord = find_coord_dir()

    def mutator(state):
        lease_obj = {
            "lease_id": new_lease_id,
            "task": task_id,
            "worktree": worktree,
            "profile": profile,
            "holder_role": holder,
            "ttl_seconds": ttl_seconds,
            "enqueued_at": ss.now_iso(),
        }
        if state.get("state") == "free":
            lease_obj["granted_at"] = ss.now_iso()
            lease_obj["ready_at"] = None
            state["active_lease"] = lease_obj
            ss.record_transition(
                state, "free", "preparing", holder,
                lease_id=new_lease_id,
                reason=f"lease for {task_id} ({profile})",
            )
        elif state.get("state") in ("preparing", "ready", "down"):
            queue = state.get("queue") or []
            queue.append(lease_obj)
            state["queue"] = queue

    ss.update_stand_state(coord, mutator)
    click.echo(f"lease_id: {new_lease_id}")


@stand.command(name="release")
@click.option("--lease-id", "lease_id", required=True,
              help="lease_id token returned by `stand lease`")
@click.option("--result", required=True,
              type=click.Choice(["pass", "fail", "partial"]),
              help="machine-readable resolution status (NOT a report)")
def stand_release(lease_id: str, result: str) -> None:
    """0244: release the active lease. Transitions ready→free; SK
    pops the next FIFO queue entry for the next lease.

    Only the holder may release. The CLI rejects with exit_code=3 if
    ``--lease-id`` doesn't match the current active lease. Result is
    a CLOSED ENUM (pass/fail/partial) — no prose channel; TESTER's
    observations live exclusively in the product-task's tests block.

    If the holder mismatches but the lease_id is in the queue, the
    requester is cancelling a pending request (state file removes
    the queue entry; no state transition).
    """
    from greatminds.cli import stand_state as ss
    coord = find_coord_dir()

    holder = _holder_role()

    captured: dict[str, Any] = {}

    def mutator(state):
        active = state.get("active_lease") or {}
        if active and active.get("lease_id") == lease_id:
            if active.get("holder_role") != holder:
                raise GreatMindsError(
                    f"lease {lease_id} held by "
                    f"{active.get('holder_role')!r}; only the holder "
                    f"may release",
                    exit_code=3,
                )
            captured["task"] = active.get("task")
            captured["was_active"] = True
            state["active_lease"] = None
            ss.record_transition(
                state, state.get("state") or "ready", "free",
                holder, lease_id=lease_id,
                reason=f"release ({result})",
            )
            return
        # Look in queue — cancellation case.
        queue = state.get("queue") or []
        new_queue = []
        cancelled = False
        for entry in queue:
            if isinstance(entry, dict) and entry.get("lease_id") == lease_id:
                if entry.get("holder_role") != holder:
                    raise GreatMindsError(
                        f"queued lease {lease_id} held by "
                        f"{entry.get('holder_role')!r}; only the holder "
                        f"may cancel",
                        exit_code=3,
                    )
                cancelled = True
                continue
            new_queue.append(entry)
        if cancelled:
            state["queue"] = new_queue
            captured["was_cancelled"] = True
        else:
            raise GreatMindsError(
                f"lease {lease_id} not found (not active, not queued)",
                exit_code=2,
            )

    ss.update_stand_state(coord, mutator)
    if captured.get("was_active"):
        click.echo(f"released lease {lease_id} (result={result})")
    elif captured.get("was_cancelled"):
        click.echo(f"cancelled queued lease {lease_id}")


@stand.command(name="down")
@click.option("--reason", required=True,
              help="operational reason logged in state file")
def stand_down(reason: str) -> None:
    """0244: SK-only. Mark the stand DOWN (failed deploy / infra
    incident). Halts queue processing until `stand up`."""
    from greatminds.cli import stand_state as ss
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        raise GreatMindsError(
            "only STAND-KEEPER may transition state to down",
            exit_code=3,
        )
    coord = find_coord_dir()

    def mutator(state):
        prev = state.get("state") or "free"
        state["down_reason"] = reason
        ss.record_transition(state, prev, "down", role, reason=reason)

    ss.update_stand_state(coord, mutator)
    click.echo(f"state → down: {reason}")


@stand.command(name="up")
@click.option("--reason", required=True, help="resolution note")
def stand_up(reason: str) -> None:
    """0244: SK-only. Transition down→free; resumes queue processing
    on SK's next tick."""
    from greatminds.cli import stand_state as ss
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        raise GreatMindsError(
            "only STAND-KEEPER may transition state out of down",
            exit_code=3,
        )
    coord = find_coord_dir()

    def mutator(state):
        if state.get("state") != "down":
            raise GreatMindsError(
                f"stand up requires state=down; current state="
                f"{state.get('state')!r}",
                exit_code=2,
            )
        state["down_reason"] = None
        ss.record_transition(state, "down", "free", role, reason=reason)

    ss.update_stand_state(coord, mutator)
    click.echo(f"state → free: {reason}")


@stand.command(name="ready")
@click.option("--lease-id", "lease_id", required=True,
              help="lease that just finished preparing")
def stand_ready(lease_id: str) -> None:
    """0244: SK-only. Transition preparing→ready for ``lease_id``
    after deploy + smoke succeeds. Files an inbox-info to the lease
    holder so they wake up and start probing the stand."""
    from greatminds.cli import stand_state as ss
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        raise GreatMindsError(
            "only STAND-KEEPER may transition state to ready",
            exit_code=3,
        )
    coord = find_coord_dir()
    captured: dict[str, str] = {}

    def mutator(state):
        if state.get("state") != "preparing":
            raise GreatMindsError(
                f"stand ready requires state=preparing; current="
                f"{state.get('state')!r}",
                exit_code=2,
            )
        active = state.get("active_lease") or {}
        if active.get("lease_id") != lease_id:
            raise GreatMindsError(
                f"active lease is {active.get('lease_id')!r}, not "
                f"{lease_id!r}",
                exit_code=3,
            )
        active["ready_at"] = ss.now_iso()
        captured["holder"] = active.get("holder_role", "")
        captured["task"] = active.get("task", "")
        ss.record_transition(state, "preparing", "ready", role,
                              lease_id=lease_id, reason="deploy ok")

    ss.update_stand_state(coord, mutator)
    if captured.get("holder"):
        _file_inbox_info(
            coord, captured["holder"],
            f"stand lease {lease_id} ready; "
            f"task={captured.get('task', '?')}",
            task_ref=captured.get("task", ""),
        )
    click.echo(f"state → ready (lease {lease_id})")


@stand.command(name="status")
def stand_status() -> None:
    """0243 (0242 Phase 1): print the singleton stand resource state.

    Reads ``coordination/.stand/state.yaml`` (creates a synthetic
    empty-state view when the file doesn't exist yet) and prints a
    compact human-readable summary: state, active lease (if any),
    queue contents, and the last few transitions.

    Read-only. Mutation paths land in 0244 (lease/release) and 0245
    (SK migration).
    """
    from greatminds.cli import stand_state as ss
    coord = find_coord_dir()
    state = ss.read_stand_state(coord)

    click.echo(f"state: {state.get('state')}")
    active = state.get("active_lease")
    if active:
        click.echo("active_lease:")
        for k in ("lease_id", "task", "worktree", "holder_role",
                  "granted_at", "ready_at", "ttl_seconds"):
            v = active.get(k)
            if v is not None:
                click.echo(f"  {k}: {v}")
    else:
        click.echo("active_lease: (none)")

    queue = state.get("queue") or []
    if queue:
        click.echo(f"queue: {len(queue)} pending")
        for i, item in enumerate(queue):
            click.echo(
                f"  {i+1}. lease={item.get('lease_id', '?')[:8]} "
                f"task={item.get('task', '?')} "
                f"role={item.get('holder_role', '?')}"
            )
    else:
        click.echo("queue: (empty)")

    down_reason = state.get("down_reason")
    if down_reason:
        click.echo(f"down_reason: {down_reason}")

    history = state.get("history") or []
    if history:
        click.echo("history (last 5):")
        for entry in history[-5:]:
            click.echo(
                f"  {entry.get('t', '?')} {entry.get('from', '?')}"
                f" → {entry.get('to', '?')} "
                f"by={entry.get('by', '?')}"
            )


if __name__ == "__main__":
    stand()
