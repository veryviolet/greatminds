#!/usr/bin/env python3
"""Check stand-evidence gate for a product task.

Usage:
    gate_check <task-id> [--project-dir <dir>] [--canon-dir <dir>]

<task-id> may be the full id (e.g. 0123-foo) or just the seq (e.g. 0123).

Looks up the task across coordination queues, reads its plan block, and
verifies stand-evidence per the schema:
  - plan.stand_required: false  → output 'n/a'
  - plan.stand_required: true   → find stand_done/*.md whose
    stand_result.evidence_for contains this task id. If commit hash in
    plan/implementation matches the stand_result.commit, output 'pass'.
    If a candidate exists but commit differs or result != pass → 'fail'.
    No candidate → 'missing'.

Exit code:
  0  pass | n/a
  1  fail
  2  missing
  3  error (task not found, malformed)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

# Active queues + verified/ where a task could live. Read from schema.yaml at
# runtime so we don't drift from the canon list.
DEFAULT_QUEUES = [
    "feature_inbox", "feature_plan", "feature_blocked",
    "feature_dev", "feature_ui_dev", "feature_docs",
    "feature_test", "feature_docs_review", "feature_review",
    "verified", "archive",
]


def split_yaml_blocks(text: str, verbose_errors: bool = False, source: str = "") -> list[dict]:
    """Return a list of parsed YAML blocks from a task file.

    Task files are markdown with multiple `---`-fenced YAML blocks. The first
    block is initial front-matter; later ones are append-only (plan,
    implementation, tests, reader, review, blocked, stand_result, etc.).

    If verbose_errors=True, prints YAML parse errors to stderr so a broken
    block (e.g. unquoted `:` in a list item) is visible instead of silently
    making the task look "missing".
    """
    blocks: list[dict] = []
    parts = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    for i in range(1, len(parts), 2):
        chunk = parts[i].strip()
        if not chunk:
            continue
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError as exc:
            if verbose_errors:
                first_line = chunk.splitlines()[0] if chunk else ""
                print(
                    f"  YAML parse failed in {source} block #{i // 2 + 1} "
                    f"(starts with `{first_line[:60]}`):\n    {exc}",
                    file=sys.stderr,
                )
            continue
        if isinstance(data, dict):
            blocks.append(data)
    return blocks


def merge_blocks(blocks: list[dict]) -> dict:
    """Merge front-matter and named blocks into one dict.

    Later blocks override earlier on top-level keys (representing append-only
    iterations of the same block name). The initial front-matter is the first
    block; if it has bare `id`/`stream` keys we keep them at top level.
    """
    merged: dict = {}
    for b in blocks:
        for k, v in b.items():
            merged[k] = v
    return merged


def find_task_file(project_dir: Path, task_id: str, queues: list[str]) -> Path | None:
    coord = project_dir / "coordination"
    # Normalize: if just seq given, accept any file starting with "<seq>-".
    seq_only = re.fullmatch(r"\d{1,4}", task_id)
    for q in queues:
        d = coord / q
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "_TEMPLATE.md":
                continue
            if f.stem == task_id:
                return f
            if seq_only:
                if f.stem.startswith(f"{int(task_id):04d}-"):
                    return f
    return None


def load_schema_queues(canon_dir: Path) -> list[str]:
    schema_path = canon_dir / "schema.yaml"
    if not schema_path.exists():
        return DEFAULT_QUEUES
    try:
        data = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return DEFAULT_QUEUES
    queues = data.get("queues") or {}
    # Include only product-pipeline queues, not bot/stand.
    relevant = [name for name in queues if not name.startswith("bot_") and not name.startswith("stand_")]
    return relevant or DEFAULT_QUEUES


def find_stand_evidence(project_dir: Path, task_id: str) -> list[tuple[Path, dict]]:
    """Return list of (path, parsed stand_result block) where evidence_for matches."""
    found: list[tuple[Path, dict]] = []
    stand_done = project_dir / "coordination" / "stand_done"
    if not stand_done.is_dir():
        return found
    seq = task_id.split("-")[0] if "-" in task_id else task_id
    for f in sorted(stand_done.glob("*.md")):
        if f.name == "_TEMPLATE.md":
            continue
        blocks = split_yaml_blocks(f.read_text(encoding="utf-8"))
        merged = merge_blocks(blocks)
        stand_result = merged.get("stand_result")
        if not isinstance(stand_result, dict):
            continue
        evidence_for = stand_result.get("evidence_for") or []
        if not isinstance(evidence_for, list):
            continue
        # Match by full id or by seq-prefix.
        match = any(
            isinstance(e, str) and (e == task_id or e.startswith(f"{seq}-") or e == seq)
            for e in evidence_for
        )
        if not match:
            # Legacy: also accept related_product_task scalar
            rpt = stand_result.get("related_product_task")
            if isinstance(rpt, str) and (rpt == task_id or rpt.startswith(f"{seq}-") or rpt == seq):
                match = True
        if match:
            found.append((f, stand_result))
    return found


def get_task_commit(merged: dict) -> str | None:
    """Latest commit known for the task — prefer implementation, then plan."""
    impl = merged.get("implementation")
    if isinstance(impl, dict):
        base = impl.get("base_commit")
        if base:
            return str(base)
    plan = merged.get("plan")
    if isinstance(plan, dict):
        base = plan.get("base_commit")
        if base:
            return str(base)
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-gate-check`` in pyproject.toml."""
    from greatminds.core.paths import find_canon_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--canon-dir",
        type=Path,
        default=None,
        help="canon data directory (default: packaged greatminds.data)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    canon_dir = args.canon_dir if args.canon_dir is not None else find_canon_dir()

    queues = load_schema_queues(canon_dir)
    task_path = find_task_file(args.project_dir, args.task_id, queues)
    if task_path is None:
        print(f"error: task '{args.task_id}' not found in any queue", file=sys.stderr)
        return 3

    blocks = split_yaml_blocks(
        task_path.read_text(encoding="utf-8"),
        verbose_errors=args.verbose,
        source=str(task_path),
    )
    if not blocks:
        print(f"error: no YAML blocks in {task_path}", file=sys.stderr)
        return 3
    merged = merge_blocks(blocks)
    plan = merged.get("plan")
    if not isinstance(plan, dict):
        print("missing")
        if args.verbose:
            print(f"  reason: no plan block in {task_path}", file=sys.stderr)
        return 2

    stand_required = plan.get("stand_required")
    task_id_full = merged.get("id") or task_path.stem

    if stand_required is False or stand_required is None:
        print("n/a")
        if args.verbose:
            print(f"  reason: plan.stand_required is {stand_required!r}", file=sys.stderr)
        return 0

    if stand_required is not True:
        print("missing")
        if args.verbose:
            print(f"  reason: plan.stand_required is {stand_required!r}, expected true/false", file=sys.stderr)
        return 2

    candidates = find_stand_evidence(args.project_dir, str(task_id_full))
    if not candidates:
        print("missing")
        if args.verbose:
            print(f"  reason: no stand_done/*.md with evidence_for matching {task_id_full}", file=sys.stderr)
        return 2

    task_commit = get_task_commit(merged)
    pass_any = False
    fail_reasons: list[str] = []
    for path, sr in candidates:
        result = sr.get("result")
        sr_commit = sr.get("commit")
        if result != "pass":
            fail_reasons.append(f"{path.name}: result={result!r}")
            continue
        if task_commit and sr_commit and not str(sr_commit).startswith(str(task_commit)) \
                and not str(task_commit).startswith(str(sr_commit)):
            fail_reasons.append(f"{path.name}: commit mismatch (stand={sr_commit!r}, task={task_commit!r})")
            continue
        pass_any = True
        if args.verbose:
            print(f"  matched: {path.name}", file=sys.stderr)
        break

    if pass_any:
        print("pass")
        return 0
    print("fail")
    if args.verbose:
        for r in fail_reasons:
            print(f"  {r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
