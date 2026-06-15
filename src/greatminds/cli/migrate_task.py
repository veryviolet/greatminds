#!/usr/bin/env python3
"""greatminds migrate-task — convert markdown task files to strict .yaml.

Input format: a markdown file with multiple YAML blocks separated
by `---`, optionally interleaved with free prose.
  * the first dict-shaped YAML block is the header (id, stream, kind, ...);
  * subsequent dict-shaped YAML blocks whose only top-level key is a known
    "<kind>_block" name are progress blocks;
  * everything else (prose, headings) becomes the task's `description` field.

New format: single YAML file with header fields at top level and an
ordered `blocks:` list. See `greatminds task show` output and the packaged
schema for the exact shape.

Usage:
  greatminds migrate-task --file path/to/task.md   one file
  greatminds migrate-task --queue feature_dev      all active in a queue
  greatminds migrate-task --all                    every .md task in
                                                   .greatminds/ (active queues
                                                   only by default; pass
                                                   --include-terminal to also
                                                   touch verified/, archive/,
                                                   etc.)

Flags:
  --dry-run         report what would be migrated; write nothing
  --keep-md         leave .md alongside new .yaml
  --force           overwrite existing .yaml if present

Best-effort: per-block fields are remapped where the canonical name
changed (written_by→by, written_at→at, closed_by→by, closed_at→at).
Unknown fields are preserved verbatim under their original key.
"""

from __future__ import annotations

import datetime
import re
import uuid
from pathlib import Path
from typing import Any

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir


