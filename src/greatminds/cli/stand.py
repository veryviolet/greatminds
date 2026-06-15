#!/usr/bin/env python3
"""greatminds stand — singleton stand lease and deployment commands.

Current stand operations use ``coordination/.stand/state.yaml`` plus the lease
API:

  lease    request a lease for a product task and stand profile.
  status   inspect the stand state, active lease, pending queue, and history.
  deploy   run the active lease's profile through the stand executor.
  ready    mark a successfully deployed active lease ready.
  release  release the active lease with pass/fail/partial result.
  down/up  pause or recover the singleton stand resource.
  reclaim  recover an expired or abandoned lease.

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


@click.group(help="singleton stand lease and deployment commands")
def stand() -> None:
    pass


# 0247 (1.3.0): `greatminds stand request` and `greatminds stand
# result` CLI subcommands REMOVED. They wrote to the deleted
# stand_requests / stand_wip / stand_done queues. Use the lease
# API instead:
#   - `greatminds stand lease --task <id> --worktree <path> --profile <enum>`
#     (replaces `stand request`)
#   - SK runs `greatminds stand ready --lease-id <id>` when deploy
#     succeeds; TESTER runs `greatminds stand release --lease-id <id>
#     --result pass|fail|partial` (replaces `stand result`).


def _validate_lease_worktree(task_id: str, worktree: str,
                              project_dir: Path) -> None:
    """0271: enforce schema.stand.resource.lease.worktree_constraint.

    Reject any path that is not ``<project_dir>/.worktrees/<seq>[-...]``.
    The main fleet tree (``project_dir`` itself) is rejected with the
    most explicit error because it is the deployment foot-gun that
    motivated this task — passing it would direct STAND-KEEPER to
    deploy onto the running host's own checkout.
    """
    if not worktree or not isinstance(worktree, str):
        raise GreatMindsError(
            "stand lease --worktree must be a non-empty path",
            exit_code=2,
        )

    try:
        wt = Path(worktree).resolve(strict=False)
    except (OSError, RuntimeError):
        raise GreatMindsError(
            f"stand lease --worktree {worktree!r} cannot be resolved",
            exit_code=2,
        )

    project_resolved = project_dir.resolve(strict=False)
    if wt == project_resolved:
        raise GreatMindsError(
            "stand lease --worktree must be a per-task isolated branch "
            "checkout under "
            f"{project_resolved}/.worktrees/<seq>[-slug], NOT the main "
            "fleet tree itself (deploying onto the running host would "
            "self-modify the very processes the lease serves). Create "
            f"the worktree with: git worktree add "
            f"{project_resolved}/.worktrees/{task_id.split('-')[0]} "
            f"task/{task_id}",
            exit_code=2,
        )

    expected_parent = project_resolved / ".worktrees"
    if wt.parent != expected_parent:
        raise GreatMindsError(
            f"stand lease --worktree {worktree!r} must live under "
            f"{expected_parent}/ (got parent {wt.parent}); per-task "
            "isolated branch is required by schema.stand.resource."
            "lease.worktree_constraint",
            exit_code=2,
        )

    seq = task_id.split("-", 1)[0]
    name = wt.name
    if name != seq and not name.startswith(f"{seq}-"):
        raise GreatMindsError(
            f"stand lease --worktree {worktree!r}: basename {name!r} "
            f"must equal task seq {seq!r} or start with {seq + '-'!r}. "
            f"The full task id is {task_id!r}; expected one of: "
            f"{expected_parent / seq} or {expected_parent / task_id}",
            exit_code=2,
        )


def _resolve_or_create_lease_worktree(task_id: str, worktree: "str | None",
                                       project_dir: Path) -> str:
    """0380: resolve the lease worktree path and ensure it EXISTS, auto-
    creating the per-task worktree when absent.

    Before 0380 ``stand lease`` REQUIRED an existing ``--worktree`` under
    ``.worktrees/<seq>[-slug]``. That stranded EXPLORER's review_session
    leases: review_session tasks get NO per-task worktree on route to
    review_sessions (worktrees.required_for_task_kinds is product-only), and
    EXPLORER is forbidden from running raw ``git worktree add``. There was
    no legal path to a lease.

    Now the worktree is materialized lazily, here, via the sanctioned
    ``worktree_create`` CLI surface (NOT raw git from the calling role):

    - ``worktree`` omitted → default to the canonical per-task path
      (``.worktrees/<task_id>``), so a caller need not know/guess it.
    - the shape is still validated (the main-fleet-tree foot-gun stays
      rejected — deploying onto the running host self-modifies it).
    - if the path doesn't exist yet, create the per-task worktree and
      return the canonical path actually created (off plan.base_commit,
      else default_branch HEAD — fine for a review_session with no plan).

    Returns the absolute worktree path the lease should record."""
    from greatminds.cli.worktree import (load_worktree_policy,
                                          worktree_create)
    if not worktree:
        policy = load_worktree_policy(project_dir)
        worktree = str(policy.worktree_path_for(project_dir, task_id))
    _validate_lease_worktree(task_id, worktree, project_dir)
    wt = Path(worktree).resolve(strict=False)
    if not wt.is_dir():
        created = worktree_create(project_dir, task_id)
        return str(created.resolve(strict=False))
    if not (wt / ".git").exists():
        raise GreatMindsError(
            f"stand lease --worktree {wt} exists but is not a git worktree "
            "(missing .git file/dir). Stand leases deploy source checkouts "
            "under .worktrees/<task>, not no-git deployed payloads. Remove "
            "the bad directory and rerun the lease so Greatminds can create "
            "a real per-task worktree.",
            exit_code=2,
        )
    return str(wt)


def _stand_keeper_notification_target(event: str) -> str | None:
    """0291: read ``schema.stand_keeper.notifications.<event>`` so
    SK's auto-inbox-info on lifecycle events stays schema-driven
    rather than hardcoded. Returns None when the schema lacks the
    notification entry — caller silently skips the send (graceful
    degradation; the FSM is unaffected)."""
    try:
        import yaml as _yaml
        from greatminds.core.paths import find_canon_dir
        doc = _yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(
                encoding="utf-8")) or {}
    except Exception:
        return None
    notif = ((doc.get("stand_keeper") or {})
             .get("notifications") or {})
    target = notif.get(event)
    if isinstance(target, str) and target.strip():
        return target.strip()
    return None


def _available_profiles(coord: Path) -> list[str]:
    """Profile names present in THIS project's ``stand-profiles/`` dir —
    the real set a leaser may name (each fleet's own, e.g. mlgpu2 /
    orange, plus the seeded presets full-deploy / vite-dev / smoke-only).
    The lease no longer restricts to a hardcoded schema enum: whatever
    profile the leaser names is what coordd deploys."""
    d = coord / "stand-profiles"
    if not d.is_dir():
        return []
    return sorted({p.stem for p in d.iterdir()
                   if p.suffix in (".yaml", ".md")
                   and not p.name.startswith("_")})


def _holder_role() -> str:
    """Caller's role for lease bookkeeping."""
    role = (os.environ.get("GREATMINDS_ROLE") or "").strip().upper()
    if not role:
        raise GreatMindsError(
            "stand lease requires GREATMINDS_ROLE to be set; the lease "
            "tracks who holds the stand for inbox-info dispatch."
        )
    return role


