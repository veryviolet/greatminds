#!/usr/bin/env python3
"""greatminds task — single entry point for all task-file operations.

NEVER edit task files by hand. Use this CLI. It enforces:

  * strict YAML structure (no markdown, no free-form blocks);
  * required fields per stream and per block kind (validated against
    schema.yaml task_kinds);
  * caller-role permission per transition (only the role allowed by
    schema.transitions can mv from a given queue to a given queue);
  * atomic intent → mv → del-intent → journal-append (no half-states);
  * heartbeat side-effect on every successful invocation.

Caller role is taken strictly from ``$GREATMINDS_ROLE`` (set per tmux
window). There is no ``--as`` override: lying about role is not a feature.

Subcommands:
  new           create a new task (intake)
  mv            move task between queues
  append-block  append a typed block to an existing task
  show          pretty-print a task by id
  list          list tasks in a queue
  validate      validate a task file
  paths         print resolved coordination paths

Exit codes (raised via :class:`GreatMindsError`, surfaced by click):
   1  usage / generic
   2  validation failure
   3  permission denied (role not allowed for this transition)
   4  fs / atomicity failure (intent / mv / journal)
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir, find_coord_dir
from greatminds.core.util import ISO_FMT, now_iso  # noqa: F401  (ISO_FMT re-exported)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INTENT_DIR_NAME = "intent"
JOURNAL_NAME = "journal.ndjson"
HEARTBEAT_PREFIX = "heartbeat."
ID_RE = re.compile(r"^[0-9]{4}-[a-z0-9][a-z0-9\-]*$")

# 0174: FSM tables now load from schema.yaml at module-import. The
# constants are populated below, AFTER the schema() function is
# defined. Other call sites read them as plain dicts/sets — that API
# is preserved. Adding a new block_kind / scope / enum becomes a
# schema-only edit; no code change needed for the data parts.

TITLE_MAX_LEN = 200

# B5: ``--in-queue`` is restricted to known intake queues per stream.
# 0258 / 0247 (1.3.0 BREAKING): ``stand`` stream removed. The lease-
# based singleton stand resource replaces stand_requests intake.
ALLOWED_INTAKE_QUEUES: dict[str, set[str]] = {
    "product":         {"feature_inbox", "user_feedback"},
    "review_session":  {"review_sessions"},
}

# Fields whose ``--field key=value`` form is expected to carry a list.
# Anything else is left as a string even if it contains commas (so prose
# values like ``stand_reason="POST /x, then GET /y"`` aren't fragmented).
LIST_FIELDS = {
    "dependencies", "files", "test_files", "hosts", "scenarios",
    "evidence_for", "docs_checked", "bugs_filed", "commands",
    # 0235: tests block (post-0228) requires functional_probes as a
    # non-empty list. Without this entry coerce_value would store a
    # `--field functional_probes=cmd1,cmd2` value as the literal
    # string "cmd1,cmd2"; the 0228 validator then rejects with «not
    # a list», blocking TESTER's mv. tester_observations stays
    # scalar — it's a single text blob per probe-run.
    "functional_probes",
}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_schema_cache: dict[str, Any] | None = None


def schema() -> dict[str, Any]:
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache
    p = find_canon_dir() / "schema.yaml"
    try:
        _schema_cache = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"failed to load schema.yaml: {exc}")
    return _schema_cache


def queue_meta(queue: str) -> dict[str, Any]:
    q = (schema().get("queues") or {}).get(queue)
    if not isinstance(q, dict):
        raise GreatMindsError(f"unknown queue: {queue}")
    return q


def role_meta(role: str) -> dict[str, Any]:
    r = (schema().get("roles") or {}).get(role)
    if not isinstance(r, dict):
        raise GreatMindsError(f"unknown role: {role}")
    return r


def _load_fsm_tables_from_schema() -> dict[str, Any]:
    """0174: build FSM-validation tables from schema.yaml.

    Called once at module-import (right below). If the schema is
    malformed or missing required sections, raises GreatMindsError —
    the CLI cannot function without these tables and silent fallbacks
    would hide breakage.
    """
    doc = schema()

    streams_data = doc.get("streams") or {}
    block_kinds_data = doc.get("block_kinds") or {}
    queue_accepts_data = doc.get("queue_accepts_blocks") or {}
    assignee_data = doc.get("assignee_role_by_scope") or {}
    product_enums = doc.get("product_enums") or {}

    if not streams_data or not block_kinds_data or not queue_accepts_data:
        raise GreatMindsError(
            "schema.yaml missing one of: streams, block_kinds, "
            "queue_accepts_blocks (post-0174 these are required)"
        )

    out: dict[str, Any] = {}
    out["STREAM_BLOCK_KINDS"] = {
        name: set(meta.get("allowed_block_kinds") or [])
        for name, meta in streams_data.items()
    }
    out["BLOCK_KIND_ROLES"] = {
        name: set(meta.get("authored_by") or [])
        for name, meta in block_kinds_data.items()
        if meta and meta.get("authored_by")
    }
    out["IMPL_ROLE_BY_SCOPE"] = dict(assignee_data)
    out["QUEUE_BLOCK_KINDS"] = {
        q: set(blocks or [])
        for q, blocks in queue_accepts_data.items()
    }
    out["PRODUCT_KINDS"] = set(product_enums.get("kinds") or [])
    out["PRODUCT_SCOPES"] = set(product_enums.get("scopes") or [])
    out["PRIORITIES"] = set(product_enums.get("priorities") or [])
    out["PLAN_KINDS"] = set(product_enums.get("plan_kinds") or [])
    out["MODES"] = set(product_enums.get("modes") or [])

    tests_meta = block_kinds_data.get("tests") or {}
    out["TEST_RESULTS"] = set(tests_meta.get("allowed_test_results") or [])
    out["GATE_CHECK_RESULTS"] = set(
        tests_meta.get("allowed_gate_check_results") or [])
    out["REVIEW_OUTCOMES"] = set(
        (block_kinds_data.get("review") or {}).get("allowed_outcomes") or [])
    out["READER_OUTCOMES"] = set(
        (block_kinds_data.get("reader_review") or {}).get("allowed_outcomes")
        or [])
    return out


_FSM = _load_fsm_tables_from_schema()
STREAM_BLOCK_KINDS:  dict[str, set[str]] = _FSM["STREAM_BLOCK_KINDS"]
BLOCK_KIND_ROLES:    dict[str, set[str]] = _FSM["BLOCK_KIND_ROLES"]
IMPL_ROLE_BY_SCOPE:  dict[str, str]      = _FSM["IMPL_ROLE_BY_SCOPE"]
QUEUE_BLOCK_KINDS:   dict[str, set[str]] = _FSM["QUEUE_BLOCK_KINDS"]
PRODUCT_KINDS:       set[str] = _FSM["PRODUCT_KINDS"]
PRODUCT_SCOPES:      set[str] = _FSM["PRODUCT_SCOPES"]
PRIORITIES:          set[str] = _FSM["PRIORITIES"]
PLAN_KINDS:          set[str] = _FSM["PLAN_KINDS"]
MODES:               set[str] = _FSM["MODES"]
TEST_RESULTS:        set[str] = _FSM["TEST_RESULTS"]
GATE_CHECK_RESULTS:  set[str] = _FSM["GATE_CHECK_RESULTS"]
REVIEW_OUTCOMES:     set[str] = _FSM["REVIEW_OUTCOMES"]
READER_OUTCOMES:     set[str] = _FSM["READER_OUTCOMES"]


def transitions_for(from_q: str, to_q: str) -> list[dict[str, Any]]:
    """All schema rows matching the (from, to) pair.

    Multiple rows may share the same ``(from, to)`` with different ``by:``
    roles — e.g. ``review_sessions → archive`` legitimately permits both
    ARCHITECT-PLANNER (intake archive) and EXPLORER (self-close after AC
    campaign). Returning only the first row, as ``transition_for`` did,
    silently blocks any role that appears second in ``schema.yaml``.

    Wildcards resolved here:
      ``from == "any_active_queue"``       → matches any concrete ``from_q``.
      ``to   == "any_resume_to_queue"``    → matches any NON-TERMINAL
          concrete ``to_q``. "Resume" returns a parked task to active
          work; it must never resolve to a terminal queue (archive /
          verified). Letting it match terminal queues was a real FSM
          bug: ``feature_blocked → archive`` then matched BOTH the exact
          withdraw row (requires feature_blocked_withdrawn_reason) AND
          this wildcard (requires all_dependencies_exist_per_wake_check),
          and enforce_schema_requires runs requires from every matching
          row — so the resume path's wake-check fired on the withdraw
          path. A withdrawn task carries a never-resolving sentinel dep
          by design, making archive impossible. The two paths are
          mutually exclusive; the wildcard must stay out of terminals.
    """
    matches: list[dict[str, Any]] = []
    for t in schema().get("transitions") or []:
        if not isinstance(t, dict):
            continue
        f, to = t.get("from"), t.get("to")
        f_ok = (f == from_q) or (f == "any_active_queue")
        resume_ok = (to == "any_resume_to_queue"
                     and not _is_terminal_queue(to_q))
        to_ok = (to == to_q) or resume_ok
        if f_ok and to_ok:
            matches.append(t)
    return matches


def _is_terminal_queue(queue: str) -> bool:
    """True if ``queue`` is a known terminal queue (archive / verified).

    Tolerant of unknown names (returns False) so the resume-wildcard
    matcher never raises on a non-queue ``to_q``.
    """
    q = (schema().get("queues") or {}).get(queue)
    return isinstance(q, dict) and q.get("kind") == "terminal"


def transition_for(from_q: str, to_q: str) -> dict[str, Any] | None:
    """Back-compat singular form: first match or None.

    Prefer ``transitions_for`` in new code that needs role-aware
    disambiguation (see ``can_role_move``).
    """
    matches = transitions_for(from_q, to_q)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------


def caller_role() -> str:
    """Resolved caller role from ``$GREATMINDS_ROLE``, validated against schema."""
    from greatminds.core.paths import caller_role as _bare_caller_role

    role = _bare_caller_role()
    if role not in (schema().get("roles") or {}):
        raise GreatMindsError(f"caller role not in schema: {role}")
    return role


# ---------------------------------------------------------------------------
# Task file I/O
# ---------------------------------------------------------------------------


def task_path_in_queue(coord: Path, queue: str, task_id: str) -> Path | None:
    qdir = coord / queue
    if not qdir.is_dir():
        return None
    yaml_p = qdir / f"{task_id}.yaml"
    md_p = qdir / f"{task_id}.md"
    if yaml_p.is_file():
        return yaml_p
    if md_p.is_file():
        return md_p
    return None


_SEQ_ONLY_RE = re.compile(r"^\d{1,4}$")


def find_task(coord: Path, task_id: str) -> tuple[Path, str] | None:
    """Return ``(path, queue_name)`` where task currently sits, or ``None``.

    0114 unification: accepts THREE id shapes, in priority order:
      1. Full filename stem: ``0109-make-schema-...`` → exact match.
      2. Short numeric id: ``0109`` (1–4 digits) → matches any file
         whose stem starts ``<zero-padded-seq>-``. Disambiguation:
         if multiple matches, returns the first (lex-sorted) and
         logs ambiguity; this matches the de-facto behavior of
         gate_check.find_task_file from before the unification.
      3. Slug prefix: ``0109-make-schema`` → matches any file whose
         stem starts with the given prefix. Same ambiguity rule.

    Scans every coordination subdirectory except ``intent``, ``inbox``,
    ``.locks``, ``.agent_registry``, and any other dot-prefixed dir.

    Pre-0114, only shape (1) worked — short-id lookups silently
    returned None, causing the misleading "task X not found in any
    queue" race-masking error (the 2026-05-25 0097 incident behind
    tasks 0113 / 0114).
    """
    if not coord.is_dir():
        return None
    # 0326: unify id intake — also accept a full filename and a path
    # (absolute, cwd-relative, or coordination-relative) so every
    # subcommand resolves identically. If the arg points at an existing
    # task file, return it directly; otherwise reduce it to the bare
    # stem (strip any directory + ``.yaml``/``.md``) and fall through to
    # the short-id / full-stem / slug-prefix scan below.
    if isinstance(task_id, str) and task_id:
        cand = Path(task_id)
        for p in (cand, coord / cand, coord.parent / cand):
            try:
                if p.is_file() and p.suffix in (".yaml", ".md"):
                    return (p.resolve(), p.parent.name)
            except OSError:
                pass
        if cand.suffix in (".yaml", ".md"):
            task_id = cand.stem
        elif "/" in task_id or os.sep in task_id:
            task_id = cand.name
    seq_only = bool(_SEQ_ONLY_RE.match(task_id))
    seq_prefix = f"{int(task_id):04d}-" if seq_only else None
    exact_candidates: list[tuple[Path, str]] = []
    prefix_candidates: list[tuple[Path, str]] = []
    for q in sorted(coord.iterdir()):
        if not q.is_dir() or q.name.startswith("."):
            continue
        if q.name in ("intent", "inbox"):
            continue
        for f in sorted(q.iterdir()):
            if not f.is_file():
                continue
            if f.suffix not in (".yaml", ".md"):
                continue
            stem = f.stem
            if stem == task_id:
                exact_candidates.append((f, q.name))
            elif seq_only and seq_prefix and stem.startswith(seq_prefix):
                prefix_candidates.append((f, q.name))
            elif not seq_only and stem.startswith(task_id + "-"):
                prefix_candidates.append((f, q.name))
    if exact_candidates:
        return exact_candidates[0]
    if prefix_candidates:
        return prefix_candidates[0]
    return None


def load_task(path: Path) -> dict[str, Any]:
    if path.suffix == ".yaml":
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise GreatMindsError(f"yaml parse error in {path}: {exc}", exit_code=2)
    text = path.read_text(encoding="utf-8")
    return parse_legacy_md(text)


def parse_legacy_md(text: str) -> dict[str, Any]:
    """Best-effort: take the FIRST YAML block as header, leave rest as raw."""
    parts = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    header: dict[str, Any] = {}
    for chunk in parts:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            header = data
            break
    header["_legacy_md"] = True
    header["_legacy_raw"] = text
    return header


def atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML atomically (temp + rename) in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                data,
                f,
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
            )
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Heartbeat + journal + intent
# ---------------------------------------------------------------------------


def touch_heartbeat(coord: Path, role: str) -> None:
    p = coord / f"{HEARTBEAT_PREFIX}{role.lower()}"
    try:
        p.touch()
        now = time.time()
        os.utime(p, (now, now))
    except OSError:
        pass  # heartbeat is best-effort


def journal_append(coord: Path, entry: dict[str, Any]) -> None:
    p = coord / JOURNAL_NAME
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise GreatMindsError(f"journal append failed: {exc}", exit_code=4)


def intent_write(coord: Path, role: str, task_id: str,
                 from_q: str, to_q: str, reason: str) -> Path:
    idir = coord / INTENT_DIR_NAME
    idir.mkdir(parents=True, exist_ok=True)
    intent_id = uuid.uuid4().hex
    p = idir / f"{task_id}-{role.lower()}-{intent_id}.json"
    data = {
        "task_id": task_id,
        "from": f"coordination/{from_q}/" if from_q else "",
        "to": f"coordination/{to_q}/" if to_q else "",
        "role": role,
        "intent_id": intent_id,
        "intent_at": now_iso(),
        "reason": reason or "",
    }
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def intent_clear(intent_path: Path) -> None:
    try:
        intent_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-task file lock (serialise concurrent append-block / mv on same id)
# ---------------------------------------------------------------------------


TASK_FILE_LOCK_TIMEOUT_SEC = 30.0
TASK_FILE_LOCK_POLL_SEC = 0.1
# 0185: per-source-file lock machinery (0115/0166) removed.
# Replaced by per-task git worktree isolation — two tasks cannot
# contaminate each other's working tree because each tasks edits in
# its own ``.worktrees/<task-id>/`` directory. See cli/worktree.py.



@contextmanager
def task_file_lock(coord: Path, task_id: str,
                   timeout: float = TASK_FILE_LOCK_TIMEOUT_SEC,
                   poll_interval: float = TASK_FILE_LOCK_POLL_SEC):
    """Exclusive per-task-id flock for read-modify-write on a single
    task file (used by ``mv`` and ``append-block``).

    Non-blocking acquire with a polling retry loop up to ``timeout``
    seconds. On timeout, raises ``GreatMindsError`` naming the holder
    PID (read from the lock file content). The kernel auto-releases
    flock on holder process exit — no explicit stale-pid cleanup
    required.

    While the lock is held, the lock file content is the holder PID
    (decimal ASCII). Other waiters read this on timeout to produce a
    diagnostic message.
    """
    lock_dir = coord / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{task_id}.lock"
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        holder = lock_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        holder = ""
                    holder_desc = f"pid {holder}" if holder else "unknown pid"
                    raise GreatMindsError(
                        f"task {task_id} is being transitioned by "
                        f"{holder_desc}; waited {int(timeout)}s "
                        f"(lock at {lock_path}). Retry after the holder "
                        f"releases, or investigate if the pid is stuck.",
                        exit_code=4,
                    )
                time.sleep(poll_interval)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass
        yield
    finally:
        if acquired:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def must_enum(field: str, value: Any, allowed: set[str]) -> None:
    if value not in allowed:
        raise GreatMindsError(
            f"field '{field}' must be one of {sorted(allowed)}, got: {value!r}"
        , exit_code=2)


def must_str(field: str, value: Any, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise GreatMindsError(f"field '{field}' must be a non-empty string", exit_code=2)


def must_bool(field: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise GreatMindsError(f"field '{field}' must be true|false", exit_code=2)


def must_list_of_str(field: str, value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise GreatMindsError(f"field '{field}' must be a list of strings", exit_code=2)


def must_iso(field: str, value: Any) -> None:
    if not isinstance(value, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value
    ):
        raise GreatMindsError(f"field '{field}' must be ISO-8601 (got: {value!r})", exit_code=2)


def must_id(value: Any) -> None:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise GreatMindsError(f"id must match {ID_RE.pattern} (got: {value!r})", exit_code=2)


def validate_header(data: dict[str, Any]) -> None:
    stream = data.get("stream")
    # 0258 / 0247 (1.3.0 BREAKING): ``stand`` stream removed; use the
    # lease API via ``greatminds stand lease``.
    if stream not in ("product", "review_session"):
        raise GreatMindsError(
            f"stream must be product|review_session, got: {stream!r}"
        , exit_code=2)
    must_id(data.get("id"))
    must_str("title", data.get("title"))
    must_str("reporter", data.get("reporter"))
    must_iso("opened_at", data.get("opened_at"))
    must_enum("priority", data.get("priority"), PRIORITIES)

    if stream == "product":
        must_enum("kind", data.get("kind"), PRODUCT_KINDS)
        must_enum("scope", data.get("scope"), PRODUCT_SCOPES)
    elif stream == "review_session":
        if data.get("kind") != "review_session":
            raise GreatMindsError(
                "review_session-stream tasks must have kind: review_session"
            , exit_code=2)
        must_enum("mode", data.get("mode"), MODES)
        must_str("target_functionality", data.get("target_functionality"))
        scen = data.get("scenarios")
        if not isinstance(scen, list) or not scen:
            raise GreatMindsError("scenarios must be a non-empty list", exit_code=2)


def validate_block(stream: str, block: dict[str, Any]) -> None:
    kind = block.get("kind")
    allowed = STREAM_BLOCK_KINDS.get(stream, set())
    if kind not in allowed:
        raise GreatMindsError(
            f"block kind {kind!r} not allowed in stream {stream!r}; "
            f"allowed: {sorted(allowed)}"
        , exit_code=2)
    must_str("block.by", block.get("by"))
    must_iso("block.at", block.get("at"))
    if kind == "plan":
        must_str("base_commit", block.get("base_commit"))
        must_str("assignee_role", block.get("assignee_role"))
        must_bool("stand_required", block.get("stand_required"))
        must_enum("plan_kind", block.get("plan_kind"), PLAN_KINDS)
        must_enum("mode", block.get("mode"), MODES)
        must_bool("ready_for_implementation", block.get("ready_for_implementation"))
        # A3: stand_required must be justified.
        if block.get("stand_required") is True and not (block.get("stand_reason") or "").strip():
            raise GreatMindsError(
                "plan with stand_required=true must include stand_reason"
            , exit_code=2)
    elif kind == "implementation":
        must_str("base_commit", block.get("base_commit"))
        must_list_of_str("files", block.get("files") or [])
        must_bool("ready_for_test", block.get("ready_for_test"))
    elif kind == "tests":
        must_str("base_commit", block.get("base_commit"))
        must_list_of_str("test_files", block.get("test_files") or [])
        # A1: TESTER must list at least one test file.
        if not (block.get("test_files") or []):
            raise GreatMindsError(
                "tests block requires at least one entry in test_files"
            , exit_code=2)
        must_str("test_command", block.get("test_command"))
        must_enum("test_result", block.get("test_result"), TEST_RESULTS)
        must_enum("gate_check_result", block.get("gate_check_result"), GATE_CHECK_RESULTS)
        must_iso("gate_check_at", block.get("gate_check_at"))
        must_str("gate_check_commit", block.get("gate_check_commit"))
        must_bool("ready_for_review", block.get("ready_for_review"))
    elif kind == "reader_review":
        must_enum("outcome", block.get("outcome"), READER_OUTCOMES)
        must_bool("stand_checked", block.get("stand_checked"))
        must_bool("ready_for_architect", block.get("ready_for_architect"))
    elif kind == "review":
        must_enum("outcome", block.get("outcome"), REVIEW_OUTCOMES)
        # commit is only required on approval; changes_requested is a
        # hand-back, nothing was committed.
        if block.get("outcome") == "approved":
            must_str("commit", block.get("commit"))
    elif kind == "rollback":
        # 0195: rollback block — REVIEWER's withdraw/revisit marker
        # for a task in verified/. Must carry non-empty reason.
        reason = block.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise GreatMindsError(
                "rollback block requires non-empty 'reason' field",
                exit_code=2,
            )
    elif kind == "blocked":
        must_str("reason", block.get("reason"))
        deps = block.get("dependencies") or []
        must_list_of_str("dependencies", deps)
        if not deps:
            raise GreatMindsError("blocked block requires at least one dependency", exit_code=2)
        dep_re = re.compile(r"^([a-z_]+)/([0-9]{1,4}-[a-z0-9-]+)\.(yaml|md)$")
        known_queues = set((schema().get("queues") or {}).keys())
        for d in deps:
            m = dep_re.match(d)
            if m is None:
                raise GreatMindsError(
                    f"dependency {d!r} must look like <queue>/<id>.{{yaml,md}}"
                , exit_code=2)
            if m.group(1) not in known_queues:
                raise GreatMindsError(
                    f"dependency {d!r}: unknown queue {m.group(1)!r}"
                , exit_code=2)
        must_str("resume_to", block.get("resume_to"))
        if block.get("resume_to") not in known_queues:
            raise GreatMindsError(
                f"resume_to: {block.get('resume_to')!r} is not a known queue"
            , exit_code=2)
    # 0247 (1.3.0): stand_result block kind removed. The lease-based
    # singleton stand resource stores release evidence on the
    # product task's tests block directly (tests.stand_evidence with
    # lease_id / result / commit fields). No more per-stand task
    # files, so no per-block schema field validation needed.
    elif kind == "session_iteration":
        must_str("summary", block.get("summary"))
    elif kind == "triage":
        pass  # only needs by/at/notes


def validate_task(data: dict[str, Any]) -> None:
    validate_header(data)
    blocks = data.get("blocks") or []
    if not isinstance(blocks, list):
        raise GreatMindsError("blocks: must be a list", exit_code=2)
    stream = data["stream"]
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            raise GreatMindsError(f"blocks[{i}] must be a mapping", exit_code=2)
        validate_block(stream, b)


# ---------------------------------------------------------------------------
# Permission: can this role move from_q → to_q?
# ---------------------------------------------------------------------------


def can_role_move(role: str, from_q: str, to_q: str,
                  task_data: dict[str, Any]) -> str | None:
    """Return ``None`` if allowed, otherwise an error message string.

    Iterates ALL schema rows matching ``(from_q, to_q)`` and authorizes
    if any one of them is satisfied. Handles two row shapes:

      - ``by: <ROLE>``: succeeds if ``role == by``.
      - ``by: current_owner``: succeeds if ``role`` owns or writes the
        ``from_q`` queue.

    If no row authorizes ``role``, the error lists the union of permitted
    ``by:`` roles for that pair (joined with " or " in sorted order), so
    the caller sees every legal alternative rather than just the first.
    """
    matches = transitions_for(from_q, to_q)
    if not matches:
        return f"no transition {from_q} → {to_q} in schema"

    for t in matches:
        by = t.get("by")
        if by == "current_owner":
            owner = (queue_meta(from_q).get("owner") or "").upper()
            writers = queue_meta(from_q).get("writers") or []
            if not owner or owner == role or role in writers:
                return None
            # This `current_owner` row rejects `role`; another row may still authorize.
            continue
        if isinstance(by, str) and by == role:
            return None

    permitted = sorted({
        str(t.get("by")) for t in matches if isinstance(t.get("by"), str)
    })
    if not permitted:
        # All matches were current_owner-only and `role` failed ownership.
        owner = (queue_meta(from_q).get("owner") or "").upper()
        return f"role {role} is not current owner of {from_q} (owner: {owner})"
    permitted_str = " or ".join(permitted)
    return f"only role {permitted_str} may perform {from_q} → {to_q}, not {role}"


# ---------------------------------------------------------------------------
# Routing / readiness preconditions
# ---------------------------------------------------------------------------


READY_FLAG_PER_TARGET: dict[str, tuple[str, str]] = {
    "feature_dev":    ("plan", "ready_for_implementation"),
    "feature_ui_dev": ("plan", "ready_for_implementation"),
    "feature_docs":   ("plan", "ready_for_implementation"),
    "feature_test":   ("implementation", "ready_for_test"),
    # feature_docs_review and feature_review are dual-source — handled
    # explicitly in require_target_readiness().
}

# F3: routing from feature_plan → per-scope queue requires scope match.
SCOPE_TO_QUEUE: dict[str, str] = {
    "backend": "feature_dev",
    "ui":      "feature_ui_dev",
    "docs":    "feature_docs",
}


def latest_plan(data: dict[str, Any]) -> dict[str, Any] | None:
    plans = [b for b in (data.get("blocks") or [])
             if isinstance(b, dict) and b.get("kind") == "plan"]
    return plans[-1] if plans else None


def is_audit_only(data: dict[str, Any]) -> bool:
    p = latest_plan(data)
    return bool(p and p.get("audit_only") is True)


def is_interactive_task(data: dict[str, Any]) -> bool:
    """LIVE-DEVELOPER routing marker: the latest plan sets
    ``interactive: true`` (USER asked to work the task live)."""
    p = latest_plan(data)
    return bool(p and p.get("interactive") is True)


def is_verify_only(data: dict[str, Any]) -> bool:
    """No-code stand/playbook verification marker: the latest plan sets
    ``verify_only: true``. Such a task produces NO implementation — it
    routes feature_plan → feature_test directly so TESTER leases a stand,
    runs the playbook/probe, and records stand evidence (mirrors the
    READER ``audit_only`` path that skips the implementer)."""
    p = latest_plan(data)
    return bool(p and p.get("verify_only") is True)


def require_target_readiness(data: dict[str, Any],
                             from_q: str, to_q: str) -> None:
    """Some transitions require the latest block to set a ready_for_* flag."""
    # B1: triage block required before mv inbox → feature_plan, so the
    # intake step is auditable.
    if to_q == "feature_plan" and from_q == "feature_inbox":
        blocks = data.get("blocks") or []
        if not any(isinstance(b, dict) and b.get("kind") == "triage" for b in blocks):
            raise GreatMindsError(
                "mv feature_inbox → feature_plan requires a triage block first"
            , exit_code=2)

    # An audit-only task must never be routed into feature_docs (WRITER's
    # queue) — it has no write-plan.
    if to_q == "feature_docs" and is_audit_only(data):
        raise GreatMindsError(
            "audit-only task: do NOT route the audit into feature_docs. "
            "Its findings become a separate feature_docs write task "
            "(PLANNER triages). The audit flows feature_docs_review → "
            "feature_review → verified."
        , exit_code=2)

    if to_q == "feature_docs_review":
        if from_q == "feature_docs":
            block_kind, flag = "implementation", "ready_for_test"
        elif from_q == "feature_plan":
            p = latest_plan(data)
            if not (p and p.get("audit_only") is True):
                raise GreatMindsError(
                    "mv feature_plan → feature_docs_review requires the "
                    "latest plan block to set audit_only: true (this is "
                    "the independent READER-audit path)"
                , exit_code=2)
            if not p.get("ready_for_implementation"):
                raise GreatMindsError(
                    "mv feature_plan → feature_docs_review requires "
                    "plan.ready_for_implementation=true"
                , exit_code=2)
            return
        elif from_q == "feature_blocked":
            return
        else:
            raise GreatMindsError(f"mv {from_q} → feature_docs_review not allowed", exit_code=2)
        blocks = data.get("blocks") or []
        matching = [b for b in blocks
                    if isinstance(b, dict) and b.get("kind") == block_kind]
        if not matching:
            raise GreatMindsError(
                f"mv → feature_docs_review (from {from_q}) requires "
                f"{block_kind} block"
            , exit_code=2)
        if not matching[-1].get(flag):
            raise GreatMindsError(
                f"mv → feature_docs_review (from {from_q}) requires "
                f"{block_kind}.{flag}=true"
            , exit_code=2)
        return

    if to_q == "feature_review":
        if from_q == "feature_test":
            block_kind, flag = "tests", "ready_for_review"
        elif from_q == "feature_docs_review":
            block_kind, flag = "reader_review", "ready_for_architect"
        elif from_q == "feature_blocked":
            return
        elif from_q == "verified":
            # 0195: verified → feature_review (revisit path) is gated
            # by the schema's `rollback_block_with_reason` validator,
            # not by ready-for-review/ready-for-architect flags. Let
            # the schema-requires check do its job.
            return
        else:
            raise GreatMindsError(
                f"mv {from_q} → feature_review not allowed; route via "
                f"feature_test or feature_docs_review"
            , exit_code=2)
        blocks = data.get("blocks") or []
        matching = [b for b in blocks
                    if isinstance(b, dict) and b.get("kind") == block_kind]
        if not matching:
            raise GreatMindsError(
                f"mv → feature_review (from {from_q}) requires {block_kind} block"
            , exit_code=2)
        if not matching[-1].get(flag):
            raise GreatMindsError(
                f"mv → feature_review (from {from_q}) requires "
                f"{block_kind}.{flag}=true"
            , exit_code=2)
        return

    # A verify-only task (plan.verify_only: true) has NO implementer
    # step — it routes feature_plan → feature_test directly so TESTER
    # leases a stand, runs the playbook/probe, and records stand
    # evidence. Bypass the implementation.ready_for_test gate (there is
    # no implementation block); still require the plan to be marked
    # ready_for_implementation. Mirrors the audit_only feature_docs_review
    # path. (feature_dev/feature_ui_dev → feature_test still hit the
    # generic implementation gate below.)
    if to_q == "feature_test" and from_q == "feature_plan":
        p = latest_plan(data)
        if not (p and p.get("verify_only") is True):
            raise GreatMindsError(
                "mv feature_plan → feature_test requires the latest plan "
                "block to set verify_only: true (the no-code stand/playbook "
                "verification path — TESTER leases a stand and records "
                "evidence; otherwise route via an implementer queue and "
                "advance with an implementation block)"
            , exit_code=2)
        if not p.get("ready_for_implementation"):
            raise GreatMindsError(
                "mv feature_plan → feature_test (verify_only) requires "
                "plan.ready_for_implementation=true"
            , exit_code=2)
        return

    rule = READY_FLAG_PER_TARGET.get(to_q)
    if not rule:
        return
    block_kind, flag = rule
    blocks = data.get("blocks") or []
    matching = [b for b in blocks
                if isinstance(b, dict) and b.get("kind") == block_kind]
    if not matching:
        raise GreatMindsError(f"mv → {to_q} requires {block_kind} block on task", exit_code=2)
    if not matching[-1].get(flag):
        raise GreatMindsError(f"mv → {to_q} requires {block_kind}.{flag}=true", exit_code=2)


def require_scope_match_on_routing(data: dict[str, Any],
                                   from_q: str, to_q: str) -> None:
    if from_q != "feature_plan":
        return
    if to_q not in SCOPE_TO_QUEUE.values():
        return
    scope = data.get("scope")
    expected = SCOPE_TO_QUEUE.get(scope)
    if expected is None:
        raise GreatMindsError(
            f"task scope {scope!r} has no per-scope queue routing"
        , exit_code=2)
    if expected != to_q:
        raise GreatMindsError(
            f"task scope: {scope!r} routes to {expected}, not {to_q}"
        , exit_code=2)


# ---------------------------------------------------------------------------
# Schema requires-validator (task 0103)
#
# transitions[].requires used to be marked "DOCUMENTARY" in schema.yaml —
# the field was never parsed. Result: schema and code could drift, and
# new require keys added to schema would be silently no-ops (the FSM-hole
# class that 0102 / 0105 want to use to close further holes).
#
# 0103 makes the field load-bearing:
#   - SCHEMA_REQUIRES_VALIDATORS maps each known require name to a
#     callable returning None (ok) or an error message string.
#   - enforce_schema_requires() looks up the (from, to, by) row, iterates
#     its `requires:` list, and runs each validator.
#   - An unknown require name is itself an error ("schema requires key
#     'X' has no registered validator"). This prevents anyone (including
#     0102/0105 follow-ups) from adding a documentary key by accident.
#
# Strategy: most existing require names (triage_block, plan_block, etc.)
# are still enforced by the pre-existing `require_target_readiness()`
# function — their entries here just record the name as known and return
# None. The single name that needed REAL implementation per the task
# body is `gate_check_pass_if_stand_required`, which now invokes the
# gate-check logic (not just reads tests.gate_check_result from the
# block) so TESTER cannot bypass the gate by writing pass into the
# field manually.
# ---------------------------------------------------------------------------


def _evaluate_gate_check(task_data: dict[str, Any]) -> str:
    """Run the gate-check logic against a task dict, return 'pass' |
    'fail' | 'missing' | 'n/a' as a string (matching the CLI's exit
    codes 0 / 1 / 2 / 0)."""
    from greatminds.cli import gate_check as gc_mod

    plan = latest_plan(task_data)
    if not isinstance(plan, dict):
        return "missing"
    stand_required = plan.get("stand_required")
    if stand_required is False or stand_required is None:
        return "n/a"
    if stand_required is not True:
        return "missing"

    task_id_full = task_data.get("id") or ""
    if not task_id_full:
        return "missing"

    project_dir = Path.cwd()
    try:
        from greatminds.core.paths import find_coord_dir
        project_dir = find_coord_dir().parent
    except Exception:
        pass

    # 0268 / 0246: prefer lease evidence carried on the tests block
    # (post-1.3.0 path) and fall back to the legacy stand_done/ scan
    # only for tasks shipped before the lease API. ``find_stand_evidence``
    # reads ``coordination/stand_done/`` which is empty for fresh fleets
    # (0247 BREAKING removed the queue model), so without this branch
    # every well-formed post-1.3.0 task would short-circuit to "missing"
    # and block the feature_test → feature_review mv.
    #
    # ``extract_lease_evidence_from_tests`` consumes the merged
    # ``parse_task_file`` shape (latest tests block lifted to
    # ``merged['tests']``); validator data carries the raw blocks list,
    # so we wrap the latest tests block in that shape.
    tests_blocks = [b for b in (task_data.get("blocks") or [])
                    if isinstance(b, dict) and b.get("kind") == "tests"]
    latest_tests = tests_blocks[-1] if tests_blocks else None
    lease_evidence = gc_mod.extract_lease_evidence_from_tests(
        {"tests": latest_tests} if latest_tests else {}
    )
    if lease_evidence is not None:
        synth_path = type("SyntheticPath", (),
                          {"name": "tests.stand_evidence"})()
        candidates: list[tuple[Any, dict]] = [(synth_path, lease_evidence)]
    else:
        candidates = gc_mod.find_stand_evidence(project_dir, str(task_id_full))
        if not candidates:
            return "missing"

    # Replicate gate_check.gate_check()'s pass/fail logic on the merged
    # task data we already have in-hand (no need to re-parse the task
    # file — we're called mid-mv after the task is loaded).
    impl_blocks = [b for b in (task_data.get("blocks") or [])
                   if isinstance(b, dict) and b.get("kind") == "implementation"]
    task_commit = None
    if impl_blocks:
        task_commit = impl_blocks[-1].get("base_commit")
    if not task_commit and plan:
        task_commit = plan.get("base_commit")

    for path, sr in candidates:
        result = sr.get("result")
        sr_commit = sr.get("commit")
        if result not in ("pass", "ok"):
            continue
        if task_commit and sr_commit and not str(sr_commit).startswith(str(task_commit)) \
                and not str(task_commit).startswith(str(sr_commit)):
            continue
        return "pass"
    return "fail"


def _check_gate_for_stand_required(data: dict[str, Any],
                                   from_q: str, to_q: str) -> str | None:
    """Real check for ``gate_check_pass_if_stand_required``.

    The pre-0103 hardcoded logic in require_target_readiness only looked
    at the tests block's ``gate_check_result`` field — which TESTER could
    set to ``pass`` manually without running ``greatminds gate-check``.
    This validator actually evaluates the gate-check rule (stand_done
    file presence + matching commit + result in {pass,ok}) the same way
    the CLI command does, so the field cannot be forged.
    """
    plan = latest_plan(data)
    if not isinstance(plan, dict):
        return None  # No plan → other validators catch this case.
    if plan.get("stand_required") is not True:
        return None  # Gate doesn't apply.
    # Sprint carve-out: a LIVE-DEVELOPER task is validated live by the
    # USER and approved by REVIEWER's no-regression review (outcome
    # approved_sprint). That review IS the gate — there is no TESTER
    # gate-check evidence to evaluate, so the standard gate doesn't apply.
    reviews = [b for b in (data.get("blocks") or [])
               if isinstance(b, dict) and b.get("kind") == "review"]
    if reviews and reviews[-1].get("outcome") == "approved_sprint":
        return None
    result = _evaluate_gate_check(data)
    if result == "pass":
        return None
    return (
        f"gate_check_pass_if_stand_required: gate_check returns {result!r} "
        f"(stand_done evidence missing / wrong commit / result not pass)"
    )


# Validators that return None — these names are still enforced by the
# pre-existing `require_target_readiness()` function. Recording them
# here keeps the registry the single source of truth for "is this
# require name known to the validator layer".
def _noop_existing(data: dict[str, Any], from_q: str, to_q: str) -> str | None:
    return None


# 0247 (1.3.0): _check_stand_result_block removed alongside the
# stand_wip → stand_done transition (queue itself dropped). Lease-
# based release evidence lives on the product task's tests block,
# validated by gate_check (0246).


def _check_triage_block(data: dict[str, Any],
                         from_q: str, to_q: str) -> str | None:
    """0222: user_feedback → {feature_inbox, archive} must carry a
    ``triage`` block authored by ARCHITECT-PLANNER with non-empty
    notes/body.

    Pre-0222 the schema row carried ``requires: [triage_block]`` but
    the registry mapped it to ``_noop_existing`` — the gate was
    documentary, not enforced. EXPLORER's stand_done/0205 ran a
    blocks=[] task through these transitions and watched the mv
    succeed. This validator closes the hole.

    Latest-wins: a task may accumulate triage blocks across re-
    triage iterations; the LATEST one decides whether the mv is
    allowed.
    """
    blocks = data.get("blocks") or []
    triages = [b for b in blocks
               if isinstance(b, dict) and b.get("kind") == "triage"]
    if not triages:
        return (
            "triage_block: user_feedback → {feature_inbox, archive} "
            "requires a triage block. Append "
            "`greatminds task append-block triage --id <X> --field "
            "by=ARCHITECT-PLANNER --field notes='<triage outcome>'` "
            "before mv."
        )
    latest = triages[-1]
    notes = (latest.get("notes") or latest.get("body") or "")
    if not (isinstance(notes, str) and notes.strip()):
        return (
            "triage_block: latest triage block has empty notes/body. "
            "Append a fresh triage block with a non-empty notes "
            "field stating the routing decision."
        )
    return None


def _check_reader_block_pass(data: dict[str, Any],
                              from_q: str, to_q: str) -> str | None:
    """0222: feature_docs_review → feature_review must carry a
    ``reader_review`` block whose latest entry has
    ``outcome in {pass, approved}``.

    Pre-0222 mapped to ``_noop_existing`` — READER could mv with
    outcome=fail and the FSM would accept. EXPLORER's stand_done/0205
    showed this transition accepting reader_review.outcome='fail'.
    Latest-wins semantics."""
    blocks = data.get("blocks") or []
    readers = [b for b in blocks
               if isinstance(b, dict) and b.get("kind") == "reader_review"]
    if not readers:
        return (
            "reader_block_pass: feature_docs_review → feature_review "
            "requires a reader_review block with outcome in {pass, "
            "approved}. Append `greatminds task append-block "
            "reader_review --id <X> --field outcome=pass ...` "
            "before mv."
        )
    outcome = readers[-1].get("outcome")
    if outcome in ("pass", "approved"):
        return None
    return (
        f"reader_block_pass: latest reader_review block has "
        f"outcome={outcome!r}, expected 'pass' or 'approved'. Either "
        f"append a fresh reader_review with outcome=pass (post-fix), "
        f"or route the task back to feature_docs via the "
        f"reader_block_fail_or_partial transition."
    )


def _check_reader_block_fail_or_partial(data: dict[str, Any],
                                          from_q: str,
                                          to_q: str) -> str | None:
    """0222: feature_docs_review → feature_docs (hand-back) must
    carry a ``reader_review`` block whose latest entry has
    ``outcome in {fail, partial, changes_requested}``.

    The complement of ``reader_block_pass``. Pre-0222 noop'd, so
    contradictory states (no reader block at all, or outcome=pass)
    could route to the hand-back path."""
    blocks = data.get("blocks") or []
    readers = [b for b in blocks
               if isinstance(b, dict) and b.get("kind") == "reader_review"]
    if not readers:
        return (
            "reader_block_fail_or_partial: feature_docs_review → "
            "feature_docs requires a reader_review block with outcome "
            "in {fail, partial, changes_requested}. Append "
            "`greatminds task append-block reader_review --id <X> "
            "--field outcome=fail ...` before mv."
        )
    outcome = readers[-1].get("outcome")
    if outcome in ("fail", "partial", "changes_requested"):
        return None
    return (
        f"reader_block_fail_or_partial: latest reader_review block "
        f"has outcome={outcome!r}, expected 'fail', 'partial', or "
        f"'changes_requested'. The hand-back transition fires only "
        f"when READER found something to fix."
    )


def _check_tests_block_fail_or_partial(data: dict[str, Any],
                                         from_q: str,
                                         to_q: str) -> str | None:
    """0225: feature_test → implementer hand-back path requires a
    ``tests`` block whose latest entry has ``test_result`` in
    {fail, partial}. Pre-0225 noop'd; a task could be punted back
    to DEVELOPER without TESTER ever recording the failure."""
    blocks = data.get("blocks") or []
    tests = [b for b in blocks
             if isinstance(b, dict) and b.get("kind") == "tests"]
    if not tests:
        return (
            "tests_block_fail_or_partial: feature_test → "
            "{feature_dev, feature_ui_dev} hand-back requires a tests "
            "block with test_result in {fail, partial}. Append "
            "`greatminds task append-block tests --id <X> --field "
            "test_result=fail ...` before mv."
        )
    result = tests[-1].get("test_result")
    if result in ("fail", "partial"):
        return None
    return (
        f"tests_block_fail_or_partial: latest tests block has "
        f"test_result={result!r}, expected 'fail' or 'partial'. The "
        f"hand-back transition fires only when TESTER found a "
        f"failure. For pass, route via feature_test → feature_review."
    )


def _check_review_block_changes_requested(data: dict[str, Any],
                                            from_q: str,
                                            to_q: str) -> str | None:
    """0225: feature_review → implementer hand-back requires a
    ``review`` block whose latest entry has ``outcome=changes_requested``.
    Pre-0225 noop'd; REVIEWER could send a task back without leaving
    a review block recording the rejection rationale."""
    blocks = data.get("blocks") or []
    reviews = [b for b in blocks
               if isinstance(b, dict) and b.get("kind") == "review"]
    if not reviews:
        return (
            "review_block_changes_requested: feature_review → "
            "{feature_dev, feature_ui_dev, feature_docs} hand-back "
            "requires a review block with outcome=changes_requested. "
            "Append `greatminds task append-block review --id <X> "
            "--field outcome=changes_requested ...` before mv."
        )
    outcome = reviews[-1].get("outcome")
    if outcome == "changes_requested":
        return None
    return (
        f"review_block_changes_requested: latest review block has "
        f"outcome={outcome!r}, expected 'changes_requested'. The "
        f"hand-back transition fires only on rejection."
    )


def _check_all_dependencies_exist(data: dict[str, Any],
                                    from_q: str,
                                    to_q: str) -> str | None:
    """0225: feature_blocked → any_resume_to_queue requires that all
    declared dependencies actually exist at the named paths. Pre-
    0225 noop'd at mv (the same check happened via `greatminds wake-
    check` but only if REVIEWER ran it; not enforced inline).

    Resolves dependency strings from the latest blocked block and
    checks each against ``coord/<path>``. Missing dep → reject."""
    blocks = data.get("blocks") or []
    blockeds = [b for b in blocks
                if isinstance(b, dict) and b.get("kind") == "blocked"]
    if not blockeds:
        return (
            "all_dependencies_exist_per_wake_check: feature_blocked "
            "→ any_resume_to_queue requires a blocked block listing "
            "the dependencies that satisfied the wake."
        )
    deps = blockeds[-1].get("dependencies") or []
    if not isinstance(deps, list):
        return (
            "all_dependencies_exist_per_wake_check: latest blocked "
            "block has non-list dependencies field"
        )
    coord = find_coord_dir()
    missing: list[str] = []
    for d in deps:
        if not isinstance(d, str):
            continue
        if not (coord / d).exists():
            missing.append(d)
    if missing:
        return (
            f"all_dependencies_exist_per_wake_check: {len(missing)} "
            f"dependency(s) still missing: "
            f"{', '.join(missing[:3])}"
            + (" …" if len(missing) > 3 else "")
            + ". Run `greatminds wake-check` for the full picture."
        )
    return None


# 0247 (1.3.0): _check_evidence_for_if_related_product_task removed
# alongside the stand_wip → stand_done transition (queue dropped).
# The lease model carries evidence_for implicitly via `lease.task`
# (the lease IS the linkage).


def _check_rollback_block_with_reason(data: dict[str, Any],
                                       from_q: str, to_q: str) -> str | None:
    """0195: verified → {archive, feature_review} must carry a rollback
    block whose latest entry has a non-empty ``reason``.

    The rollback block is REVIEWER's withdraw/revisit marker for a
    verified task whose work was reverted at the code level (or needs
    further amendment). Without this gate, anyone could silently exit
    a task from verified/ — losing the WHY behind the rollback in
    the FSM record.

    Latest-wins: a task may accumulate multiple rollback blocks over
    its lifetime; the LATEST one decides whether THIS mv is allowed.
    """
    blocks = data.get("blocks") or []
    rollbacks = [b for b in blocks
                 if isinstance(b, dict) and b.get("kind") == "rollback"]
    if not rollbacks:
        return (
            "rollback_block_with_reason: verified → {archive,"
            "feature_review} requires a rollback block with non-empty "
            "reason. Append `greatminds task append-block rollback "
            "--id <X> --field reason='<why this is rolled back>'` "
            "before mv."
        )
    reason = rollbacks[-1].get("reason")
    if isinstance(reason, str) and reason.strip():
        return None
    return (
        "rollback_block_with_reason: latest rollback block has empty "
        "reason. Append a fresh rollback block with a non-empty "
        "reason explaining why the verified task is being rolled back."
    )