def normalize_iso(value: Any) -> Any:
    """Coerce datetime / loose date strings to ISO-8601 with 'T'."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")
        return value.strftime("%Y-%m-%dT%H:%M:%S") + (
            "Z" if value.utcoffset() == datetime.timedelta(0) else value.strftime("%z")
        )
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d") + "T00:00:00Z"
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value):
        return value.replace(" ", "T", 1)
    return value


def walk_normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: walk_normalize(v) if not _is_ts_key(k) else normalize_iso(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk_normalize(x) for x in obj]
    return obj


def _is_ts_key(k: str) -> bool:
    if not isinstance(k, str):
        return False
    return k in ("at", "opened_at", "answered_at", "sent_at",
                 "closed_at", "written_at", "reviewed_at", "intent_at",
                 "gate_check_at")

# block-name → canonical kind
BLOCK_NAME_TO_KIND: dict[str, str] = {
    "triage_block": "triage",
    "triage": "triage",
    "plan_block": "plan",
    "plan": "plan",
    "implementation_block": "implementation",
    "impl_block": "implementation",
    "implementation": "implementation",
    "tests_block": "tests",
    "tests": "tests",
    "reader_block": "reader_review",
    "reader_review": "reader_review",
    "review_block": "review",
    "review": "review",
    "blocked_block": "blocked",
    "blocked": "blocked",
    "stand_result_block": "stand_result",
    "stand_result": "stand_result",
    "session_iteration": "session_iteration",
}

# legacy field name → canonical name
FIELD_REMAP: dict[str, str] = {
    "written_by": "by",
    "written_at": "at",
    "closed_by": "by",
    "closed_at": "at",
    "reviewed_by": "by",
    "reviewed_at": "at",
}


# Active product queues (default scope for --all).
ACTIVE_QUEUES = (
    "feature_inbox", "feature_plan",
    "feature_dev", "feature_ui_dev", "feature_docs",
    "feature_test", "feature_docs_review",
    "feature_review", "feature_blocked",
    "user_feedback", "review_sessions",
    "stand_requests", "stand_wip",
)

TERMINAL_QUEUES = ("verified", "archive", "stand_done")


def die(msg: str, code: int = 1) -> None:
    """Raise :class:`GreatMindsError` with this module's historical
    ``die(msg, code=1)`` signature.

    The other modules raise ``GreatMindsError`` directly. This thin wrapper
    is kept because every callsite below already uses ``die("...")`` /
    ``die("...", code=N)`` — the rewrite would touch ~50 lines without
    behavioural benefit. The raise still goes through ``GreatMindsError``,
    so click catches it identically.
    """
    raise GreatMindsError(msg, exit_code=code)


def split_md_chunks(text: str) -> list[str]:
    """Split on lines that are exactly '---', keep contents between."""
    return re.split(r"^---\s*$", text, flags=re.MULTILINE)


def chunk_yaml(chunk: str) -> Any:
    """Try parse chunk as YAML; return value or None."""
    chunk = chunk.strip()
    if not chunk:
        return None
    try:
        return yaml.safe_load(chunk)
    except yaml.YAMLError:
        return None


def remap_block_fields(block: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in block.items():
        new_k = FIELD_REMAP.get(k, k)
        if new_k in out and out[new_k] != v:
            # if both written_by and closed_by present, keep the later one
            out[new_k] = v
        else:
            out[new_k] = v
    return out


def migrate_one(path: Path, dry_run: bool, force: bool, keep_md: bool) -> tuple[str, str | None]:
    """Return (status, message). status: 'migrated' | 'skipped' | 'error'."""
    if path.suffix != ".md":
        return ("skipped", "not a .md file")
    yaml_path = path.with_suffix(".yaml")
    if yaml_path.exists() and not force:
        return ("skipped", f"{yaml_path.name} already exists (use --force)")

    text = path.read_text(encoding="utf-8")
    chunks = split_md_chunks(text)

    header: dict[str, Any] = {}
    blocks: list[dict[str, Any]] = []
    prose_parts: list[str] = []

    for chunk in chunks:
        data = chunk_yaml(chunk)
        if data is None:
            # plain prose
            s = chunk.strip()
            if s:
                prose_parts.append(s)
            continue
        if not isinstance(data, dict):
            # weird (list at top level) — skip but warn
            continue
        # is this header? heuristic: first dict with 'id' field
        if not header and "id" in data:
            header = dict(data)
            continue
        # is it a known block? heuristic: dict has exactly one key that
        # matches a known block name, OR has 'kind' field directly.
        if "kind" in data and data["kind"] in BLOCK_NAME_TO_KIND.values():
            blocks.append(remap_block_fields(dict(data)))
            continue
        if len(data) == 1:
            (k, v), = data.items()
            if k in BLOCK_NAME_TO_KIND and isinstance(v, dict):
                block = dict(v)
                block["kind"] = BLOCK_NAME_TO_KIND[k]
                blocks.append(remap_block_fields(block))
                continue
        # check if dict-as-whole-block-payload: keys include 'written_by'
        # or 'closed_by' etc — treat as bare block (kind unknown). Skip
        # rather than guess.
        if any(k in data for k in ("written_by", "closed_by", "reviewed_by")):
            # block without explicit kind tag — try inferring
            inferred = infer_block_kind(data)
            if inferred:
                block = dict(data)
                block["kind"] = inferred
                blocks.append(remap_block_fields(block))
                continue
        # otherwise, treat as additional header data (merge into header)
        if header:
            for k, v in data.items():
                header.setdefault(k, v)

    if not header:
        return ("error", "no header (first YAML block with id) found")
    if not header.get("id"):
        return ("error", "header missing 'id'")

    # build new doc
    new_doc: dict[str, Any] = {}
    # canonical order of header fields
    for k in ("id", "stream", "kind", "scope", "title", "reporter",
              "opened_at", "priority", "assignee_role",
              "request_type", "target", "evidence_for",
              "mode", "target_functionality", "scenarios", "stand_target",
              "related"):
        if k in header:
            new_doc[k] = header[k]
    # copy any other header fields verbatim (not already moved)
    for k, v in header.items():
        if k not in new_doc:
            new_doc[k] = v

    if prose_parts:
        new_doc["description"] = "\n\n".join(prose_parts)

    new_doc["blocks"] = ensure_block_meta(blocks)

    # normalize legacy date/datetime values to ISO strings
    new_doc = walk_normalize(new_doc)

    if dry_run:
        return ("migrated", f"would write {yaml_path.name} "
                            f"({len(blocks)} block(s), prose: {bool(prose_parts)})")

    write_yaml_atomic(yaml_path, new_doc)

    if keep_md:
        return ("migrated", f"wrote {yaml_path.name} (kept {path.name})")
    legacy = path.with_suffix(".md.legacy")
    try:
        os.rename(path, legacy)
    except OSError as exc:
        return ("error", f"rename {path.name} → .legacy failed: {exc}")
    return ("migrated", f"wrote {yaml_path.name}, renamed {path.name} → {legacy.name}")


def infer_block_kind(data: dict[str, Any]) -> str | None:
    """Heuristic: block payload without explicit kind tag."""
    has = lambda *keys: any(k in data for k in keys)
    if has("plan_kind", "assignee_role") and has("ready_for_implementation"):
        return "plan"
    if has("ready_for_test", "files", "files_touched"):
        return "implementation"
    if has("test_result", "test_command", "gate_check_result"):
        return "tests"
    if has("docs_checked", "stand_checked", "command_or_flow"):
        return "reader_review"
    if has("review_block", "approved") or data.get("outcome") in ("approved", "changes_requested"):
        return "review"
    if has("dependencies", "resume_to"):
        return "blocked"
    if has("stand_status", "result") and has("profile"):
        return "stand_result"
    return None


def ensure_block_meta(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make sure every block has 'by' and 'at'; leave missing fields as None
    but keep the block (legacy files are messy)."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        nb = dict(b)
        # 'kind' must be at front for readability
        ordered: dict[str, Any] = {}
        for k in ("kind", "by", "at"):
            if k in nb:
                ordered[k] = nb.pop(k)
        ordered.update(nb)
        out.append(ordered)
    return out


def write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True,
                           default_flow_style=False)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def collect_targets(file: str | None, queue: str | None, all_flag: bool,
                    include_terminal: bool) -> list[Path]:
    if file:
        return [Path(file).resolve()]
    coord = find_coord_dir()
    queues: list[str] = []
    if queue:
        queues = [queue]
    elif all_flag:
        queues = list(ACTIVE_QUEUES)
        if include_terminal:
            queues += list(TERMINAL_QUEUES)
    else:
        die("specify --file FILE, --queue QUEUE, or --all")
    out: list[Path] = []
    for q in queues:
        qd = coord / q
        if not qd.is_dir():
            continue
        for f in qd.glob("*.md"):
            if f.name == "_TEMPLATE.md":
                continue
            out.append(f)
    return out


@click.command(name="migrate-task",
               short_help="convert .md task files to strict .yaml",
               help=__doc__)
@click.option("--file", "file", default=None, help="single .md file to migrate")
@click.option("--queue", default=None, help="migrate all .md in this queue")
@click.option("--all", "all_flag", is_flag=True,
              help="migrate every .md in active queues")
@click.option("--include-terminal", is_flag=True,
              help="with --all, also include verified/archive/stand_done/...")
@click.option("--dry-run", is_flag=True)
@click.option("--keep-md", is_flag=True,
              help="leave the original .md file in place")
@click.option("--force", is_flag=True, help="overwrite existing .yaml")
def migrate_task(file: str | None, queue: str | None, all_flag: bool,
                 include_terminal: bool, dry_run: bool, keep_md: bool,
                 force: bool) -> None:
    from greatminds.cli._colors import info, ok, warn, err
    targets = collect_targets(file, queue, all_flag, include_terminal)
    if not targets:
        info("migrate-task: no .md tasks found")
        return

    stats = {"migrated": 0, "skipped": 0, "error": 0}
    for path in targets:
        status, msg = migrate_one(path, dry_run, force, keep_md)
        stats[status] = stats.get(status, 0) + 1
        if status != "skipped" or dry_run:
            tag = {"migrated": "OK", "skipped": "--", "error": "ERR"}[status]
            line = f"  [{tag}] {path.name}: {msg}"
            (ok if status == "migrated" else warn if status == "error" else info)(line)
    info(f"\nmigrated: {stats['migrated']}  "
         f"skipped: {stats['skipped']}  errors: {stats['error']}")
    if stats["error"] != 0:
        raise click.exceptions.Exit(2)


if __name__ == "__main__":
    migrate_task()
