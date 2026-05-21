#!/usr/bin/env python3
"""bin/task — single entry point for all task-file operations in coordination/.

NEVER edit task files directly. Use this script. It enforces:

  * strict YAML structure (no markdown, no free-form blocks);
  * required fields per stream and per block kind (validated against
    schema.yaml task_kinds);
  * caller-role permission per transition (only the role allowed by
    schema.transitions can mv from a given queue to a given queue);
  * atomic intent → mv → del-intent → journal-append (no half-states);
  * heartbeat side-effect on every successful invocation.

Caller role is taken strictly from $GREATMINDS_ROLE (set per tmux window).
There is no --as override: lying about role is not a feature.

Subcommands:
  new          create a new task (initial intake)
  mv           move task between queues
  append-block <kind>  append a typed block to an existing task
  show         pretty-print a task (file path or id)
  list         list tasks in a queue
  validate     run validation on a task file
  paths        print resolved coordination paths

Exit codes:
   0  ok
   1  user/usage error (missing arg, bad enum, etc.)
   2  validation failure
   3  permission denied (caller role not allowed for this transition)
   4  fs/atomicity failure (intent/mv/journal)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from greatminds.core.paths import find_canon_dir, find_coord_dir
from greatminds.core.util import ISO_FMT, die, now_iso  # noqa: F401  (ISO_FMT used below)


# ---------------------------------------------------------------------------
# Constants and small helpers
# ---------------------------------------------------------------------------

# ISO_FMT now lives in greatminds.core.util (imported above).
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

# F1: only these roles may produce each block kind. `blocked` is special-
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
    # "blocked" handled in validate_role_for_block_kind via current_owner
}

# For `implementation`, the caller role must match the task's `scope:`.
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


# now_iso, die and the path-resolution functions are imported from
# greatminds.core (above). Kept as module-level names for the rest of this
# file so the historical body doesn't need rewriting.


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
        die(1, f"failed to load schema.yaml: {exc}")
        raise SystemExit
    return _schema_cache


def queue_meta(queue: str) -> dict[str, Any]:
    q = (schema().get("queues") or {}).get(queue)
    if not isinstance(q, dict):
        die(1, f"unknown queue: {queue}")
        raise SystemExit
    return q


def role_meta(role: str) -> dict[str, Any]:
    r = (schema().get("roles") or {}).get(role)
    if not isinstance(r, dict):
        die(1, f"unknown role: {role}")
        raise SystemExit
    return r


def transition_for(from_q: str, to_q: str) -> dict[str, Any] | None:
    """Find allowed transition in schema; None if none."""
    for t in schema().get("transitions") or []:
        if not isinstance(t, dict):
            continue
        f, to = t.get("from"), t.get("to")
        if f == from_q and to == to_q:
            return t
        # wildcards
        if f == "any_active_queue" and to_q == to:
            return t
        if f == from_q and to == "any_resume_to_queue":
            return t
    return None


# ---------------------------------------------------------------------------
# Caller identity
# ---------------------------------------------------------------------------


def caller_role() -> str:
    """Task-level wrapper: ``core.paths.caller_role`` + ``schema.yaml`` validation.

    The schema check stays here (not in core) because not every CLI module
    wants to pay the YAML load just to know its role.
    """
    from greatminds.core.paths import caller_role as _bare_caller_role

    role = _bare_caller_role()
    if role not in (schema().get("roles") or {}):
        die(1, f"caller role not in schema: {role}")
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
    """Return (path, queue_name) where task currently sits, or None."""
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
            die(2, f"yaml parse error in {path}: {exc}")
            raise SystemExit
    # legacy .md — parse front-matter + named blocks
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
        die(4, f"journal append failed: {exc}")


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
# Validation
# ---------------------------------------------------------------------------


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


def must_enum(field: str, value: Any, allowed: set[str]) -> None:
    if value not in allowed:
        die(2, f"field '{field}' must be one of {sorted(allowed)}, got: {value!r}")


def must_str(field: str, value: Any, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        die(2, f"field '{field}' must be a non-empty string")


def must_bool(field: str, value: Any) -> None:
    if not isinstance(value, bool):
        die(2, f"field '{field}' must be true|false")


def must_list_of_str(field: str, value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        die(2, f"field '{field}' must be a list of strings")


def must_iso(field: str, value: Any) -> None:
    if not isinstance(value, str) or not re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value
    ):
        die(2, f"field '{field}' must be ISO-8601 (got: {value!r})")


def must_id(value: Any) -> None:
    if not isinstance(value, str) or not ID_RE.match(value):
        die(2, f"id must match {ID_RE.pattern} (got: {value!r})")


def validate_header(data: dict[str, Any]) -> None:
    stream = data.get("stream")
    if stream not in ("product", "stand", "review_session"):
        die(2, f"stream must be product|stand|review_session, got: {stream!r}")
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
            die(2, "stand-stream tasks must have kind: stand_request")
        must_enum("request_type", data.get("request_type"), STAND_REQUEST_TYPES)
        target = data.get("target") or {}
        if not isinstance(target, dict):
            die(2, "target must be a mapping")
        must_enum("target.profile", target.get("profile"), STAND_PROFILES)
        must_list_of_str("target.hosts", target.get("hosts") or [])
        # evidence_for is optional but if present must be list of str ids
        ef = data.get("evidence_for")
        if ef is not None:
            must_list_of_str("evidence_for", ef)
    elif stream == "review_session":
        if data.get("kind") != "review_session":
            die(2, "review_session-stream tasks must have kind: review_session")
        must_enum("mode", data.get("mode"), MODES)
        must_str("target_functionality", data.get("target_functionality"))
        scen = data.get("scenarios")
        if not isinstance(scen, list) or not scen:
            die(2, "scenarios must be a non-empty list")


def validate_block(stream: str, block: dict[str, Any]) -> None:
    kind = block.get("kind")
    allowed = STREAM_BLOCK_KINDS.get(stream, set())
    if kind not in allowed:
        die(2, f"block kind {kind!r} not allowed in stream {stream!r}; allowed: {sorted(allowed)}")
    must_str("block.by", block.get("by"))
    must_iso("block.at", block.get("at"))
    # per-kind required fields
    if kind == "plan":
        must_str("base_commit", block.get("base_commit"))
        must_str("assignee_role", block.get("assignee_role"))
        must_bool("stand_required", block.get("stand_required"))
        must_enum("plan_kind", block.get("plan_kind"), PLAN_KINDS)
        must_enum("mode", block.get("mode"), MODES)
        must_bool("ready_for_implementation", block.get("ready_for_implementation"))
        # A3: if stand_required, must justify why.
        if block.get("stand_required") is True and not (block.get("stand_reason") or "").strip():
            die(2, "plan with stand_required=true must include stand_reason")
    elif kind == "implementation":
        must_str("base_commit", block.get("base_commit"))
        must_list_of_str("files", block.get("files") or [])
        must_bool("ready_for_test", block.get("ready_for_test"))
    elif kind == "tests":
        must_str("base_commit", block.get("base_commit"))
        must_list_of_str("test_files", block.get("test_files") or [])
        # A1: TESTER must list at least one test file — empty list means
        # "I ran nothing" and is incompatible with a tests block.
        if not (block.get("test_files") or []):
            die(2, "tests block requires at least one entry in test_files")
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
        # commit exists only for an approval. A changes_requested review
        # is a hand-back — nothing was committed, so commit is empty.
        # Requiring it unconditionally bricked every bounced task: the
        # whole-file validation that append-block/mv run would then
        # reject the DEVELOPER's re-implementation + re-handoff.
        if block.get("outcome") == "approved":
            must_str("commit", block.get("commit"))
    elif kind == "blocked":
        must_str("reason", block.get("reason"))
        deps = block.get("dependencies") or []
        must_list_of_str("dependencies", deps)
        if not deps:
            die(2, "blocked block requires at least one dependency")
        # validate format AND that referenced queue is a known queue
        dep_re = re.compile(r"^([a-z_]+)/([0-9]{1,4}-[a-z0-9-]+)\.(yaml|md)$")
        known_queues = set((schema().get("queues") or {}).keys())
        for d in deps:
            m = dep_re.match(d)
            if m is None:
                die(2, f"dependency {d!r} must look like <queue>/<id>.{{yaml,md}}")
            if m.group(1) not in known_queues:
                die(2, f"dependency {d!r}: unknown queue {m.group(1)!r}")
        must_str("resume_to", block.get("resume_to"))
        # resume_to must be a known queue
        if block.get("resume_to") not in known_queues:
            die(2, f"resume_to: {block.get('resume_to')!r} is not a known queue")
    elif kind == "stand_result":
        must_enum("result", block.get("result"), STAND_RESULTS)
        must_enum("stand_status", block.get("stand_status"), STAND_STATUSES)
        must_str("commit", block.get("commit"))
        must_enum("profile", block.get("profile"), STAND_PROFILES)
    elif kind == "session_iteration":
        must_str("summary", block.get("summary"))
    elif kind == "triage":
        pass  # triage block needs only by/at/notes (validated above)


def validate_task(data: dict[str, Any]) -> None:
    validate_header(data)
    blocks = data.get("blocks") or []
    if not isinstance(blocks, list):
        die(2, "blocks: must be a list")
    stream = data["stream"]
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            die(2, f"blocks[{i}] must be a mapping")
        validate_block(stream, b)


# ---------------------------------------------------------------------------
# Permission: can role move from from_q to to_q?
# ---------------------------------------------------------------------------


def can_role_move(role: str, from_q: str, to_q: str, task_data: dict[str, Any]) -> str | None:
    """Return None if allowed, error message string otherwise."""
    t = transition_for(from_q, to_q)
    if t is None:
        return f"no transition {from_q} → {to_q} in schema"
    by = t.get("by")
    if by == "current_owner":
        owner = (queue_meta(from_q).get("owner") or "").upper()
        if owner and owner != role and role not in (queue_meta(from_q).get("writers") or []):
            return f"role {role} is not current owner of {from_q} (owner: {owner})"
        return None
    if isinstance(by, str) and by != role:
        return f"only role {by} may perform {from_q} → {to_q}, not {role}"
    return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


TITLE_MAX_LEN = 200

# B5: --in-queue is restricted to known intake queues per stream.
ALLOWED_INTAKE_QUEUES: dict[str, set[str]] = {
    "product":         {"feature_inbox", "user_feedback"},
    "stand":           {"stand_requests"},
    "review_session":  {"review_sessions"},
}


def cmd_new(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    role = caller_role()

    stream = args.stream
    if stream not in ("product", "stand", "review_session"):
        die(1, f"--stream must be product|stand|review_session")

    # E1: bound title length so an accidental dump doesn't fill the file.
    if len(args.title) > TITLE_MAX_LEN:
        die(1, f"title too long ({len(args.title)} chars, max {TITLE_MAX_LEN})")

    # generate id: <seq>-<slug>
    slug = re.sub(r"[^a-z0-9]+", "-", args.title.lower()).strip("-")[:60]
    if not slug:
        # title was entirely non-ascii / control chars / empty after slugify
        # fall back to a short hash so the id stays valid.
        slug = "task-" + uuid.uuid4().hex[:8]
    seq = args.seq or next_seq(coord)
    if not re.match(r"^[0-9]{4}$", seq):
        die(1, f"--seq must be a 4-digit non-negative number, got: {seq!r}")
    task_id = f"{seq}-{slug}"

    data: dict[str, Any] = {
        "id": task_id,
        "stream": stream,
        "title": args.title,
        "reporter": args.reporter or role,
        "opened_at": now_iso(),
        "priority": args.priority or "normal",
    }
    if stream == "product":
        if not args.kind or not args.scope:
            die(1, "product stream needs --kind and --scope")
        data["kind"] = args.kind
        data["scope"] = args.scope
    elif stream == "stand":
        data["kind"] = "stand_request"
        if not args.request_type:
            die(1, "stand stream needs --request-type")
        data["request_type"] = args.request_type
        data["target"] = {
            "profile": args.profile or "full-deploy",
            "hosts": args.hosts or [],
        }
        if args.evidence_for:
            data["evidence_for"] = args.evidence_for
    elif stream == "review_session":
        data["kind"] = "review_session"
        data["mode"] = args.mode or "B"
        if not args.target_functionality:
            die(1, "review_session needs --target-functionality")
        data["target_functionality"] = args.target_functionality
        data["scenarios"] = args.scenarios or []

    if args.description:
        data["description"] = read_body(args.description)
    data["blocks"] = []

    # which queue?
    in_queue = args.in_queue or default_intake_queue(stream)
    # B5: only intake queues are allowed for `new`. Creating tasks
    # directly in feature_review / verified / archive / etc. bypasses
    # the entire pipeline and is never legitimate.
    allowed = ALLOWED_INTAKE_QUEUES.get(stream, set())
    if in_queue not in allowed:
        die(1, f"--in-queue {in_queue!r} not allowed for stream {stream!r}; "
               f"allowed intake: {sorted(allowed)}")
    target_path = coord / in_queue / f"{task_id}.yaml"
    if target_path.exists():
        die(1, f"task {task_id} already exists at {target_path}")

    validate_task(data)

    atomic_write_yaml(target_path, data)
    journal_append(coord, {
        "t": now_iso(),
        "actor": role,
        "task": task_id,
        "from": "_new",
        "to": in_queue,
        "reason": args.reason or f"new {stream} task",
        "intent_id": "",
    })
    touch_heartbeat(coord, role)
    print(f"created {target_path}")
    return 0


def default_intake_queue(stream: str) -> str:
    return {
        "product": "feature_inbox",
        "stand": "stand_requests",
        "review_session": "review_sessions",
    }[stream]


import fcntl
from contextlib import contextmanager


@contextmanager
def task_file_lock(coord: Path, task_id: str):
    """Exclusive lock for read-modify-write on a single task file.

    Held via a sibling `.task.<id>.lock` so the lock survives the rename
    that mv does. Released on context exit.
    """
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


def next_seq(coord: Path) -> str:
    """Find max numeric prefix in queues + counter file, return next.

    Uses an exclusive flock on coord/.id_counter so two concurrent
    bin/task new invocations don't collide on the same id. The counter
    file caches the last issued id; on first use we seed it from the
    actual filesystem scan.
    """
    lock_path = coord / ".id_counter"
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # read current counter
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64).decode("ascii", errors="ignore").strip()
        cached = int(raw) if raw.isdigit() else 0
        # rescan FS in case files were created out-of-band
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
        # write back
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
    """If spec starts with '@' it's a file path; if '-' it's stdin; else literal."""
    if spec == "-":
        return sys.stdin.read()
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


def cmd_mv(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    role = caller_role()
    task_id = args.id
    to_q = args.to_queue

    # Hold the per-task lock so an append-block can't race with us.
    with task_file_lock(coord, task_id):
        return _do_mv(coord, role, task_id, to_q, args.reason or "")


def _do_mv(coord: Path, role: str, task_id: str, to_q: str, reason: str) -> int:
    found = find_task(coord, task_id)
    if found is None:
        die(1, f"task {task_id} not found in any queue")
    src_path, from_q = found

    if from_q == to_q:
        die(1, f"task already in {to_q}")

    # validate destination queue exists
    if to_q not in (schema().get("queues") or {}):
        die(1, f"unknown destination queue: {to_q}")

    # load task data (also for validation)
    data = load_task(src_path)
    if data.get("_legacy_md"):
        die(2, f"task {task_id} is legacy .md; migrate before moving")

    # check transition permission
    err = can_role_move(role, from_q, to_q, data)
    if err is not None:
        die(3, err)

    # F3: if routing feature_plan → per-scope queue, scope must match
    require_scope_match_on_routing(data, from_q, to_q)

    # check required block(s) for target — minimal: ready_for_* flags on
    # the most recent block of the appropriate kind.
    require_target_readiness(data, from_q, to_q)

    # intent write
    intent_path = intent_write(coord, role, task_id, from_q, to_q, reason)
    intent_id = intent_path.stem.rsplit("-", 1)[-1]

    # mv
    dst_path = coord / to_q / src_path.name
    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src_path, dst_path)
    except OSError as exc:
        intent_clear(intent_path)
        die(4, f"mv failed: {exc}")

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
    print(f"moved {task_id}: {from_q} → {to_q}")
    return 0


READY_FLAG_PER_TARGET: dict[str, tuple[str, str]] = {
    # to_q: (latest_block_kind_needed, ready_flag_field).
    # PLANNER writes the plan block IN feature_plan, so mv into
    # feature_plan has no readiness prerequisite. mv from feature_plan
    # into a per-scope queue requires the plan to be marked ready.
    "feature_dev":          ("plan",            "ready_for_implementation"),
    "feature_ui_dev":       ("plan",            "ready_for_implementation"),
    "feature_docs":         ("plan",            "ready_for_implementation"),
    "feature_test":         ("implementation",  "ready_for_test"),
    # feature_docs_review and feature_review are special-cased in
    # require_target_readiness (dual-source: writer-path vs audit-only /
    # tests-path vs reader-path).
}


def latest_plan(data: dict[str, Any]) -> dict[str, Any] | None:
    plans = [b for b in (data.get("blocks") or [])
             if isinstance(b, dict) and b.get("kind") == "plan"]
    return plans[-1] if plans else None


def is_audit_only(data: dict[str, Any]) -> bool:
    p = latest_plan(data)
    return bool(p and p.get("audit_only") is True)


def require_target_readiness(data: dict[str, Any], from_q: str, to_q: str) -> None:
    """Some transitions require the most-recent block to have a ready_for_* flag.

    Special cases:
      - feature_review accepts from feature_test (tests.ready_for_review)
        OR feature_docs_review (reader_review.ready_for_architect).
      - feature_docs_review accepts from feature_docs (WRITER path:
        implementation.ready_for_test) OR feature_plan (PLANNER
        audit-only path: plan.ready_for_implementation + plan.audit_only).
    The rule depends on the SOURCE queue, not just the target.
    """
    # B1: triage block required before mv inbox → feature_plan, so the
    # intake step is auditable in the task history.
    if to_q == "feature_plan" and from_q == "feature_inbox":
        blocks = data.get("blocks") or []
        if not any(isinstance(b, dict) and b.get("kind") == "triage" for b in blocks):
            die(2, "mv feature_inbox → feature_plan requires a triage block first")

    # B: an audit-only task must never be routed into feature_docs
    # (WRITER's queue) — it has no write-plan; WRITER can't act on it.
    # READER records findings; the audit itself flows to feature_review
    # then verified, and PLANNER spawns a SEPARATE feature_docs write
    # task from the findings.
    if to_q == "feature_docs" and is_audit_only(data):
        die(2, "audit-only task: do NOT route the audit into feature_docs. "
               "Its findings become a separate feature_docs write task "
               "(PLANNER triages). The audit flows feature_docs_review → "
               "feature_review → verified.")

    # A: feature_docs_review dual-source readiness.
    if to_q == "feature_docs_review":
        if from_q == "feature_docs":
            block_kind, flag = "implementation", "ready_for_test"
        elif from_q == "feature_plan":
            # audit-only path
            p = latest_plan(data)
            if not (p and p.get("audit_only") is True):
                die(2, "mv feature_plan → feature_docs_review requires the "
                       "latest plan block to set audit_only: true (this is "
                       "the independent READER-audit path)")
            if not p.get("ready_for_implementation"):
                die(2, "mv feature_plan → feature_docs_review requires "
                       "plan.ready_for_implementation=true")
            return
        elif from_q == "feature_blocked":
            return
        else:
            die(2, f"mv {from_q} → feature_docs_review not allowed")
        blocks = data.get("blocks") or []
        matching = [b for b in blocks
                    if isinstance(b, dict) and b.get("kind") == block_kind]
        if not matching:
            die(2, f"mv → feature_docs_review (from {from_q}) requires "
                   f"{block_kind} block")
        if not matching[-1].get(flag):
            die(2, f"mv → feature_docs_review (from {from_q}) requires "
                   f"{block_kind}.{flag}=true")
        return

    if to_q == "feature_review":
        if from_q == "feature_test":
            block_kind, flag = "tests", "ready_for_review"
        elif from_q == "feature_docs_review":
            block_kind, flag = "reader_review", "ready_for_architect"
        elif from_q == "feature_blocked":
            # resuming from blocked: prior readiness already established;
            # caller's decision.
            return
        else:
            die(2, f"mv {from_q} → feature_review not allowed; route via "
                   f"feature_test or feature_docs_review")
        blocks = data.get("blocks") or []
        matching = [b for b in blocks
                    if isinstance(b, dict) and b.get("kind") == block_kind]
        if not matching:
            die(2, f"mv → feature_review (from {from_q}) requires {block_kind} block")
        latest = matching[-1]
        if not latest.get(flag):
            die(2, f"mv → feature_review (from {from_q}) requires "
                   f"{block_kind}.{flag}=true")
        return

    rule = READY_FLAG_PER_TARGET.get(to_q)
    if not rule:
        return
    block_kind, flag = rule
    blocks = data.get("blocks") or []
    # find LATEST block of the kind
    matching = [b for b in blocks if isinstance(b, dict) and b.get("kind") == block_kind]
    if not matching:
        die(2, f"mv → {to_q} requires {block_kind} block on task")
    latest = matching[-1]
    if not latest.get(flag):
        die(2, f"mv → {to_q} requires {block_kind}.{flag}=true")


# F3: when ARCHITECT-PLANNER routes from feature_plan → feature_{dev,ui_dev,docs},
# task.scope must match the destination queue.
SCOPE_TO_QUEUE: dict[str, str] = {
    "backend": "feature_dev",
    "ui":      "feature_ui_dev",
    "docs":    "feature_docs",
}


def require_scope_match_on_routing(data: dict[str, Any], from_q: str, to_q: str) -> None:
    if from_q != "feature_plan":
        return
    if to_q not in SCOPE_TO_QUEUE.values():
        return
    scope = data.get("scope")
    expected = SCOPE_TO_QUEUE.get(scope)
    if expected is None:
        die(2, f"task scope {scope!r} has no per-scope queue routing")
    if expected != to_q:
        die(2, f"task scope: {scope!r} routes to {expected}, not {to_q}")


# F1: role-per-block-kind validation.
def role_for_block_kind(role: str, kind: str, queue: str,
                        data: dict[str, Any]) -> str | None:
    """Return None if role may author this block-kind on this task,
    otherwise an error message."""
    if kind == "blocked":
        # blocked block authored by the current owner of the queue the
        # task sits in (per schema.queues[<queue>].owner).
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
    # F1 cont'd: implementation block also requires scope/role match.
    if kind == "implementation":
        scope = data.get("scope")
        expected = IMPL_ROLE_BY_SCOPE.get(scope or "")
        if expected and expected != role:
            return (f"task scope: {scope!r} requires {expected} for "
                    f"implementation, not {role}")
    return None


def require_block_cross_state(new_block: dict[str, Any], data: dict[str, Any]) -> None:
    """A2: cross-block consistency at append time.

    review.outcome=approved requires the most recent testing-side block
    (tests or reader_review) to be a pass. Prevents REVIEWER from
    rubber-stamping a task whose tests just failed.
    """
    kind = new_block.get("kind")
    if kind != "review":
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
            die(2, f"cannot approve: latest tests.test_result="
                   f"{latest_tests.get('test_result')!r} (expected 'pass')")
        return
    if latest_reader is not None:
        if latest_reader.get("outcome") != "pass":
            die(2, f"cannot approve: latest reader_review.outcome="
                   f"{latest_reader.get('outcome')!r} (expected 'pass')")
        return
    die(2, "cannot approve: no tests or reader_review block on this task")


# F5: queue → set of block kinds that may be appended to a task currently
# in that queue.
def require_block_acceptable_in_queue(queue: str, kind: str) -> None:
    allowed = QUEUE_BLOCK_KINDS.get(queue)
    if allowed is None:
        die(2, f"queue {queue!r} has no block-policy entry")
    if not allowed:
        die(2, f"queue {queue!r} is terminal — no new blocks accepted")
    if kind not in allowed:
        die(2, f"queue {queue!r} accepts {sorted(allowed)}, not {kind!r}")


def cmd_append_block(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    role = caller_role()
    task_id = args.id

    # Hold the per-task lock for the whole read-modify-write so
    # concurrent appends don't lose updates.
    with task_file_lock(coord, task_id):
        found = find_task(coord, task_id)
        if found is None:
            die(1, f"task {task_id} not found")
        src_path, queue = found
        if src_path.suffix != ".yaml":
            die(2, f"task {task_id} is legacy .md; migrate first")
        data = load_task(src_path)

        # F5: this block kind must be acceptable in the current queue
        require_block_acceptable_in_queue(queue, args.kind)
        # F1: role must be allowed to author this block kind on this task
        err = role_for_block_kind(role, args.kind, queue, data)
        if err is not None:
            die(3, err)

        # parse --field foo=bar entries
        block: dict[str, Any] = {
            "kind": args.kind,
            "by": role,
            "at": now_iso(),
        }
        for kv in args.field or []:
            if "=" not in kv:
                die(1, f"--field expects key=value, got: {kv}")
            k, v = kv.split("=", 1)
            block[k] = coerce_value(k, v)
        if args.body:
            block_body_field = body_field_for(args.kind)
            block[block_body_field] = read_body(args.body)

        # validate block
        validate_block(data.get("stream") or "product", block)

        # A2: cross-block consistency (e.g. approve requires tests pass)
        require_block_cross_state(block, data)

        # validate whole task with this block appended
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
            "reason": f"append-block {args.kind}",
            "intent_id": "",
        })
    touch_heartbeat(coord, role)
    print(f"appended {args.kind} block to {src_path}")
    return 0


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


LIST_FIELDS = {
    "dependencies", "files", "test_files", "hosts", "scenarios",
    "evidence_for", "docs_checked", "bugs_filed", "commands",
}


def coerce_value(key: str, v: str) -> Any:
    """Best-effort coerce based on field name + value shape."""
    if key in LIST_FIELDS:
        if "," in v:
            return [x.strip() for x in v.split(",") if x.strip()]
        return [v] if v else []
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.isdigit():
        return int(v)
    if "," in v:
        return [x.strip() for x in v.split(",") if x.strip()]
    return v


def cmd_show(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    found = find_task(coord, args.id)
    if found is None:
        die(1, f"task {args.id} not found")
    src_path, queue = found
    if src_path.suffix == ".yaml":
        print(f"# queue: {queue}")
        print(src_path.read_text(encoding="utf-8"))
    else:
        print(f"# legacy .md, queue: {queue}")
        print(src_path.read_text(encoding="utf-8"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    qdir = coord / args.queue
    if not qdir.is_dir():
        die(1, f"queue {args.queue} not found")
    for f in sorted(qdir.iterdir()):
        if f.suffix not in (".yaml", ".md"):
            continue
        if f.name == "_TEMPLATE.md" or f.name == "_TEMPLATE.yaml":
            continue
        print(f.name)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    if args.file:
        path = Path(args.file)
    else:
        found = find_task(coord, args.id)
        if found is None:
            die(1, f"task {args.id} not found")
        path = found[0]
    if path.suffix != ".yaml":
        die(2, f"only .yaml supported; got {path.suffix}")
    data = load_task(path)
    validate_task(data)
    print(f"valid: {path}")
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    canon = find_canon_dir()
    print(f"coord:        {coord}")
    print(f"canon:        {canon}")
    print(f"intent_dir:   {coord / INTENT_DIR_NAME}")
    print(f"journal:      {coord / JOURNAL_NAME}")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# click facade — every legacy cmd_X(args) is wrapped into a click subcommand
# of the `task` group. SimpleNamespace bridges click-options to the
# argparse.Namespace shape the cmd_X bodies expect — keeps the 7 large
# handler functions untouched.
# ---------------------------------------------------------------------------

import click
from types import SimpleNamespace


@click.group(help="task-file CRUD (intake, mv, append-block, show, list, validate)")
def task() -> None:
    pass


_ALL_BLOCK_KINDS = sorted(set().union(*STREAM_BLOCK_KINDS.values()))


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
@click.option("--hosts", multiple=True)
@click.option("--evidence-for", "evidence_for", multiple=True)
@click.option("--mode", default=None, type=click.Choice(sorted(MODES)))
@click.option("--target-functionality", "target_functionality", default=None)
@click.option("--scenarios", multiple=True)
@click.option("--description", default=None, help="literal | @file | - (stdin)")
@click.option("--in-queue", "in_queue", default=None,
              help="destination queue (default depends on stream)")
@click.option("--seq", default=None, help="override numeric id prefix")
@click.option("--reason", default=None, help="journal reason")
def _task_new(**kw) -> None:
    # multiple=True → tuple; argparse-style code expects list-or-None
    for k in ("hosts", "evidence_for", "scenarios"):
        v = kw.get(k)
        kw[k] = list(v) if v else None
    rc = cmd_new(SimpleNamespace(**kw))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="mv")
@click.argument("id")
@click.argument("to_queue")
@click.option("--reason", default=None)
def _task_mv(id, to_queue, reason) -> None:
    rc = cmd_mv(SimpleNamespace(id=id, to_queue=to_queue, reason=reason))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="append-block")
@click.argument("kind", type=click.Choice(_ALL_BLOCK_KINDS))
@click.option("--id", required=True)
@click.option("--field", multiple=True, help="key=value (repeat)")
@click.option("--body", default=None, help="literal | @file | - (stdin)")
def _task_append_block(kind, id, field, body) -> None:
    rc = cmd_append_block(SimpleNamespace(
        kind=kind, id=id, field=list(field) if field else None, body=body,
    ))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="show")
@click.argument("id")
def _task_show(id) -> None:
    rc = cmd_show(SimpleNamespace(id=id))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="list")
@click.argument("queue")
def _task_list(queue) -> None:
    rc = cmd_list(SimpleNamespace(queue=queue))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="validate")
@click.option("--id", default=None)
@click.option("--file", "file", default=None)
def _task_validate(id, file) -> None:
    if (id is None) == (file is None):
        raise click.UsageError("exactly one of --id or --file is required")
    rc = cmd_validate(SimpleNamespace(id=id, file=file))
    if rc:
        raise click.exceptions.Exit(rc)


@task.command(name="paths")
def _task_paths() -> None:
    rc = cmd_paths(SimpleNamespace())
    if rc:
        raise click.exceptions.Exit(rc)


if __name__ == "__main__":
    task()
