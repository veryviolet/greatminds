"""0243 (0242a / 0242 Phase 1): stand resource state file IO.

Singleton stand resource per project. State lives in
``.greatminds/.stand/state.yaml`` as the FSM source of truth:

    state: free | preparing | ready | down
    active_lease:        # null when state == free | down
      lease_id: <uuid>
      task: <task-id>
      worktree: <path>
      holder_role: <ROLE>
      granted_at: <ISO>
      ready_at: <ISO|null>
      ttl_seconds: 14400
    queue:               # FIFO of pending lease requests
      - lease_id: <uuid>
        task: <task-id>
        worktree: <path>
        holder_role: <ROLE>
        enqueued_at: <ISO>
    last_state_change_at: <ISO>
    last_state_change_by: <ROLE>
    down_reason: <string|null>
    history:             # last N transitions for status tail
      - t: <ISO>
        from: <state>
        to: <state>
        by: <ROLE>
        lease_id: <uuid|null>
        reason: <string|null>

Phase 1 (this module) is read-only from the operator's POV — the
mutating CLI (``stand lease`` / ``stand release``) lands in 0244.
But the IO helpers below already support write-with-fcntl for use
by 0244 + tests.

Mutations always: open(fd, O_RDWR | O_CREAT) → fcntl.LOCK_EX →
read → mutate dict → seek 0 + truncate + write → fsync → unlock.
The lock guards against TOCTOU races between concurrent agents.
"""
from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from greatminds.core.errors import GreatMindsError


STAND_STATE_DIR = ".stand"
STAND_STATE_FILE = "state.yaml"
HISTORY_TAIL_LEN = 20

VALID_STATES = ("free", "preparing", "ready", "down")


def _empty_state() -> dict[str, Any]:
    """0243: initial state for a fresh project — no lease, empty
    queue, state=free. Returned by ``read_stand_state`` when the
    state file doesn't exist yet."""
    return {
        "state": "free",
        "active_lease": None,
        "queue": [],
        "last_state_change_at": None,
        "last_state_change_by": None,
        "down_reason": None,
        "history": [],
    }


def state_file_path(coord: Path) -> Path:
    """Resolved absolute path to ``.greatminds/.stand/state.yaml``."""
    return coord / STAND_STATE_DIR / STAND_STATE_FILE


def read_stand_state(coord: Path) -> dict[str, Any]:
    """0243: read the stand state without acquiring the write lock.

    Safe for read-only consumers (``stand status``, watchdog).
    Returns an empty-state dict when the file doesn't exist (fresh
    project / pre-0242 fleet) so callers don't need to special-case
    the bootstrap.
    """
    sp = state_file_path(coord)
    if not sp.is_file():
        return _empty_state()
    try:
        with sp.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"stand state file: {exc}")
    if not isinstance(doc, dict):
        raise GreatMindsError(
            f"stand state file: top-level must be mapping; got "
            f"{type(doc).__name__}"
        )
    # Defensive: fill missing keys so callers always see the full
    # shape (file might be old / partially-written).
    base = _empty_state()
    base.update(doc)
    if not isinstance(base.get("queue"), list):
        base["queue"] = []
    if not isinstance(base.get("history"), list):
        base["history"] = []
    return base


def update_stand_state(coord: Path,
                       mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """0243: read-modify-write the state file under fcntl.LOCK_EX.

    ``mutator`` receives the current state dict and mutates it in
    place (must return None or the mutated dict). Lock acquisition
    is blocking so two concurrent CLI invocations serialize
    deterministically.

    Returns the post-mutation state for the caller's convenience.
    """
    sp = state_file_path(coord)
    sp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(sp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            existing_bytes = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                existing_bytes += chunk
            if existing_bytes:
                try:
                    doc = yaml.safe_load(existing_bytes.decode("utf-8")) or {}
                except yaml.YAMLError as exc:
                    raise GreatMindsError(
                        f"stand state file parse: {exc}"
                    )
                if not isinstance(doc, dict):
                    raise GreatMindsError(
                        "stand state file: top-level must be mapping"
                    )
            else:
                doc = {}
            base = _empty_state()
            base.update(doc)
            if not isinstance(base.get("queue"), list):
                base["queue"] = []
            if not isinstance(base.get("history"), list):
                base["history"] = []

            ret = mutator(base)
            new_state = ret if isinstance(ret, dict) else base

            if new_state.get("state") not in VALID_STATES:
                raise GreatMindsError(
                    f"stand state: {new_state.get('state')!r} not in "
                    f"{list(VALID_STATES)}"
                )

            # Truncate before write (file may shrink).
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            payload = yaml.safe_dump(new_state, sort_keys=False,
                                     allow_unicode=True)
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
            return new_state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def now_iso() -> str:
    """Stable ISO timestamp (UTC, second precision) for state-file
    entries. Local imports avoid pulling util into module-load time."""
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def record_transition(state: dict[str, Any], from_s: str, to_s: str,
                      by_role: str, lease_id: str | None = None,
                      reason: str | None = None) -> None:
    """0243: append a transition to ``state['history']`` (bounded
    to ``HISTORY_TAIL_LEN`` entries) + update last_state_change_*.

    Mutator-friendly: call from inside ``update_stand_state``'s
    callback.
    """
    entry = {
        "t": now_iso(),
        "from": from_s,
        "to": to_s,
        "by": by_role,
        "lease_id": lease_id,
        "reason": reason,
    }
    history = state.get("history") or []
    history.append(entry)
    state["history"] = history[-HISTORY_TAIL_LEN:]
    state["state"] = to_s
    state["last_state_change_at"] = entry["t"]
    state["last_state_change_by"] = by_role


def promote_head_on_free(state: dict[str, Any], by_role: str,
                         reason: str | None = None) -> str | None:
    """0343: auto-promote the next FIFO-queued lease when the stand is
    free.

    ``stand release`` / ``stand up`` transition the singleton to
    ``free`` but historically left a non-empty queue untouched — the
    documented "pops the next FIFO queue entry" never happened, so
    queued validations stalled until someone manually re-leased. Call
    this from inside a mutator AFTER recording the ``→free`` transition:
    if the state is ``free`` and the queue is non-empty, the head entry
    is granted (``granted_at`` now, ``ready_at`` cleared), set as the
    ``active_lease``, removed from the queue, and the stand moves
    ``free→preparing`` so SK deploys it on its next tick without a
    manual re-lease.

    No-op (returns ``None``) when the state isn't ``free`` or the queue
    is empty. Returns the promoted ``lease_id`` otherwise.
    """
    if state.get("state") != "free":
        return None
    queue = state.get("queue") or []
    if not queue:
        return None
    head = dict(queue[0])
    head["granted_at"] = now_iso()
    head["ready_at"] = None
    state["active_lease"] = head
    state["queue"] = list(queue[1:])
    # A fresh grant is a clean slate — never carry a prior incident's
    # down_reason into the promoted lease's preparing cycle.
    state["down_reason"] = None
    record_transition(
        state, "free", "preparing", by_role,
        lease_id=head.get("lease_id"),
        reason=reason or (
            f"auto-promoted queued lease for {head.get('task')}"),
    )
    return head.get("lease_id")