def _check_review_block_approved(data: dict[str, Any],
                                 from_q: str, to_q: str) -> str | None:
    """0171: feature_review → verified must carry a review block whose
    latest entry has ``outcome: approved``.

    Pre-0171 the schema row had ``requires: [review_block_approved,
    gate_check_pass_if_stand_required]`` but only the gate-check
    validator was real (0103). ARCHITECT-REVIEWER could ``task mv
    <id> verified`` without appending a review block at all, or with
    ``outcome != approved``. This validator closes the hole.

    Latest-wins semantics: a task can ping-pong feature_review ↔
    feature_dev across iterations, accumulating review blocks. Only
    the LATEST one decides whether this verify is allowed.
    """
    blocks = data.get("blocks") or []
    reviews = [b for b in blocks
               if isinstance(b, dict) and b.get("kind") == "review"]
    if not reviews:
        return (
            "review_block_approved: feature_review → verified requires "
            "a review block with outcome=approved. Append "
            "`greatminds task append-block review --id <X> --field "
            "outcome=approved ...` before mv."
        )
    outcome = reviews[-1].get("outcome")
    # approved_sprint: LIVE-DEVELOPER sprint task — REVIEWER's
    # no-regression approval (USER validated it live); also verifies.
    if outcome in ("approved", "approved_sprint"):
        return None
    return (
        f"review_block_approved: latest review block has "
        f"outcome={outcome!r}, expected 'approved' or 'approved_sprint'. "
        f"Either append a fresh review block with outcome=approved "
        f"(post-fix iteration), or route the task back to the appropriate "
        f"per-scope queue with outcome=changes_requested."
    )