STAND_GLOBAL_CONTROL_ROLES = frozenset({"MAINTAINER"})


def _require_stand_global_control_role(command: str) -> str:
    """0395: down/up are global singleton controls, not probe commands."""
    role = (os.environ.get("GREATMINDS_ROLE") or "OPERATOR").upper()
    if role not in STAND_GLOBAL_CONTROL_ROLES:
        allowed = ", ".join(sorted(STAND_GLOBAL_CONTROL_ROLES))
        raise GreatMindsError(
            f"stand {command} is restricted to {allowed}; role {role} "
            "may not mutate global stand availability. Use lease/release "
            "for holder-scoped lifecycle actions, or ask MAINTAINER for "
            "infrastructure recovery.",
            exit_code=3,
        )
    return role


def _lease_expired(lease: dict) -> bool:
    """0342: True iff the lease is past ``granted_at + ttl_seconds``.

    Conservative: if the timestamp or ttl can't be read, returns False
    (treat as NOT expired) so a force-reclaim never steals a lease whose
    expiry can't be proven."""
    from datetime import datetime, timedelta, timezone
    ttl = lease.get("ttl_seconds")
    started = lease.get("granted_at") or lease.get("enqueued_at")
    if not isinstance(ttl, (int, float)) or not started:
        return False
    try:
        t0 = datetime.fromisoformat(str(started))
    except (ValueError, TypeError):
        return False
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc) >= t0 + timedelta(seconds=int(ttl))


