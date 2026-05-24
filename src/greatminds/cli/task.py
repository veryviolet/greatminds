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

# Stream → set of block kinds it allows.
STREAM_BLOCK_KINDS: dict[str, set[str]] = {
    "product": {
        "triage",
        "plan",
        "implementation",
        "tests",
        "reader_review",
        "review",
        "blocked",
    },
    "stand": {"stand_result", "blocked"},
    "review_session": {"session_iteration", "blocked"},
}

# F1: only these roles may produce each block kind. ``blocked`` is special-
# cased — the current owner of the queue where the task sits is the only
# role allowed to mark it blocked.
BLOCK_KIND_ROLES: dict[str, set[str]] = {
    "triage":            {"ARCHITECT-PLANNER"},
    "plan":              {"ARCHITECT-PLANNER"},
    "implementation":    {"DEVELOPER", "UI-DEVELOPER", "TECHNICAL-WRITER"},
    "tests":             {"TESTER"},
    "reader_review":     {"READER"},
    "review":            {"ARCHITECT-REVIEWER"},
    "stand_result":      {"STAND-KEEPER"},
    "session_iteration": {"EXPLORER"},
    # ``blocked`` handled in role_for_block_kind via current_owner
}

# For ``implementation``, the caller role must match the task's ``scope:``.
IMPL_ROLE_BY_SCOPE: dict[str, str] = {
    "backend": "DEVELOPER",
    "ui":      "UI-DEVELOPER",
    "docs":    "TECHNICAL-WRITER",
}

# F5: which block kinds may be APPENDED to a task currently sitting in
# each queue. Terminal queues accept nothing.
QUEUE_BLOCK_KINDS: dict[str, set[str]] = {
    "feature_inbox":         {"triage", "blocked"},
    "feature_plan":          {"plan", "blocked"},
    "feature_dev":           {"implementation", "blocked"},
    "feature_ui_dev":        {"implementation", "blocked"},
    "feature_docs":          {"implementation", "blocked"},
    "feature_test":          {"tests", "blocked"},
    "feature_docs_review":   {"reader_review", "blocked"},
    "feature_review":        {"review", "blocked"},
    "feature_blocked":       {"blocked"},
    "user_feedback":         {"triage", "blocked"},
    "review_sessions":       {"session_iteration", "blocked"},
    "stand_requests":        {"blocked"},
    "stand_wip":             {"stand_result", "blocked"},
    # terminal:
    "verified":              set(),
    "archive":               set(),
    "stand_done":            set(),
}

PRODUCT_KINDS = {"feature", "bugfix", "docs", "ops", "research"}
PRODUCT_SCOPES = {"backend", "ui", "docs", "stand", "research"}
PRIORITIES = {"low", "normal", "high"}
PLAN_KINDS = {"full", "bugfix"}
MODES = {"A", "B", "C"}
STAND_REQUEST_TYPES = {
    "deploy", "restart", "rebuild", "smoke",
    "remote_sync", "gpu_check", "teardown",
}
STAND_PROFILES = {"full-deploy", "vite-dev"}
STAND_RESULTS = {"ok", "partial", "fail"}
STAND_STATUSES = {"READY", "DEGRADED", "DOWN", "BLOCKED"}
TEST_RESULTS = {"pass", "fail", "partial"}
GATE_CHECK_RESULTS = {"pass", "fail", "missing", "n/a"}
REVIEW_OUTCOMES = {"approved", "changes_requested"}
READER_OUTCOMES = {"pass", "fail", "partial"}

TITLE_MAX_LEN = 200

# B5: ``--in-queue`` is restricted to known intake queues per stream.
ALLOWED_INTAKE_QUEUES: dict[str, set[str]] = {
    "product":         {"feature_inbox", "user_feedback"},
    "stand":           {"stand_requests"},
    "review_session":  {"review_sessions"},
}