# ---------------------------------------------------------------------------
# 0102 validators: close 4 single-role archive holes by adding real
# preconditions (review_sessions → archive, feature_blocked → archive,
# stand_done → archive, stand_requests → archive).
# ---------------------------------------------------------------------------

_WITHDRAWN_REASON_TOKENS = ("withdrawn", "abandoned", "obsoleted")


def _latest_block_of_kind(data: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for b in reversed(data.get("blocks") or []):
        if isinstance(b, dict) and b.get("kind") == kind:
            return b
    return None


def _has_block_of_kind(data: dict[str, Any], kind: str) -> bool:
    return _latest_block_of_kind(data, kind) is not None


def _check_review_session_terminal(data: dict[str, Any],
                                   from_q: str, to_q: str) -> str | None:
    """0102 (1)+(2): review_sessions → archive requires evidence the
    session reached a terminal state — either at least one
    ``session_iteration`` block (EXPLORER iterated) OR a ``blocked``
    block whose reason names a withdrawn-class token. An empty
    review_session may not be silently archived."""
    if _has_block_of_kind(data, "session_iteration"):
        return None
    blocked = _latest_block_of_kind(data, "blocked")
    if blocked is not None:
        reason = str(blocked.get("reason") or "").lower()
        if any(t in reason for t in _WITHDRAWN_REASON_TOKENS):
            return None
    return (
        "review_session_terminal_block: review_session has no "
        "session_iteration block and no blocked block with a "
        "withdrawn/abandoned/obsoleted reason; either record at least "
        "one iteration or file a withdrawn-blocked block first"
    )


def _check_feature_blocked_withdrawn(data: dict[str, Any],
                                     from_q: str, to_q: str) -> str | None:
    """0102 (3): feature_blocked → archive by ARCHITECT-REVIEWER requires
    the latest ``blocked`` block to carry a withdrawn-class reason.
    Without this guard REVIEWER alone can wipe in-flight work."""
    blocked = _latest_block_of_kind(data, "blocked")
    if blocked is None:
        return (
            "feature_blocked_withdrawn_reason: task has no blocked block "
            "(unexpected — feature_blocked entry should have one)"
        )
    reason = str(blocked.get("reason") or "").lower()
    if any(t in reason for t in _WITHDRAWN_REASON_TOKENS):
        return None
    return (
        f"feature_blocked_withdrawn_reason: latest blocked.reason "
        f"{blocked.get('reason')!r} does not contain a withdrawn-class "
        f"token ({list(_WITHDRAWN_REASON_TOKENS)}); refuse to archive "
        f"in-flight work"
    )


# 0247 (1.3.0): _check_stand_done_no_active_dependents and
# _check_stand_request_not_yet_claimed both removed alongside the
# stand_done → archive + stand_requests → archive transitions.
# The lease model has no queue-to-queue transitions; release
# evidence lives on the product task's tests block (0246).


def _check_plan_interactive(data: dict[str, Any],
                            from_q: str, to_q: str) -> str | None:
    """feature_plan → feature_live requires the latest plan block to set
    ``interactive: true`` (the LIVE-DEVELOPER routing marker)."""
    if is_interactive_task(data):
        return None
    return (
        "plan.interactive: feature_plan → feature_live requires the latest "
        "plan block to set interactive: true. Append `greatminds task "
        "append-block plan --id <X> --field interactive=true ...`, or route "
        "to a per-scope queue (feature_dev / feature_ui_dev / feature_docs)."
    )


def _check_plan_verify_only(data: dict[str, Any],
                            from_q: str, to_q: str) -> str | None:
    """feature_plan → feature_test requires the latest plan block to set
    ``verify_only: true`` (the no-code stand/playbook verification path:
    TESTER leases a stand, runs the playbook/probe, records stand
    evidence — no implementer step)."""
    if is_verify_only(data):
        return None
    return (
        "plan.verify_only: feature_plan → feature_test requires the latest "
        "plan block to set verify_only: true (no-code stand/playbook "
        "verification — TESTER leases a stand and records evidence). "
        "Append `greatminds task append-block plan --id <X> --field "
        "verify_only=true ...`, or route via an implementer queue "
        "(feature_dev / feature_ui_dev) and advance with an implementation "
        "block."
    )


# ---------------------------------------------------------------------------
SCHEMA_REQUIRES_VALIDATORS: dict[str, "callable"] = {
    # Empty pre-condition is always satisfied.
    # (Schema entries with `requires: []` are still validated for role/
    # transition existence via can_role_move.)
    # 0222: was _noop_existing; real validator enforces user_feedback
    # → {feature_inbox, archive} carries a triage block with non-empty
    # notes (EXPLORER stand_done/0205 found the hole).
    "triage_block": _check_triage_block,
    # 0225 doc: ``plan_block`` is named in
    # ``feature_inbox → feature_plan`` requires but the plan block
    # itself lands AFTER the mv (PLANNER appends inside feature_plan).
    # Documentary — there is no mv-time prerequisite to enforce.
    "plan_block": _noop_existing,
    # 0225 doc: scope_* names are gates for routing decisions enforced
    # by ``require_scope_match_on_routing`` (a separate pre-schema
    # gate). The schema-level requires entry is documentary; the real
    # check fires in cli/task.py:require_scope_match_on_routing.
    "scope_backend": _noop_existing,
    "scope_ui": _noop_existing,
    "scope_docs": _noop_existing,
    # 0225 doc: plan.audit_only enforced by validate_block's plan
    # branch on the plan block. Documentary at mv level.
    "plan.audit_only": _noop_existing,
    # feature_plan → feature_live: real gate on the latest plan's
    # interactive marker (the LIVE-DEVELOPER routing path).
    "plan.interactive": _check_plan_interactive,
    # feature_plan → feature_test: real gate on the latest plan's
    # verify_only marker (the no-code stand/playbook verification path).
    "plan.verify_only": _check_plan_verify_only,
    # 0225 doc: implementation_block / tests_block names appear in
    # ``feature_plan → feature_dev`` and ``feature_dev → feature_test``
    # requires but the named block doesn't exist yet at mv time (it
    # lands AFTER, in the destination queue). Real enforcement is owned
    # by require_target_readiness(): it rejects both missing required
    # blocks on exits that need them and false/missing ready_for_* flags
    # on the prior block. Documentary here.
    "implementation_block": _noop_existing,
    "tests_block": _noop_existing,
    # 0225: real validator for the test-handback path.
    "tests_block_fail_or_partial": _check_tests_block_fail_or_partial,
    # 0222: real validators for the docs-review verdict gates.
    "reader_block_pass": _check_reader_block_pass,
    "reader_block_fail_or_partial": _check_reader_block_fail_or_partial,
    # 0171 real-enforcement: require_target_readiness no longer trusts
    # the bare presence of a review block; the latest one must carry
    # outcome=approved.
    "review_block_approved": _check_review_block_approved,
    # 0225: real validator for the review-handback path.
    "review_block_changes_requested": _check_review_block_changes_requested,
    # 0225 doc: ``blocked_block_with_dependencies_and_resume_to`` is
    # enforced by validate_block's `blocked` branch (non-empty deps +
    # known resume_to queue). The mv-time validator would be
    # redundant; the field shape is already gated. Documentary.
    "blocked_block_with_dependencies_and_resume_to": _noop_existing,
    # 0225: real validator — feature_blocked → any_resume_to requires
    # every dependency file actually exists at its declared path.
    "all_dependencies_exist_per_wake_check": _check_all_dependencies_exist,
    # 0247 (1.3.0): stand_result_block, evidence_for_if_related_product_task,
    # stand_done_no_active_dependents, stand_request_not_yet_claimed
    # all REMOVED. Their corresponding schema transitions (stand_wip →
    # stand_done, stand_done → archive, stand_requests → archive) are
    # gone with the queues. Lease evidence lives on the product task's
    # tests block; gate_check (0246) validates the chain there.
    # 0103 real-enforcement: re-evaluate gate-check rather than trust
    # tests.gate_check_result.
    "gate_check_pass_if_stand_required": _check_gate_for_stand_required,
    # 0102 real-enforcement: archive-hole guards.
    "review_session_terminal_block": _check_review_session_terminal,
    "feature_blocked_withdrawn_reason": _check_feature_blocked_withdrawn,
    # 0195: verified → archive/feature_review must carry a rollback
    # block with non-empty reason. Restores the 0105 intent.
    "rollback_block_with_reason": _check_rollback_block_with_reason,
}


def enforce_schema_requires(data: dict[str, Any], role: str,
                            from_q: str, to_q: str) -> None:
    """Run each `requires:` entry's validator for every (from, to) schema
    row that authorizes ``role`` to perform this move. Raise
    GreatMindsError on the first failure or on an unknown require name.

    Row matching mirrors ``can_role_move`` authorization, including
    ``by: current_owner`` — when ``role`` owns or writes ``from_q``,
    the current_owner row authorizes the move and its requires must
    be enforced. Restricting to ``by == role`` would leave current_owner
    rows (e.g. any_active_queue → feature_blocked) with documentary-only
    requires — the exact FSM-hole class 0103 closes.

    If multiple rows authorize the role, requires from all of them are
    enforced (deduped by name).
    """
    matches = transitions_for(from_q, to_q)
    if not matches:
        return  # can_role_move already rejected; no schema row to enforce.

    owner = (queue_meta(from_q).get("owner") or "").upper()
    writers = queue_meta(from_q).get("writers") or []
    authorizing_rows: list[dict[str, Any]] = []
    for t in matches:
        by = t.get("by")
        if isinstance(by, str) and by == role:
            authorizing_rows.append(t)
        elif by == "current_owner":
            if not owner or owner == role or role in writers:
                authorizing_rows.append(t)
    if not authorizing_rows:
        return  # No row authorizes role; can_role_move handles the rejection.

    seen: set[str] = set()
    for row in authorizing_rows:
        for name in row.get("requires") or []:
            if name in seen:
                continue
            seen.add(name)
            validator = SCHEMA_REQUIRES_VALIDATORS.get(name)
            if validator is None:
                raise GreatMindsError(
                    f"schema requires key {name!r} (transition {from_q} → "
                    f"{to_q} by {row.get('by')!r}) has no registered "
                    f"validator in SCHEMA_REQUIRES_VALIDATORS — this would "
                    f"land as a documentary-only key (the exact FSM-hole "
                    f"class task 0103 closes). Add an entry to the registry."
                , exit_code=2)
            err = validator(data, from_q, to_q)
            if err is not None:
                raise GreatMindsError(
                    f"transition {from_q} → {to_q} by {role} failed "
                    f"requires-check {name!r}: {err}"
                , exit_code=2)


def role_for_block_kind(role: str, kind: str, queue: str,
                        data: dict[str, Any]) -> str | None:
    """Return ``None`` if role may author this block-kind on this task."""
    if kind == "blocked":
        owner = (queue_meta(queue).get("owner") or "").upper()
        if owner and role == owner:
            return None
        return (f"role {role} is not owner of {queue} "
                f"(owner: {owner}); only owner may file a blocked block")
    allowed = BLOCK_KIND_ROLES.get(kind)
    if allowed is None:
        return f"block kind {kind!r} has no role whitelist"
    if role not in allowed:
        return (f"role {role} may not author block kind {kind!r}; "
                f"allowed: {sorted(allowed)}")
    if kind == "implementation":
        scope = data.get("scope")
        expected = IMPL_ROLE_BY_SCOPE.get(scope or "")
        if expected and expected != role:
            return (f"task scope: {scope!r} requires {expected} for "
                    f"implementation, not {role}")
    return None


def _enforce_tests_functional_probes_per_scope(
    new_block: dict[str, Any], data: dict[str, Any],
) -> None:
    """0228: TESTER must run own functional probes on the prepared
    stand. STAND-KEEPER's ``observed_with_fix`` records infra-
    readiness (container UP, /health 200, version match). TESTER's
    ``tests.functional_probes`` + ``tests.stand_evidence.
    tester_observations`` record behavior verification. These are
    distinct activities by distinct roles; rubber-stamping SK's
    observation as the test result is what this validator catches.

    Schema-driven: schema.tests_block_validation.required_for_scopes
    lists which scopes require which fields; exempt_scopes lists
    scopes where this validator is a no-op (docs, research).
    """
    scope = data.get("scope") or ""
    if not isinstance(scope, str) or not scope:
        return

    # Schema lookup.
    try:
        cfg = (schema().get("tests_block_validation") or {})
    except Exception:
        return
    exempt = set(cfg.get("exempt_scopes") or [])
    if scope in exempt:
        return
    required_table = cfg.get("required_for_scopes") or {}
    required = required_table.get(scope)
    if not required:
        return

    # functional_probes — non-empty list.
    probes = new_block.get("functional_probes")
    if "functional_probes" in required:
        if not isinstance(probes, list) or not probes:
            raise GreatMindsError(
                f"tests block on scope={scope!r} requires non-empty "
                f"functional_probes list (TESTER's own commands ran "
                f"AGAINST the prepared stand — curl/psql/UI per scope). "
                f"stand_result is SK's infra-readiness, not your test "
                f"result. See COORDINATE.md §9 + 0228.",
                exit_code=2,
            )

    # tester_observations under stand_evidence — non-empty + distinct
    # from SK's stand_result.observed_with_fix.
    if "stand_evidence.tester_observations" in required:
        ev = new_block.get("stand_evidence") or {}
        if not isinstance(ev, dict):
            ev = {}
        tester_obs = ev.get("tester_observations")
        if not (isinstance(tester_obs, str) and tester_obs.strip()):
            raise GreatMindsError(
                f"tests block on scope={scope!r} requires "
                f"stand_evidence.tester_observations (TESTER's own "
                f"probe output — DISTINCT from SK's "
                f"stand_result.observed_with_fix). See 0228.",
                exit_code=2,
            )
        # Anti-rubber-stamp pin: string-equality against the latest
        # stand_result block's observed_with_fix. If TESTER copied
        # SK's text verbatim, that's the failure mode 0228 closes.
        latest_stand_result = next(
            (b for b in reversed(data.get("blocks") or [])
             if isinstance(b, dict) and b.get("kind") == "stand_result"),
            None,
        )
        if latest_stand_result is not None:
            sk_obs = (latest_stand_result.get("observed_with_fix")
                      or "")
            if (isinstance(sk_obs, str) and sk_obs.strip()
                and tester_obs.strip() == sk_obs.strip()):
                raise GreatMindsError(
                    "tests.stand_evidence.tester_observations is "
                    "VERBATIM identical to stand_result.observed_with_"
                    "fix. SK observed infra-readiness; TESTER must "
                    "record DIFFERENT observations from own functional "
                    "probes (0228 rubber-stamp guard).",
                    exit_code=2,
                )


def require_block_cross_state(new_block: dict[str, Any],
                              data: dict[str, Any]) -> None:
    """Cross-block validation at append time.

    A2: REVIEWER cannot approve when latest tests/reader block is not pass.
    0091 item 3: tests block on a stand_required task must carry
    `stand_evidence` as a mapping with reproduction_steps +
    observed_without_fix + observed_with_fix subfields. Mirrors the
    schema.tests_block_validation contract (required_when:
    plan.stand_required is true).
    """
    # 0091 item 3 / 0301 — stand_evidence subfields gate.
    # 0301: read required_subfields from schema instead of hardcoding
    # them here. Pre-0301 the validator pinned 3 fields while
    # gate_check.extract_lease_evidence_from_tests demanded lease_id
    # additionally → every well-formed task hit ``missing`` at mv
    # time. Schema is now the single source of truth.
    if new_block.get("kind") == "tests":
        plan_blocks = [b for b in (data.get("blocks") or [])
                       if isinstance(b, dict) and b.get("kind") == "plan"]
        if plan_blocks and plan_blocks[-1].get("stand_required") is True:
            ev = new_block.get("stand_evidence")
            try:
                cfg = ((schema().get("tests_block_validation") or {})
                       .get("stand_evidence") or {})
                required_subfields = tuple(
                    cfg.get("required_subfields") or (
                        "reproduction_steps",
                        "observed_without_fix",
                        "observed_with_fix",
                    )
                )
            except Exception:
                required_subfields = (
                    "reproduction_steps",
                    "observed_without_fix",
                    "observed_with_fix",
                )
            if not isinstance(ev, dict):
                raise GreatMindsError(
                    f"tests block on a stand_required task must set "
                    f"stand_evidence as a mapping with the required "
                    f"subfields {list(required_subfields)} (0091 item 3; "
                    f"0301; schema.tests_block_validation). Got: "
                    f"{type(ev).__name__}.",
                    exit_code=2,
                )
            missing = [k for k in required_subfields
                       if not (str(ev.get(k) or "").strip())]
            if missing:
                raise GreatMindsError(
                    f"tests.stand_evidence missing required subfields: "
                    f"{missing} (0091 item 3 / 0301; schema is normative).",
                    exit_code=2,
                )
        # 0228: TESTER-vs-SK role boundary — tests block on a
        # backend/ui task must carry TESTER's own functional probes
        # + tester_observations distinct from SK's infra-readiness.
        _enforce_tests_functional_probes_per_scope(new_block, data)

    if new_block.get("kind") != "review":
        return
    if new_block.get("outcome") != "approved":
        return
    blocks = data.get("blocks") or []
    latest_tests = next(
        (b for b in reversed(blocks)
         if isinstance(b, dict) and b.get("kind") == "tests"),
        None,
    )
    latest_reader = next(
        (b for b in reversed(blocks)
         if isinstance(b, dict) and b.get("kind") == "reader_review"),
        None,
    )
    if latest_tests is not None:
        if latest_tests.get("test_result") != "pass":
            raise GreatMindsError(
                f"cannot approve: latest tests.test_result="
                f"{latest_tests.get('test_result')!r} (expected 'pass')"
            , exit_code=2)
        return
    if latest_reader is not None:
        # Audit-only tasks (plan.audit_only: true) treat 'partial' and
        # 'fail' as legitimate audit conclusions — the findings get
        # consumed by PLANNER spawning fixer tasks. For non-audit docs
        # tasks, only 'pass' approves.
        outcome = latest_reader.get("outcome")
        if is_audit_only(data):
            if outcome not in ("pass", "partial", "fail"):
                raise GreatMindsError(
                    f"cannot approve: latest reader_review.outcome="
                    f"{outcome!r} (expected 'pass', 'partial', or 'fail' "
                    f"for an audit-only task)"
                , exit_code=2)
        else:
            if outcome != "pass":
                raise GreatMindsError(
                    f"cannot approve: latest reader_review.outcome="
                    f"{outcome!r} (expected 'pass')"
                , exit_code=2)
        return
    raise GreatMindsError(
        "cannot approve: no tests or reader_review block on this task"
    , exit_code=2)


def require_block_acceptable_in_queue(queue: str, kind: str) -> None:
    allowed = QUEUE_BLOCK_KINDS.get(queue)
    if allowed is None:
        raise GreatMindsError(f"queue {queue!r} has no block-policy entry", exit_code=2)
    if not allowed:
        raise GreatMindsError(f"queue {queue!r} is terminal — no new blocks accepted", exit_code=2)
    if kind not in allowed:
        raise GreatMindsError(
            f"queue {queue!r} accepts {sorted(allowed)}, not {kind!r}"
        , exit_code=2)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def default_intake_queue(stream: str) -> str:
    # 0258 / 0247 (1.3.0 BREAKING): ``stand`` stream removed.
    return {
        "product": "feature_inbox",
        "review_session": "review_sessions",
    }[stream]


def next_seq(coord: Path) -> str:
    """Return next 4-digit id prefix, atomically incremented under flock."""
    lock_path = coord / ".id_counter"
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("ascii", errors="ignore").strip()
        cached = int(raw) if raw.isdigit() else 0
        mx = cached
        for q in coord.iterdir():
            if not q.is_dir() or q.name.startswith("."):
                continue
            for f in q.glob("[0-9][0-9][0-9][0-9]-*"):
                try:
                    n = int(f.name.split("-", 1)[0])
                    if n > mx:
                        mx = n
                except (ValueError, IndexError):
                    continue
        nxt = mx + 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(nxt).encode("ascii"))
        os.fsync(fd)
        return f"{nxt:04d}"
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def read_body(spec: str) -> str:
    """``@PATH`` → file contents, ``-`` → stdin, else literal."""
    if spec == "-":
        return sys.stdin.read()
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