def _holder_alive(coord: Path, holder_role: str) -> bool:
    """0342: True iff the lease holder's agent pid (from the per-role
    registry) is alive. Absent/unreadable registry or a dead pid →
    False (holder not alive → safe to reclaim an expired lease)."""
    import json
    if not holder_role:
        return False
    reg = coord / ".agent_registry" / f"{holder_role.lower()}.json"
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pid = data.get("pid") if isinstance(data, dict) else None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def _file_inbox_info(coord: Path, to_role: str, body: str,
                     task_ref: str = "") -> None:
    """File an inbox info-message to ``to_role`` about a stand lifecycle
    event. 1.6.0: the stand is driven by coordd (no STAND-KEEPER role), so
    the notification is filed as MAINTAINER (the system/infra role —
    STAND-KEEPER is gone and would be rejected as an unknown sender).
    Best-effort: failure does NOT block the state transition — the state
    file is the FSM source-of-truth; inbox messages are a notification
    layer.

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
            env={**os.environ, "GREATMINDS_ROLE": "MAINTAINER"},
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
@click.option("--worktree", required=False, default=None,
              help="path to the worktree coordd will deploy from. "
                   "Optional: defaults to the canonical per-task worktree "
                   "(.worktrees/<task_id>) and is auto-created if absent, so "
                   "review_session leases (EXPLORER) need no raw git.")
@click.option("--profile", required=True,
              help="deploy profile NAME — any profile file in this "
                   "project's stand-profiles/ (the leaser picks it; "
                   "coordd deploys exactly that profile)")
@click.option("--ttl-seconds", type=int, default=None,
              help="override ttl (default: schema lease.ttl_seconds_default)")
@click.option("--deploy-prerequisites-only", "deploy_prerequisites_only",
              is_flag=True, default=False,
              help="run only prerequisite-tagged profile tasks before the "
                   "holder performs the actual deploy. Overrides the "
                   "profile-level setting.")
def stand_lease(task_id: str, worktree: "str | None", profile: str,
                 ttl_seconds: int | None,
                 deploy_prerequisites_only: bool) -> None:
    """Request a lease on the singleton stand.

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
    coord = find_coord_dir()
    current_state = ss.read_stand_state(coord)
    if current_state.get("state") == "down":
        reason = current_state.get("down_reason") or "unknown reason"
        raise GreatMindsError(
            "stand lease refused: singleton stand is down; no lease "
            "was queued. Current down_reason: "
            f"{reason}. Recover the stand first (for stale deployment "
            "recovery: run the command named in down_reason, then "
            "`greatminds stand up --reason <recovered>` and re-run "
            "the lease).",
            exit_code=2,
        )
    # The leaser names ANY profile that exists in this project's
    # stand-profiles/ dir — each fleet ships its own (e.g. mlgpu2 /
    # orange) alongside the seeded presets (full-deploy / vite-dev /
    # smoke-only). coordd deploys EXACTLY the profile named here, so
    # validate it the same way coordd will resolve it: by load_profile.
    # No hardcoded enum — that is what silently collapsed every lease
    # onto the seeded full-deploy preset.
    from greatminds.cli.stand_profile import load_profile
    try:
        # issue #12: validate the way coordd will RESOLVE it at deploy —
        # worktree copy first (a profile that exists only in the lease
        # worktree is valid), then the main tree.
        load_profile(coord, profile, worktree=worktree)
    except GreatMindsError as exc:
        raise GreatMindsError(
            f"--profile {profile!r}: {exc} (available in stand-profiles/: "
            f"{_available_profiles(coord)})",
            exit_code=2,
        )

    # 0271: enforce per-task worktree isolation at acquire-time so
    # the mistake never reaches state.yaml. Pre-0271 a wrong path
    # would orphan TESTER's lease in preparing/ until SK's whitelist
    # rejected it on the next tick — a confusing failure mode the
    # CLI is now the first line of defense against.
    # 0380: resolve (default to the canonical per-task path) AND auto-create
    # the worktree when absent, so review_session leases (EXPLORER) have a
    # legal path without raw git. The returned path is absolute — GitHub #10
    # (Bug A): coordd re-resolves the stored string against ITS OWN cwd
    # (often /home/<user> under systemd-user with no WorkingDirectory=), so a
    # relative path would be rejected as "unknown worktree location" (rc 126).
    project_dir = find_coord_dir().parent
    worktree = _resolve_or_create_lease_worktree(task_id, worktree,
                                                 project_dir)

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
        # 0283: only persist the flag when set, so leases that
        # accept the profile default keep the state file minimal.
        if deploy_prerequisites_only:
            lease_obj["deploy_prerequisites_only"] = True
        if state.get("state") == "free":
            lease_obj["granted_at"] = ss.now_iso()
            lease_obj["ready_at"] = None
            state["active_lease"] = lease_obj
            # 0285: clear stale ``down_reason`` left by a prior
            # lease that crashed before reaching ``up`` — every
            # free→preparing is a clean slate; SK's whitelist
            # diagnostic loop must not see yesterday's reason and
            # short-circuit today's deploy.
            state["down_reason"] = None
            ss.record_transition(
                state, "free", "preparing", holder,
                lease_id=new_lease_id,
                reason=f"lease for {task_id} ({profile})",
            )
        elif state.get("state") in ("preparing", "ready"):
            queue = state.get("queue") or []
            queue.append(lease_obj)
            state["queue"] = queue
        elif state.get("state") == "down":
            reason = state.get("down_reason") or "unknown reason"
            raise GreatMindsError(
                "stand lease refused: singleton stand is down; no lease "
                "was queued. Current down_reason: "
                f"{reason}. Recover the stand first (for stale deployment "
                "recovery: run the command named in down_reason, then "
                "`greatminds stand up --reason <recovered>` and re-run "
                "the lease).",
                exit_code=2,
            )

    ss.update_stand_state(coord, mutator)
    click.echo(f"lease_id: {new_lease_id}")