# Fields whose ``--field key=value`` form is expected to carry a list.
# Anything else is left as a string even if it contains commas (so prose
# values like ``stand_reason="POST /x, then GET /y"`` aren't fragmented).
LIST_FIELDS = {
    "dependencies", "files", "test_files", "hosts", "scenarios",
    "evidence_for", "docs_checked", "bugs_filed", "commands",
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


def transitions_for(from_q: str, to_q: str) -> list[dict[str, Any]]:
    """All schema rows matching the (from, to) pair.

    Multiple rows may share the same ``(from, to)`` with different ``by:``
    roles — e.g. ``review_sessions → archive`` legitimately permits both
    ARCHITECT-PLANNER (intake archive) and EXPLORER (self-close after AC
    campaign). Returning only the first row, as ``transition_for`` did,
    silently blocks any role that appears second in ``schema.yaml``.

    Wildcards resolved here:
      ``from == "any_active_queue"``       → matches any concrete ``from_q``.
      ``to   == "any_resume_to_queue"``    → matches any concrete ``to_q``.
    """
    matches: list[dict[str, Any]] = []
    for t in schema().get("transitions") or []:
        if not isinstance(t, dict):
            continue
        f, to = t.get("from"), t.get("to")
        f_ok = (f == from_q) or (f == "any_active_queue")
        to_ok = (to == to_q) or (to == "any_resume_to_queue")
        if f_ok and to_ok:
            matches.append(t)
    return matches


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


def find_task(coord: Path, task_id: str) -> tuple[Path, str] | None:
    """Return ``(path, queue_name)`` where task currently sits, or ``None``."""
    for q in coord.iterdir():
        if not q.is_dir() or q.name.startswith("."):
            continue
        if q.name in ("intent", "inbox"):
            continue
        for ext in (".yaml", ".md"):
            p = q / f"{task_id}{ext}"
            if p.is_file():
                return p, q.name
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


@contextmanager
def task_file_lock(coord: Path, task_id: str):
    """Exclusive lock for read-modify-write on a single task file."""
    lock_dir = coord / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{task_id}.lock"
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
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
    if stream not in ("product", "stand", "review_session"):
        raise GreatMindsError(
            f"stream must be product|stand|review_session, got: {stream!r}"
        , exit_code=2)
    must_id(data.get("id"))
    must_str("title", data.get("title"))
    must_str("reporter", data.get("reporter"))
    must_iso("opened_at", data.get("opened_at"))
    must_enum("priority", data.get("priority"), PRIORITIES)

    if stream == "product":
        must_enum("kind", data.get("kind"), PRODUCT_KINDS)
        must_enum("scope", data.get("scope"), PRODUCT_SCOPES)
    elif stream == "stand":
        if data.get("kind") != "stand_request":
            raise GreatMindsError("stand-stream tasks must have kind: stand_request", exit_code=2)
        must_enum("request_type", data.get("request_type"), STAND_REQUEST_TYPES)
        target = data.get("target") or {}
        if not isinstance(target, dict):
            raise GreatMindsError("target must be a mapping", exit_code=2)
        must_enum("target.profile", target.get("profile"), STAND_PROFILES)
        must_list_of_str("target.hosts", target.get("hosts") or [])
        ef = data.get("evidence_for")
        if ef is not None:
            must_list_of_str("evidence_for", ef)
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
    elif kind == "stand_result":
        must_enum("result", block.get("result"), STAND_RESULTS)
        must_enum("stand_status", block.get("stand_status"), STAND_STATUSES)
        must_str("commit", block.get("commit"))
        must_enum("profile", block.get("profile"), STAND_PROFILES)
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


def require_block_cross_state(new_block: dict[str, Any],
                              data: dict[str, Any]) -> None:
    """A2: REVIEWER cannot approve when latest testing block is not pass."""
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
    return {
        "product": "feature_inbox",
        "stand": "stand_requests",
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
    """
    if key in LIST_FIELDS:
        if "," in v:
            return [x.strip() for x in v.split(",") if x.strip()]
        return [v] if v else []
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
    request_type: str | None = None,
    profile: str | None = None,
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
    role = caller_role()

    if stream not in ("product", "stand", "review_session"):
        raise GreatMindsError("--stream must be product|stand|review_session")

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
    elif stream == "stand":
        data["kind"] = "stand_request"
        if not request_type:
            raise GreatMindsError("stand stream needs --request-type")
        data["request_type"] = request_type
        data["target"] = {
            "profile": profile or "full-deploy",
            "hosts": hosts or [],
        }
        if evidence_for:
            data["evidence_for"] = evidence_for
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


def _do_move(coord: Path, role: str, task_id: str,
             to_q: str, reason: str) -> str:
    found = find_task(coord, task_id)
    if found is None:
        raise GreatMindsError(f"task {task_id} not found in any queue")
    src_path, from_q = found

    if from_q == to_q:
        raise GreatMindsError(f"task already in {to_q}")

    if to_q not in (schema().get("queues") or {}):
        raise GreatMindsError(f"unknown destination queue: {to_q}")

    data = load_task(src_path)
    if data.get("_legacy_md"):
        raise GreatMindsError(f"task {task_id} is legacy .md; migrate before moving", exit_code=2)

    err = can_role_move(role, from_q, to_q, data)
    if err is not None:
        raise GreatMindsError(err, exit_code=3)

    require_scope_match_on_routing(data, from_q, to_q)
    require_target_readiness(data, from_q, to_q)

    intent_path = intent_write(coord, role, task_id, from_q, to_q, reason)
    intent_id = intent_path.stem.rsplit("-", 1)[-1]

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
    touch_heartbeat(coord, role)
    return from_q


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

        require_block_acceptable_in_queue(queue, kind)
        err = role_for_block_kind(role, kind, queue, data)
        if err is not None:
            raise GreatMindsError(err, exit_code=3)

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

        validate_block(data.get("stream") or "product", block)
        require_block_cross_state(block, data)

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

    All collapse to a flat ``list[str]``. ``None`` if nothing was passed.
    """
    del ctx, param  # unused
    if not value:
        return None
    out: list[str] = []
    for v in value:
        for piece in str(v).split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out or None


@task.command(name="new")
@click.option("--stream", required=True,
              type=click.Choice(["product", "stand", "review_session"]))
@click.option("--title", required=True)
@click.option("--reporter", default=None)
@click.option("--priority", default=None, type=click.Choice(sorted(PRIORITIES)))
@click.option("--kind", default=None,
              help="product: " + "|".join(sorted(PRODUCT_KINDS)))
@click.option("--scope", default=None,
              help="product: " + "|".join(sorted(PRODUCT_SCOPES)))
@click.option("--request-type", "request_type", default=None,
              type=click.Choice(sorted(STAND_REQUEST_TYPES)))
@click.option("--profile", default=None, type=click.Choice(sorted(STAND_PROFILES)))
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
             request_type, profile, hosts, evidence_for,
             mode, target_functionality, scenarios,
             description, in_queue, seq, reason) -> None:
    target_path = create_task(
        stream=stream,
        title=title,
        reporter=reporter,
        priority=priority,
        kind=kind,
        scope=scope,
        request_type=request_type,
        profile=profile,
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
@click.option("--id", "task_id", default=None)
@click.option("--file", "file_path", default=None)
def task_validate(task_id, file_path) -> None:
    if (task_id is None) == (file_path is None):
        raise click.UsageError("exactly one of --id or --file is required")
    coord = find_coord_dir()
    if file_path is not None:
        path = Path(file_path)
    else:
        found = find_task(coord, task_id)
        if found is None:
            raise GreatMindsError(f"task {task_id} not found")
        path = found[0]
    if path.suffix != ".yaml":
        raise GreatMindsError(f"only .yaml supported; got {path.suffix}", exit_code=2)
    validate_task(load_task(path))
    click.echo(f"valid: {path}")


@task.command(name="paths")
def task_paths() -> None:
    coord = find_coord_dir()
    canon = find_canon_dir()
    click.echo(f"coord:        {coord}")
    click.echo(f"canon:        {canon}")
    click.echo(f"intent_dir:   {coord / INTENT_DIR_NAME}")
    click.echo(f"journal:      {coord / JOURNAL_NAME}")


if __name__ == "__main__":
    task()