def body_field_for(kind: str) -> str:
    return {
        "triage": "notes",
        "plan": "body",
        "implementation": "notes",
        "tests": "notes",
        "reader_review": "notes",
        "review": "notes",
        "blocked": "reason",
        "stand_result": "notes",
        "session_iteration": "summary",
    }.get(kind, "notes")


def coerce_value(key: str, v: str) -> Any:
    """Best-effort coerce of a ``--field key=value`` value.

    Only fields explicitly in :data:`LIST_FIELDS` are split on commas. All
    other fields stay as strings even if they contain commas or colons —
    so user-supplied prose like ``stand_reason="POST /node, then GET
    /health"`` isn't accidentally turned into a YAML list.

    For LIST_FIELDS keys, YAML bracket-list syntax (``files=[a.py, b.py]``
    or the empty form ``test_files=[]``) is parsed via yaml.safe_load
    when the value starts with ``[``. This matches user expectation —
    EXPLORER avatar dogfood (0035) found that bracket syntax was being
    stored as a list of one literal string ``['[a.py]']``, which then
    silently broke downstream validators reading ``files:`` as a list
    of paths. yaml.safe_load failure falls back to the comma-split path,
    so existing comma syntax keeps working.
    """
    if key in LIST_FIELDS:
        stripped = v.strip()
        if stripped.startswith("["):
            try:
                parsed = yaml.safe_load(stripped)
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, list):
                # Stringify each item — schema validators expect strings
                # (paths, hostnames, etc.). int/bool inside the bracket
                # list (`files=[1, 2]`) is meaningless for these fields.
                return [str(x).strip() for x in parsed if str(x).strip()]
            # Fell through: not a real bracket-list — let the comma path
            # handle it (raises later via validate_task if shape is wrong).
        if "," in v:
            return [x.strip() for x in v.split(",") if x.strip()]
        return [v] if v else []
    stripped = v.strip()
    if stripped.startswith("{"):
        # 0091 iter-N+M: support YAML/JSON mapping input via the CLI for
        # fields like tests.stand_evidence (canon §9 / tests_block_validation
        # requires it as a mapping with three subfields). Without this,
        # the CLI couldn't produce a validator-passing tests block — the
        # exact gap REVIEWER flagged.
        try:
            parsed = yaml.safe_load(stripped)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        # Not a real mapping — fall through to string path.
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.isdigit():
        return int(v)
    return v