@stand.command(name="release")
@click.option("--lease-id", "lease_id", required=True,
              help="lease_id token returned by `stand lease`")
@click.option("--result", required=True,
              type=click.Choice(["pass", "fail", "partial"]),
              help="machine-readable resolution status (NOT a report)")
def stand_release(lease_id: str, result: str) -> None:
    """Release the active lease and promote the next queued lease.

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
            # 0343: the documented "pops the next FIFO queue entry"
            # must actually happen — promote the head lease so a queued
            # validation activates without a manual re-lease.
            captured["promoted"] = ss.promote_head_on_free(state, holder)
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
        msg = f"released lease {lease_id} (result={result})"
        if captured.get("promoted"):
            msg += (f"; auto-promoted queued lease "
                    f"{captured['promoted']} → preparing")
        click.echo(msg)
    elif captured.get("was_cancelled"):
        click.echo(f"cancelled queued lease {lease_id}")


@stand.command(name="reclaim")
@click.option("--lease-id", "lease_id", default=None,
              help="lease to reclaim (default: the active lease)")
def stand_reclaim(lease_id: str | None) -> None:
    """Reclaim an expired lease whose holder is no longer alive.

    A stale lease (a crashed holder past its ttl_seconds) otherwise
    permanently locks the singleton — ``release`` is holder-only and
    there was no reaper. ``reclaim`` is restricted to STAND-KEEPER /
    ARCHITECT-PLANNER (the stand owners) and MAINTAINER (whose contract
    carries reclaim_stale_stand_lease_past_ttl_with_dead_holder as a
    recovery duty); it refuses to clobber a live, in-TTL lease OR a lease
    whose holder pid is still alive: it only frees a lease that is BOTH
    past its TTL AND held by a dead/absent agent.
    """
    from greatminds.cli import stand_state as ss
    role = _holder_role()
    if role.upper() not in ("ARCHITECT-PLANNER", "MAINTAINER"):
        raise GreatMindsError(
            "only ARCHITECT-PLANNER or MAINTAINER may "
            "reclaim a lease",
            exit_code=3,
        )
    coord = find_coord_dir()
    captured: dict[str, Any] = {}

    def mutator(state):
        active = state.get("active_lease") or {}
        if not active:
            raise GreatMindsError(
                "no active lease to reclaim; stand is not leased",
                exit_code=3,
            )
        if lease_id and active.get("lease_id") != lease_id:
            raise GreatMindsError(
                f"lease {lease_id} is not the active lease "
                f"({active.get('lease_id')})",
                exit_code=3,
            )
        if not _lease_expired(active):
            raise GreatMindsError(
                "active lease is still within its TTL — a live lease "
                "cannot be force-reclaimed; the holder must release it",
                exit_code=3,
            )
        hr = active.get("holder_role") or ""
        if _holder_alive(coord, hr):
            raise GreatMindsError(
                f"lease holder {hr!r} is still alive — it must release "
                f"its own lease (reclaim is for dead/absent holders only)",
                exit_code=3,
            )
        captured["lease_id"] = active.get("lease_id")
        captured["holder"] = hr
        state["active_lease"] = None
        ss.record_transition(
            state, state.get("state") or "preparing", "free", role,
            lease_id=active.get("lease_id"),
            reason=f"reclaimed expired lease (holder {hr} not alive)",
        )
        # 0343 parity: reclaim must pop the FIFO head just like release,
        # else a reclaimed stand is freed but a queued lease (e.g. TESTER
        # waiting behind a dead holder) is never promoted — the stand stays
        # free-with-pending-queue forever.
        captured["promoted"] = ss.promote_head_on_free(state, role)

    ss.update_stand_state(coord, mutator)
    msg = (f"reclaimed expired lease {captured['lease_id']} "
           f"(holder {captured['holder']} not alive); stand → free")
    if captured.get("promoted"):
        msg += (f"; auto-promoted queued lease "
                f"{captured['promoted']} → preparing")
    click.echo(msg)


@stand.command(name="down")
@click.option("--reason", required=True,
              help="operational reason logged in state file")
def stand_down(reason: str) -> None:
    """Mark the stand down after a deploy or infrastructure incident.

    MAINTAINER-only.
    """
    from greatminds.cli import stand_state as ss
    role = _require_stand_global_control_role("down")
    coord = find_coord_dir()

    captured: dict[str, Any] = {}

    def mutator(state):
        prev = state.get("state") or "free"
        state["down_reason"] = reason
        # 0289: the lease that triggered this incident is no longer
        # the live target — clear the active_lease pointer so
        # ``stand status`` doesn't show an orphan record alongside
        # state=down. The lease's task / commit details are still
        # in the transition history for audit.
        active = state.get("active_lease") or {}
        captured["task"] = active.get("task", "")
        captured["lease_id"] = active.get("lease_id", "")
        state["active_lease"] = None
        ss.record_transition(state, prev, "down", role, reason=reason)

    ss.update_stand_state(coord, mutator)
    # 0291: auto-notify PLANNER on down so they don't need to poll
    # state.yaml. Best-effort — failure to send doesn't block the
    # transition (state.yaml is the FSM source-of-truth; the inbox
    # message is a convenience).
    notify_target = _stand_keeper_notification_target("on_down")
    if notify_target:
        body = f"stand down: {reason}"
        if captured.get("lease_id"):
            body += f" (lease_id={captured['lease_id']})"
        _file_inbox_info(
            coord, notify_target, body,
            task_ref=captured.get("task", ""),
        )
    click.echo(f"state → down: {reason}")


@stand.command(name="up")
@click.option("--reason", required=True, help="resolution note")
def stand_up(reason: str) -> None:
    """Recover a down stand and resume queue processing.

    MAINTAINER-only.
    """
    from greatminds.cli import stand_state as ss
    role = _require_stand_global_control_role("up")
    coord = find_coord_dir()

    captured: dict[str, Any] = {}

    def mutator(state):
        if state.get("state") != "down":
            raise GreatMindsError(
                f"stand up requires state=down; current state="
                f"{state.get('state')!r}",
                exit_code=2,
            )
        state["down_reason"] = None
        # 0289: down→free is a clean slate for the next lease.
        # Make sure no orphan active_lease lingers (stand_down should
        # already have cleared it; this is the second-line defense
        # for older state files written before 0289).
        state["active_lease"] = None
        ss.record_transition(state, "down", "free", role, reason=reason)
        # 0343: resuming from down with a non-empty queue must promote
        # the head lease (down→free→preparing), not leave it stranded.
        captured["promoted"] = ss.promote_head_on_free(state, role)

    ss.update_stand_state(coord, mutator)
    if captured.get("promoted"):
        click.echo(
            f"state → free: {reason}; auto-promoted queued lease "
            f"{captured['promoted']} → preparing")
    else:
        click.echo(f"state → free: {reason}")


# 0388: a deploy whose lease worktree is missing a verified dependency's
# code is refused with this rc. Distinct from the executor's rc codes
# (113 no-hosts, 124 timeout, 126 unsafe) so callers/tests can tell a
# stale-deployment refusal apart from a real ansible failure. The stand
# transitions preparing→down with an actionable down_reason; deploy_lease
# RETURNS this rc (never raises) so coordd records the attempt and does
# NOT enter the 5× retry loop — the staleness is deterministic and a
# retry would only re-discover it.
DEPLOY_STALE_RC = 117


def _git_capture(worktree: Path, args: list[str]):
    """Run ``git -C <worktree> <args>`` read-only; return the
    CompletedProcess or None when git is unavailable / errors out."""
    try:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _verified_dep_commit(merged: dict) -> str | None:
    """The verified commit of a dependency task: the latest ``review``
    block's ``commit`` (what REVIEWER approved and merged to main). That
    commit becomes reachable from main on merge, so a worktree refreshed
    off current main contains it; a stale worktree does not. Returns None
    when no review block carries a commit (can't anchor staleness)."""
    commit: str | None = None
    for b in merged.get("blocks") or []:
        if not isinstance(b, dict) or b.get("kind") != "review":
            continue
        c = b.get("commit")
        if isinstance(c, str) and c.strip():
            commit = c.strip()
    return commit


def stale_verified_deps_for_lease(
        coord: Path, task_id: str | None,
        worktree: str | None) -> list[tuple[str, str]]:
    """0388: detect a STALE lease worktree — one whose code predates a
    verified dependency the leasing task was explicitly blocked on.

    Returns ``[(dep_id, dep_commit), ...]`` for every verified review
    commit that the lease worktree is missing. For ordinary product tasks,
    the checked set is intentionally narrow: only ``verified/<id>``
    dependencies drawn from the task's ``blocked`` blocks. For
    ``review_session`` tasks, the checked set is broader: every product
    task currently in ``verified/``. A final EXPLORER review-session
    lease is an integrated-system probe; after product drain it must not
    deploy a worktree predating later verified product commits.

    Deploying a stale worktree runs code missing an already-verified fix
    — the 0388 wedge (a resumed review_session redeploys its old base and
    rediscovers the very bug whose fix was verified upstream).

    CONSERVATIVE by construction: returns ``[]`` whenever staleness
    cannot be positively determined — no task_id / no worktree, a
    non-git worktree, a dependency that isn't in ``verified/`` or has no
    review commit, or a commit object git can't resolve. Only a definite
    "commit exists and is NOT an ancestor of worktree HEAD" is reported,
    so a healthy refreshed worktree and ordinary fresh leases never trip
    it."""
    from greatminds.cli.task import find_task, load_task

    if not task_id or not worktree:
        return []
    wt = Path(worktree)
    # Confirm the worktree is a real git checkout before any ancestry
    # query — otherwise we cannot determine staleness (→ []).
    probe = _git_capture(wt, ["rev-parse", "--is-inside-work-tree"])
    if probe is None or probe.returncode != 0:
        return []

    found = find_task(coord, task_id)
    if found is None:
        return []
    try:
        merged = load_task(found[0])
    except Exception:
        return []

    # Collect dependency refs from every blocked block on the task.
    dep_refs: list[str] = []
    for b in merged.get("blocks") or []:
        if not isinstance(b, dict) or b.get("kind") != "blocked":
            continue
        for d in b.get("dependencies") or []:
            if isinstance(d, str) and d.strip():
                dep_refs.append(d.strip())

    # Review sessions are integrated black-box probes. They may be created
    # before later product tasks reach verified; by the time EXPLORER leases
    # the stand after a product drain, their worktree must include every
    # verified product review commit, not only the fixes they were explicitly
    # blocked on earlier.
    if merged.get("stream") == "review_session":
        verified_dir = coord / "verified"
        try:
            for p in sorted(verified_dir.glob("*.yaml")):
                try:
                    dep = load_task(p)
                except Exception:
                    continue
                if dep.get("stream") == "review_session":
                    continue
                dep_refs.append(f"verified/{p.name}")
        except OSError:
            pass

    stale: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ref in dep_refs:
        # Only dependencies satisfied by reaching the terminal verified/
        # queue carry merged runtime code worth a staleness check.
        if not ref.startswith("verified/"):
            continue
        dep_path = coord / ref
        if not dep_path.is_file():
            continue
        dep_id = Path(ref).stem
        if dep_id in seen:
            continue
        seen.add(dep_id)
        try:
            dep_merged = load_task(dep_path)
        except Exception:
            continue
        commit = _verified_dep_commit(dep_merged)
        if not commit:
            continue
        # The commit object must exist in this worktree's repo before an
        # ancestry test is meaningful; if git can't resolve it, skip.
        exists = _git_capture(wt, ["cat-file", "-e", f"{commit}^{{commit}}"])
        if exists is None or exists.returncode != 0:
            continue
        anc = _git_capture(
            wt, ["merge-base", "--is-ancestor", commit, "HEAD"])
        if anc is None:
            continue
        if anc.returncode == 1:          # commit is NOT an ancestor → stale
            stale.append((dep_id, commit))
        # returncode 0 → present (ok); any other code → indeterminate, skip
    return stale


def deploy_lease(coord: Path, *, lease_id: str | None = None,
                 ansible_playbook: str | None = None,
                 timeout_seconds: float | None = None) -> tuple[int, str]:
    """Deterministic, sanctioned deploy of the active lease's profile.

    The single deploy path (1.6.0): load the active lease's YAML/ansible
    profile, run it (``execute_yaml_profile`` writes the deploy marker +
    honours ``is_deploy_safe`` / prerequisite tags), then transition the
    stand ``preparing → ready`` (rc==0) or ``→ down`` (rc!=0). No LLM in
    the loop — coordd runs this automatically on a ``preparing`` lease,
    and ``greatminds stand deploy`` exposes the SAME engine for manual /
    operator runs, so the deploy is a legible command instead of a raw
    in-agent ``python -c`` that the classifier blocks.

    MD-format profiles are rejected — the deploy must be a declarative
    playbook, not LLM-executed prose. Returns ``(rc, combined_log)``.
    """
    from greatminds.cli import stand_state as ss
    from greatminds.cli.stand_executor import dispatch_profile
    from greatminds.cli.stand_profile import load_profile

    cap: dict[str, Any] = {}

    def _read(state):
        if state.get("state") != "preparing":
            raise GreatMindsError(
                f"stand deploy requires state=preparing; current="
                f"{state.get('state')!r}", exit_code=2)
        active = state.get("active_lease") or {}
        if lease_id and active.get("lease_id") != lease_id:
            raise GreatMindsError(
                f"active lease is {active.get('lease_id')!r}, not "
                f"{lease_id!r}", exit_code=3)
        cap.update(active)

    ss.update_stand_state(coord, _read)

    # 0388: refuse to deploy a STALE lease worktree — one missing the
    # verified-dependency code the leasing task was blocked on. Without
    # this, a resumed review_session redeploys its old base commit and
    # rediscovers the very wedge whose fix was already verified upstream,
    # while the stand still reports "ready". Fail fast (before the ansible
    # run) and surface an actionable stale-deployment reason instead of
    # declaring the lease ready. RETURN (not raise) so coordd records the
    # attempt and skips its retry loop — the staleness is deterministic.
    stale = stale_verified_deps_for_lease(
        coord, cap.get("task"), cap.get("worktree"))
    if stale:
        names = ", ".join(f"{d} (commit {c[:12]})" for d, c in stale)
        lid = cap.get("lease_id")
        holder = cap.get("holder_role")
        task_ref = cap.get("task") or ""
        reason = (
            f"STALE DEPLOYMENT refused: lease worktree {cap.get('worktree')} "
            f"is missing verified dependency code [{names}] that task "
            f"{task_ref!r} was blocked on. The stand would run code predating "
            f"the verified fix and rediscover the resolved issue. Refresh the "
            f"worktree off current main with exactly: "
            f"`greatminds worktree remove --force <id>` then "
            f"`greatminds worktree create <id>`; then mark the stand recovered "
            f"with `greatminds stand up --reason stale-worktree-refreshed` and "
            f"re-run the lease. `worktree create` refreshes stale "
            f"review-session branches so the deployed code includes the "
            f"verified dependency."
        )

        def _down_stale(state):
            prev = state.get("state") or "preparing"
            state["down_reason"] = reason
            state["active_lease"] = None
            ss.record_transition(state, prev, "down", "COORDD",
                                 lease_id=lid, reason=reason)

        ss.update_stand_state(coord, _down_stale)
        # Notify the lease holder (so the resumed session sees the block
        # instead of silently retrying) and PLANNER (who decides whether
        # to refresh the worktree or keep the session blocked).
        for tgt in dict.fromkeys(
                t for t in (holder, "ARCHITECT-PLANNER") if t):
            _file_inbox_info(coord, tgt, reason, task_ref=task_ref)
        return DEPLOY_STALE_RC, reason

    profile = cap.get("profile")
    # issue #12: resolve the profile from the active lease's WORKTREE first
    # (its coordination/stand-profiles/ copy), so a stand-profile fix under
    # review is the one deployed/validated — not the unchanged main-tree
    # copy. Falls back to the main tree when the worktree lacks it.
    lease_worktree = cap.get("worktree")
    spec = load_profile(coord, profile, worktree=lease_worktree)
    if spec.format != "yaml":
        raise GreatMindsError(
            f"stand deploy: profile {profile!r} is {spec.format!r}; the "
            "deploy engine requires a YAML/ansible profile (MD profiles "
            "are LLM-executed prose, not deterministically deployable)",
            exit_code=2)

    # 0363 (GitHub #9): the lease state-file carries no host (the lease CLI
    # takes only --task/--worktree/--profile), so coordd-driven deploys
    # reached the executor with host=None. Resolve a host here — lease value
    # (if a future --host ever sets it) > profile YAML (``vars.deploy_host``)
    # > profile-name default — so is_deploy_safe + the deploy marker see a
    # concrete target instead of an empty string. Host is NOT forwarded to
    # ansible (the host-agnostic executor drops it; topology comes from
    # PROJECT.env / add_host); it is greatminds metadata for the safety
    # classifier + deploy evidence only.
    resolved_host = cap.get("host") or getattr(spec, "host", None) or profile
    lease_meta: dict[str, Any] = {
        "coord": str(coord),
        "lease_id": cap.get("lease_id"),
        "profile": profile,
        # issue #12: record which tree the deployed profile came from so the
        # deploy evidence (marker + logs) proves the worktree copy ran.
        "profile_source": spec.source,
        "profile_path": str(spec.path),
        "worktree": lease_worktree,
        "host": resolved_host,
        "task_id": cap.get("task"),
        "task": cap.get("task"),
    }
    for k in ("ansible_become", "deploy_prerequisites_only"):
        if k in cap:
            lease_meta[k] = cap[k]

    rc, log = dispatch_profile(spec, lease_meta,
                               ansible_playbook=ansible_playbook,
                               timeout_seconds=timeout_seconds)
    lid = cap.get("lease_id")
    if rc == 0:
        ready_cap: dict[str, Any] = {}

        def _ready(state):
            active = state.get("active_lease") or {}
            active["ready_at"] = ss.now_iso()
            ready_cap["holder"] = active.get("holder_role", "")
            ready_cap["task"] = active.get("task", "")
            ss.record_transition(
                state, "preparing", "ready", "COORDD", lease_id=lid,
                reason=f"deploy ok (profile {profile!r} from {spec.source})")

        ss.update_stand_state(coord, _ready)
        if ready_cap.get("holder"):
            _file_inbox_info(
                coord, ready_cap["holder"],
                f"stand lease {lid} ready; "
                f"task={ready_cap.get('task', '?')}; "
                f"profile={profile!r} from {spec.source}",
                task_ref=ready_cap.get("task", ""))
    else:
        reason = f"deploy rc={rc}: {(log or '').strip()[:400]}"

        def _down(state):
            prev = state.get("state") or "preparing"
            state["down_reason"] = reason
            state["active_lease"] = None
            ss.record_transition(state, prev, "down", "COORDD",
                                 lease_id=lid, reason=reason)

        ss.update_stand_state(coord, _down)
    return rc, (log or "")


@stand.command(name="deploy")
@click.option("--lease-id", "lease_id", required=True,
              help="active lease to deploy (must match state.yaml)")
@click.option("--timeout", "timeout", type=float, default=None,
              help="kill the playbook after N seconds (rc=124)")
def stand_deploy(lease_id: str, timeout: float | None) -> None:
    """Run the active lease's deploy profile and transition ready/down.

    Coordd runs this automatically for a preparing lease; this command is the
    manual/operator entry to the same deploy engine.
    """
    coord = find_coord_dir()
    rc, _log = deploy_lease(coord, lease_id=lease_id, timeout_seconds=timeout)
    click.echo(
        f"deploy rc={rc}; stand → {'ready' if rc == 0 else 'down'} "
        f"(lease {lease_id})")
    if rc != 0:
        raise GreatMindsError(
            f"deploy failed rc={rc}; stand → down",
            exit_code=(rc if 0 < rc < 256 else 1))


@stand.command(name="ready")
@click.option("--lease-id", "lease_id", required=True,
              help="lease that just finished preparing")
def stand_ready(lease_id: str) -> None:
    """Transition a prepared lease to ready and notify the holder.

    Refuses the transition unless a deploy marker exists at
    ``<coord>/.stand/deploy-<lease_id>.log``. The marker proves coordd
    invoked the YAML deploy profile before setting the lease ready.
    """
    from greatminds.cli import stand_state as ss
    from greatminds.cli.stand_executor import deploy_marker_path

    role = (os.environ.get("GREATMINDS_ROLE") or "OPERATOR").upper()
    coord = find_coord_dir()

    # 0286 gate: marker must exist for this lease_id BEFORE we touch
    # state.yaml. Skipping ansible and calling `stand ready` was the
    # bug — this check removes the foot-gun at the CLI surface.
    marker = deploy_marker_path(coord, lease_id)
    if not marker.is_file():
        raise GreatMindsError(
            f"stand ready refused: no deploy marker at {marker}. "
            "SK must invoke execute_yaml_profile (or execute_md_profile) "
            "via stand_executor.dispatch_profile BEFORE stand ready. "
            "The marker proves the deploy actually ran instead of "
            "short-circuiting to ready.",
            exit_code=2,
        )

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
    """Print the singleton stand resource state.

    Reads ``coordination/.stand/state.yaml`` (creates a synthetic
    empty-state view when the file doesn't exist yet) and prints a
    compact human-readable summary: state, active lease (if any),
    queue contents, and the last few transitions.

    Read-only. Use `stand lease`, `stand ready`, `stand release`, `stand down`,
    and `stand up` for mutations.
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