# ---------------------------------------------------------------------------
# Public library API — called by click handlers below AND by other CLI
# modules (stand.py, plan.py, inbox.py). No subprocess between modules.
# ---------------------------------------------------------------------------


def create_task(
    *,
    stream: str,
    title: str,
    reporter: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
    scope: str | None = None,
    hosts: list[str] | None = None,
    evidence_for: list[str] | None = None,
    mode: str | None = None,
    target_functionality: str | None = None,
    scenarios: list[str] | None = None,
    description: str | None = None,
    in_queue: str | None = None,
    seq: str | None = None,
    reason: str | None = None,
) -> Path:
    """Create a new task. Returns the path of the written file.

    Raises :class:`GreatMindsError` on validation / permission failure.
    """
    coord = find_coord_dir()

    # 0258 / 0247 (1.3.0 BREAKING): ``stand`` stream removed.
    if stream not in ("product", "review_session"):
        raise GreatMindsError(
            "--stream must be product|review_session "
            "(stand stream removed in 1.3.0; use `greatminds stand lease`)"
        )
    # Defense in depth: catch a stand-era ``--kind=stand_request`` even if
    # someone bypasses --stream validation via the library API.
    if kind == "stand_request":
        raise GreatMindsError(
            "--kind=stand_request removed in 1.3.0 (0247 BREAKING); "
            "use `greatminds stand lease --task <id> --worktree <path> "
            "--profile <enum>` for the new lease API.",
            exit_code=2,
        )

    # USER intake is the only flow where the caller is genuinely human and
    # outside the agent fleet — there is no role launcher exporting
    # GREATMINDS_ROLE for them. Schema says ``user_feedback.writers: [USER]``,
    # so when the destination is user_feedback and no role is set, default
    # to USER. Every other fleet intake (feature_inbox, review sessions)
    # is fleet-driven and keeps the strict env-var requirement.
    _resolved_in_q = in_queue or default_intake_queue(stream)
    if _resolved_in_q == "user_feedback" and not (os.environ.get("GREATMINDS_ROLE") or "").strip():
        role = "USER"
        if role not in (schema().get("roles") or {}):
            # Defensive: if a future schema drops USER, fall back to strict.
            role = caller_role()
    else:
        role = caller_role()

    # E1: bound title length so an accidental dump doesn't fill the file.
    if len(title) > TITLE_MAX_LEN:
        raise GreatMindsError(
            f"title too long ({len(title)} chars, max {TITLE_MAX_LEN})"
        )

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    if not slug:
        # title was entirely non-ascii / control chars / empty after slugify
        slug = "task-" + uuid.uuid4().hex[:8]
    seq_str = seq or next_seq(coord)
    if not re.match(r"^[0-9]{4}$", seq_str):
        raise GreatMindsError(
            f"--seq must be a 4-digit non-negative number, got: {seq_str!r}"
        )
    task_id = f"{seq_str}-{slug}"

    data: dict[str, Any] = {
        "id": task_id,
        "stream": stream,
        "title": title,
        "reporter": reporter or role,
        "opened_at": now_iso(),
        "priority": priority or "normal",
    }
    if stream == "product":
        if not kind or not scope:
            raise GreatMindsError("product stream needs --kind and --scope")
        data["kind"] = kind
        data["scope"] = scope
    elif stream == "review_session":
        data["kind"] = "review_session"
        data["mode"] = mode or "B"
        if not target_functionality:
            raise GreatMindsError("review_session needs --target-functionality")
        data["target_functionality"] = target_functionality
        data["scenarios"] = scenarios or []

    if description:
        data["description"] = read_body(description)
    data["blocks"] = []

    in_q = in_queue or default_intake_queue(stream)
    allowed = ALLOWED_INTAKE_QUEUES.get(stream, set())
    if in_q not in allowed:
        raise GreatMindsError(
            f"--in-queue {in_q!r} not allowed for stream {stream!r}; "
            f"allowed intake: {sorted(allowed)}"
        )
    target_path = coord / in_q / f"{task_id}.yaml"
    if target_path.exists():
        raise GreatMindsError(f"task {task_id} already exists at {target_path}")

    validate_task(data)

    atomic_write_yaml(target_path, data)
    journal_append(coord, {
        "t": now_iso(),
        "actor": role,
        "task": task_id,
        "from": "_new",
        "to": in_q,
        "reason": reason or f"new {stream} task",
        "intent_id": "",
    })
    touch_heartbeat(coord, role)
    return target_path


def move_task(*, task_id: str, to_queue: str, reason: str | None = None) -> str:
    """Move a task between queues. Returns the resolved ``from_queue``."""
    coord = find_coord_dir()
    role = caller_role()
    with task_file_lock(coord, task_id):
        return _do_move(coord, role, task_id, to_queue, reason or "")


def _role_can_reach_target(role: str, to_q: str) -> tuple[bool, list[str]]:
    """0113: does this role have ANY authorized schema row landing in
    ``to_q`` (any from_q)?

    Returns (allowed, permitted_role_list). If allowed is False, the
    role wouldn't have been authorized to mv to ``to_q`` even from
    the right source queue — surfacing this as the primary error is
    more diagnostic than 'task not found'.
    """
    permitted: set[str] = set()
    role_ok = False
    for t in schema().get("transitions") or []:
        if not isinstance(t, dict) or t.get("to") not in (to_q, "any_resume_to_queue"):
            continue
        by = t.get("by")
        if isinstance(by, str):
            if by == "current_owner":
                # current_owner row authorizes based on from_q ownership;
                # we don't know from_q here, so treat as "may apply".
                role_ok = True
                permitted.add("current_owner")
            else:
                permitted.add(by)
                if by == role:
                    role_ok = True
    return role_ok, sorted(permitted)


def _do_move(coord: Path, role: str, task_id: str,
             to_q: str, reason: str) -> str:
    if to_q not in (schema().get("queues") or {}):
        raise GreatMindsError(f"unknown destination queue: {to_q}")

    found = find_task(coord, task_id)
    if found is None:
        # 0113: enrich the not-found error. If the role has no
        # authorized path to to_q at all, surface that as the primary
        # cause — it's a more accurate diagnosis than "not found" in
        # cases like the 0097 race incident where the user wouldn't
        # have been allowed even without the race.
        role_ok, permitted = _role_can_reach_target(role, to_q)
        if not role_ok:
            permitted_str = " or ".join(permitted) if permitted else "no role"
            raise GreatMindsError(
                f"task {task_id} not found in any queue, AND role "
                f"{role} has no authorized transition into {to_q} "
                f"regardless of source queue (only "
                f"{permitted_str} may land tasks in {to_q})",
                exit_code=3,
            )
        raise GreatMindsError(
            f"task {task_id} not found in any queue (may have been "
            f"moved by another agent between your last check and "
            f"this mv; rerun greatminds task list / wake-check / "
            f"watchdog to refresh state)"
        )
    src_path, from_q = found

    if from_q == to_q:
        raise GreatMindsError(f"task already in {to_q}")

    data = load_task(src_path)
    if data.get("_legacy_md"):
        raise GreatMindsError(f"task {task_id} is legacy .md; migrate before moving", exit_code=2)

    err = can_role_move(role, from_q, to_q, data)
    if err is not None:
        raise GreatMindsError(err, exit_code=3)

    require_scope_match_on_routing(data, from_q, to_q)
    require_target_readiness(data, from_q, to_q)
    # 0103: enforce schema's `requires:` list table-driven (the
    # previously documentary-only field is now load-bearing).
    enforce_schema_requires(data, role, from_q, to_q)

    intent_path = intent_write(coord, role, task_id, from_q, to_q, reason)
    intent_id = intent_path.stem.rsplit("-", 1)[-1]

    # 0185: worktree-lifecycle hooks fire BEFORE the rename so a
    # failed worktree operation (merge conflict, etc.) leaves the
    # task in its source queue rather than half-moved.
    _worktree_hook_pre_move(coord, role, task_id, data, from_q, to_q)

    dst_path = coord / to_q / src_path.name
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src_path, dst_path)
    except OSError as exc:
        intent_clear(intent_path)
        raise GreatMindsError(f"mv failed: {exc}", exit_code=4)

    intent_clear(intent_path)
    journal_append(coord, {
        "t": now_iso(),
        "actor": role,
        "task": task_id,
        "from": from_q,
        "to": to_q,
        "reason": (reason or "")[:200],
        "intent_id": intent_id,
    })
    # 0185: post-move worktree cleanup (archive).
    _worktree_hook_post_move(coord, task_id, data, to_q)
    touch_heartbeat(coord, role)
    return from_q


# 0185: worktree-lifecycle hooks.
#
# Sites:
#   * mv → feature_dev / feature_ui_dev / feature_docs  → worktree create
#   * mv → verified by REVIEWER                          → worktree merge
#   * mv → archive                                       → worktree remove
#
# Hooks are gated on:
#   - schema's worktrees:required_for_task_kinds (so research/docs
#     tasks bypass until USER adds them); and
#   - successful import of the worktree CLI module (defensive: if
#     git is missing, hooks silently no-op rather than crash the
#     fleet).
_IMPLEMENTER_QUEUES = {"feature_dev", "feature_ui_dev", "feature_docs"}


def _worktree_hook_pre_move(coord: Path, role: str, task_id: str,
                             data: dict[str, Any],
                             from_q: str, to_q: str) -> None:
    """Run worktree create / merge hooks before the file rename."""
    try:
        from greatminds.cli import worktree as wt_mod
    except ImportError:
        return
    try:
        policy = wt_mod.load_worktree_policy()
    except Exception:
        return
    task_kind = data.get("kind")
    if task_kind not in policy.required_for_task_kinds:
        return
    project_dir = coord.parent
    # Skip cleanly on non-git projects (greenfield setup, test
    # fixtures). Hooks are no-ops when the project isn't yet under
    # version control — running `git init` is an operator decision.
    if not (project_dir / ".git").exists():
        return

    if to_q in _IMPLEMENTER_QUEUES:
        # Idempotent: returns the existing path on re-entry.
        try:
            wt_mod.worktree_create(project_dir, task_id)
        except GreatMindsError as exc:
            raise GreatMindsError(
                f"0185: worktree create for {task_id} failed: {exc}",
                exit_code=4,
            )
    elif to_q == "verified" and policy.cleanup_on_verified:
        # REVIEWER merge-to-main path. Block the mv on conflict so
        # main stays clean + REVIEWER can hand back to
        # conflict_handback_to.
        try:
            result = wt_mod.worktree_merge(project_dir, task_id,
                                           summary=f"{task_kind}({task_id})")
        except GreatMindsError as exc:
            raise GreatMindsError(
                f"0185: worktree merge for {task_id} failed: {exc}",
                exit_code=4,
            )
        if not result.ok:
            raise GreatMindsError(
                f"0185: merge of task/{task_id} into main conflicted; "
                f"REVIEWER must hand back to {policy.conflict_handback_to} "
                f"({len(result.conflicts)} file(s) in conflict). "
                f"Conflicts: {', '.join(result.conflicts[:5])}",
                exit_code=3,
            )


def _worktree_hook_post_move(coord: Path, task_id: str,
                              data: dict[str, Any], to_q: str) -> None:
    """Run worktree remove hook after the file rename."""
    try:
        from greatminds.cli import worktree as wt_mod
    except ImportError:
        return
    try:
        policy = wt_mod.load_worktree_policy()
    except Exception:
        return
    task_kind = data.get("kind")
    if task_kind not in policy.required_for_task_kinds:
        return
    project_dir = coord.parent
    if not (project_dir / ".git").exists():
        return

    if to_q == "archive" and policy.cleanup_on_archive:
        try:
            wt_mod.worktree_remove(project_dir, task_id, force=True)
        except Exception:
            pass  # best-effort; orphaned worktree is harmless
    elif to_q == "verified" and policy.cleanup_on_verified:
        # Branch + worktree dir were consumed by the merge; remove
        # leftover state.
        try:
            wt_mod.worktree_remove(project_dir, task_id, force=False)
        except Exception:
            pass


def _enforce_worktree_isolation_for_block(
    kind: str,
    data: dict[str, Any],
    coord: Path,
    task_id: str,
) -> None:
    """0303 (upstream issue #3): refuse code-mutating blocks when
    the caller's cwd is not the per-task worktree.

    Applies only to ``implementation`` / ``tests`` blocks on tasks
    whose kind is listed in ``schema.worktrees.required_for_task_kinds``
    (feature/bugfix/ops by default). Pre-0303 the schema flag was
    declarative-only; implementers could silently edit main while
    filing the block.

    The override env var ``GREATMINDS_SKIP_WORKTREE_CHECK=1`` is
    honored so test scripts + power users running from CI containers
    aren't blocked; auditing those flows is out of scope here.
    """
    if kind not in ("implementation", "tests"):
        return
    if os.environ.get("GREATMINDS_SKIP_WORKTREE_CHECK", "").strip() == "1":
        return
    task_kind = (data.get("kind") or "").strip()
    try:
        cfg = (schema().get("worktrees") or {})
    except Exception:
        return
    required_kinds = set(cfg.get("required_for_task_kinds") or [])
    if task_kind not in required_kinds:
        return
    project_dir = coord.parent.resolve(strict=False)
    base_path = (cfg.get("base_path") or ".worktrees").strip()
    worktrees_root = (project_dir / base_path).resolve(strict=False)
    try:
        cwd = Path.cwd().resolve(strict=False)
    except OSError:
        return
    # Allowed: cwd is under <project>/.worktrees/<X>/ where X starts
    # with the task's seq prefix (matches stand-lease validator 0271).
    seq = task_id.split("-", 1)[0]
    rel = None
    try:
        rel = cwd.relative_to(worktrees_root)
    except ValueError:
        rel = None
    if rel is not None and rel.parts:
        wt_name = rel.parts[0]
        if wt_name == seq or wt_name.startswith(f"{seq}-"):
            return
    raise GreatMindsError(
        f"append-block {kind!r} refused: cwd {cwd} is not under the "
        f"per-task worktree {worktrees_root}/{seq} (task.kind="
        f"{task_kind!r} requires worktree isolation per "
        f"schema.worktrees.required_for_task_kinds). Run "
        f"`cd \"$(greatminds worktree path {task_id})\"` first. "
        f"Override with GREATMINDS_SKIP_WORKTREE_CHECK=1 in CI.",
        exit_code=2,
    )


def append_block(
    *,
    task_id: str,
    kind: str,
    fields: dict[str, Any] | list[str] | None = None,
    body: str | None = None,
) -> Path:
    """Append a typed block to an existing task.

    ``fields`` may be a dict ``{"key": value, ...}`` or a list of
    ``"key=value"`` strings (the latter is what comes from ``--field``
    repeated flags on the CLI).

    Returns the task's path.
    """
    coord = find_coord_dir()
    role = caller_role()

    with task_file_lock(coord, task_id):
        found = find_task(coord, task_id)
        if found is None:
            raise GreatMindsError(f"task {task_id} not found")
        src_path, queue = found
        if src_path.suffix != ".yaml":
            raise GreatMindsError(f"task {task_id} is legacy .md; migrate first", exit_code=2)
        data = load_task(src_path)

        # 0113: role check BEFORE queue-acceptance check. If the caller
        # is not allowed to produce this block kind regardless, that's
        # the primary error — saying "block kind X is not acceptable in
        # queue Y" misleads users who'd never have been allowed anyway.
        err = role_for_block_kind(role, kind, queue, data)
        if err is not None:
            raise GreatMindsError(err, exit_code=3)
        require_block_acceptable_in_queue(queue, kind)

        block: dict[str, Any] = {
            "kind": kind,
            "by": role,
            "at": now_iso(),
        }
        if isinstance(fields, dict):
            for k, v in fields.items():
                block[k] = v
        else:
            for kv in fields or []:
                if "=" not in kv:
                    raise GreatMindsError(f"--field expects key=value, got: {kv}")
                k, v = kv.split("=", 1)
                block[k] = coerce_value(k, v)
        if body:
            block[body_field_for(kind)] = read_body(body)

        # 0229: auto-stamp worktree_fingerprint for blocks where
        # "what was tested" identity matters. Captures the
        # uncommitted overlay so gate_check can decouple tested-state
        # from committed-state. Skip if the caller supplied one
        # explicitly (allows test/operator override).
        if (kind in ("implementation", "stand_result")
                and "worktree_fingerprint" not in block):
            try:
                from greatminds.cli.gate_check import (
                    compute_worktree_fingerprint,
                )
                fp = compute_worktree_fingerprint(coord.parent)
                if fp is not None:
                    block["worktree_fingerprint"] = fp
            except Exception:
                pass  # best-effort; never block the append

        validate_block(data.get("stream") or "product", block)
        # 0303: refuse implementation / tests blocks when the caller's
        # cwd is not the per-task worktree. Pre-0303 implementers
        # could silently edit main while filing the block (upstream
        # issue #3: TESTER rsync'd from .worktrees/<id>/ where the
        # fix was absent because DEV had edited main). Schema flag
        # ``worktrees.required_for_task_kinds`` lists the product
        # kinds that require isolation.
        _enforce_worktree_isolation_for_block(
            kind, data, coord, task_id,
        )
        require_block_cross_state(block, data)

        # 0185: file-lock acquisition removed. Per-task git worktree
        # isolation makes working-tree contamination impossible — each
        # task edits in its own ``.worktrees/<task-id>/`` directory.

        new_blocks = list(data.get("blocks") or []) + [block]
        new_data = dict(data)
        new_data["blocks"] = new_blocks
        validate_task(new_data)

        atomic_write_yaml(src_path, new_data)
        journal_append(coord, {
            "t": now_iso(),
            "actor": role,
            "task": task_id,
            "from": queue,
            "to": queue,
            "reason": f"append-block {kind}",
            "intent_id": "",
        })
    touch_heartbeat(coord, role)
    return src_path


# ---------------------------------------------------------------------------
# Click facade
# ---------------------------------------------------------------------------


@click.group(help="task-file CRUD (intake, mv, append-block, show, list, validate)")
def task() -> None:
    pass


_ALL_BLOCK_KINDS = sorted(set().union(*STREAM_BLOCK_KINDS.values()))


def _split_multivalue(ctx, param, value):
    """Click callback for list-typed options.

    Click options don't natively accept space-separated values
    (``--hosts X Y`` — argparse style — doesn't work). The supported forms are:

      ``--hosts X --hosts Y``       (repeated flag — idiomatic click)
      ``--hosts X,Y``               (one flag, comma-separated values)
      ``--hosts X,Y --hosts Z``     (mix — flatten + split)
      ``--hosts [X, Y]``            (YAML bracket-list, task 0067 fix)

    All collapse to a flat ``list[str]``. ``None`` if nothing was passed.

    Bracket-list parsing (task 0067): if a single value starts with ``[``
    after strip, try ``yaml.safe_load`` first. The same fix shape as
    task 0035 for ``task append-block --field`` LIST_FIELDS keys. Was
    poisoning ``stand request --evidence-for [<task-id>]`` and ``stand
    result`` evidence chains downstream — gate-check could not match the
    string-literal ``'[<task-id>]'`` against the task identity ``<task-id>``.
    """
    del ctx, param  # unused
    if not value:
        return None
    out: list[str] = []
    for v in value:
        s = str(v).strip()
        # Bracket-list path: yaml.safe_load on a single bracketed token.
        # If it parses to a list, stringify each item and append.
        if s.startswith("["):
            try:
                parsed = yaml.safe_load(s)
            except yaml.YAMLError:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    piece = str(item).strip()
                    if piece:
                        out.append(piece)
                continue
            # Fall through on parse-failure: treat as comma-split fallback.
        for piece in s.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out or None


@task.command(name="new")
@click.option("--stream", required=True,
              type=click.Choice(["product", "review_session"]))
@click.option("--title", required=True)
@click.option("--reporter", default=None)
@click.option("--priority", default=None, type=click.Choice(sorted(PRIORITIES)))
@click.option("--kind", default=None,
              help="product: " + "|".join(sorted(PRODUCT_KINDS)))
@click.option("--scope", default=None,
              help="product: " + "|".join(sorted(PRODUCT_SCOPES)))
@click.option("--hosts", multiple=True, callback=_split_multivalue,
              help="list of hosts; repeat the flag or comma-separate values")
@click.option("--evidence-for", "evidence_for", multiple=True,
              callback=_split_multivalue,
              help="task ids this run is evidence for; repeat or comma-separate")
@click.option("--mode", default=None, type=click.Choice(sorted(MODES)))
@click.option("--target-functionality", "target_functionality", default=None)
@click.option("--scenarios", multiple=True, callback=_split_multivalue,
              help="scenario IDs; repeat or comma-separate")
@click.option("--description", default=None, help="literal | @file | - (stdin)")
@click.option("--in-queue", "in_queue", default=None,
              help="destination queue (default depends on stream)")
@click.option("--seq", default=None, help="override numeric id prefix")
@click.option("--reason", default=None, help="journal reason")
def task_new(stream, title, reporter, priority, kind, scope,
             hosts, evidence_for,
             mode, target_functionality, scenarios,
             description, in_queue, seq, reason) -> None:
    target_path = create_task(
        stream=stream,
        title=title,
        reporter=reporter,
        priority=priority,
        kind=kind,
        scope=scope,
        hosts=hosts,
        evidence_for=evidence_for,
        mode=mode,
        target_functionality=target_functionality,
        scenarios=scenarios,
        description=description,
        in_queue=in_queue,
        seq=seq,
        reason=reason,
    )
    click.echo(f"created {target_path}")


@task.command(name="mv")
@click.argument("task_id", metavar="ID")
@click.argument("to_queue")
@click.option("--reason", default=None)
def task_mv(task_id, to_queue, reason) -> None:
    from_q = move_task(task_id=task_id, to_queue=to_queue, reason=reason)
    click.echo(f"moved {task_id}: {from_q} → {to_queue}")


@task.command(name="append-block")
@click.argument("kind", type=click.Choice(_ALL_BLOCK_KINDS))
@click.option("--id", "task_id", required=True)
@click.option("--field", "fields", multiple=True, help="key=value (repeat)")
@click.option("--body", default=None,
              help="block body: literal text | @PATH (read file) | - (stdin)")
@click.option("--body-file", "body_file", default=None,
              help="block body file path (alias for --body @PATH; "
                   "preserved for orchestrator and old-CLI compat)")
def task_append_block(kind, task_id, fields, body, body_file) -> None:
    if body is not None and body_file is not None:
        raise click.UsageError("pass exactly one of --body or --body-file")
    if body_file is not None:
        body = "-" if body_file == "-" else f"@{body_file}"
    src_path = append_block(
        task_id=task_id,
        kind=kind,
        fields=list(fields) if fields else None,
        body=body,
    )
    click.echo(f"appended {kind} block to {src_path}")


@task.command(name="show")
@click.argument("task_id", metavar="ID")
def task_show(task_id) -> None:
    coord = find_coord_dir()
    found = find_task(coord, task_id)
    if found is None:
        raise GreatMindsError(f"task {task_id} not found")
    src_path, queue = found
    prefix = "# queue" if src_path.suffix == ".yaml" else "# legacy .md, queue"
    click.echo(f"{prefix}: {queue}")
    click.echo(src_path.read_text(encoding="utf-8"))


@task.command(name="list")
@click.argument("queue")
def task_list(queue) -> None:
    coord = find_coord_dir()
    # Heartbeat on read-only list — refreshes the running role's liveness
    # check during long idle stretches. Best-effort; never blocks output.
    try:
        touch_heartbeat(coord, caller_role())
    except GreatMindsError:
        pass
    qdir = coord / queue
    if not qdir.is_dir():
        raise GreatMindsError(f"queue {queue} not found")
    for f in sorted(qdir.iterdir()):
        if f.suffix not in (".yaml", ".md"):
            continue
        if f.name in ("_TEMPLATE.md", "_TEMPLATE.yaml"):
            continue
        click.echo(f.name)


@task.command(name="validate")
@click.argument("task_arg", metavar="ID", required=False, default=None)
@click.option("--id", "id_opt", default=None,
              help="(back-compat) task id — same as the positional ID")
@click.option("--file", "file_path", default=None,
              help="explicit path to a .yaml task file")
def task_validate(task_arg, id_opt, file_path) -> None:
    # 0326: unified id intake — positional ID accepts short id / full
    # filename / path (resolved via find_task), identical to show/paths/
    # mv/append-block. --id / --file kept as back-compat. Exactly one.
    sources = [s for s in (task_arg, id_opt, file_path) if s is not None]
    if len(sources) != 1:
        raise click.UsageError(
            "provide exactly one of: ID (positional), --id, or --file")
    coord = find_coord_dir()
    if file_path is not None:
        path = Path(file_path)
    else:
        arg = task_arg if task_arg is not None else id_opt
        found = find_task(coord, arg)
        if found is None:
            raise GreatMindsError(f"task {arg} not found")
        path = found[0]
    if path.suffix != ".yaml":
        raise GreatMindsError(f"only .yaml supported; got {path.suffix}", exit_code=2)
    validate_task(load_task(path))
    click.echo(f"valid: {path}")


@task.command(name="paths")
@click.argument("task_arg", metavar="[ID]", required=False, default=None)
def task_paths(task_arg) -> None:
    # 0326: with an ID (short id / full filename / path, resolved via
    # find_task) print that task's resolved file path + queue; without
    # an ID print the project's coordination paths (the legacy behavior).
    coord = find_coord_dir()
    if task_arg is not None:
        found = find_task(coord, task_arg)
        if found is None:
            raise GreatMindsError(f"task {task_arg} not found")
        path, queue = found
        click.echo(f"queue: {queue}")
        click.echo(f"path:  {path}")
        return
    canon = find_canon_dir()
    click.echo(f"coord:        {coord}")
    click.echo(f"canon:        {canon}")
    click.echo(f"intent_dir:   {coord / INTENT_DIR_NAME}")
    click.echo(f"journal:      {coord / JOURNAL_NAME}")


if __name__ == "__main__":
    task()
