#!/usr/bin/env python3
"""Check stand-evidence gate for a product task.

Usage:
    gate_check <task-id> [--project-dir <dir>] [--canon-dir <dir>]

<task-id> may be the full id (e.g. 0123-foo) or just the seq (e.g. 0123).

Looks up the task across coordination queues, reads its plan block, and
verifies stand-evidence per the schema:
  - plan.stand_required: false  → output 'n/a'
  - plan.stand_required: true   → find stand_done/*.{yaml,md} whose
    stand_result.evidence_for (or the file's top-level evidence_for in
    the yaml-native shape) contains this task id. If commit hash in
    plan/implementation matches the stand_result.commit, output 'pass'.
    If a candidate exists but commit differs or
    result not in {pass, ok} → 'fail'. No candidate → 'missing'.

Both the legacy fenced markdown shape and the yaml-native shape are
supported; the reader picks the right parser per file.

Exit code:
  0  pass | n/a
  1  fail
  2  missing
  3  error (task not found, malformed)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import click

from greatminds.core.paths import find_canon_dir

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


def _candidate_files(d: Path) -> list[Path]:
    """All non-template task/evidence candidates under a directory.

    Glob both `.md` (legacy fenced) and `.yaml` (yaml-native) shapes.
    Excludes `_TEMPLATE.*` by stem (covers `_TEMPLATE.md` and
    `_TEMPLATE.yaml` in one check).
    """
    if not d.is_dir():
        return []
    out: list[Path] = []
    for ext in ("md", "yaml"):
        for f in d.glob(f"*.{ext}"):
            if f.stem == "_TEMPLATE":
                continue
            out.append(f)
    return sorted(out)


def parse_task_file(path: Path, verbose_errors: bool = False) -> dict:
    """Parse a task/evidence file (yaml-native OR fenced .md) → merged dict.

    Yaml-native (single YAML document, no `---` fences in body):
        Whole file is a mapping with a top-level `blocks: [{kind: K, …}]`.
    Fenced legacy (markdown with multiple `---`-fenced YAML chunks):
        Parsed via `split_yaml_blocks` + `merge_blocks`.

    After either path, the returned dict mirrors each blocks-list entry
    at ``merged[entry["kind"]]`` so callers can read ``merged["plan"]``,
    ``merged["stand_result"]`` etc. uniformly regardless of shape.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    has_fences = (
        re.search(r"^---\s*$", text, flags=re.MULTILINE) is not None
    )
    if has_fences:
        blocks = split_yaml_blocks(
            text, verbose_errors=verbose_errors, source=str(path),
        )
        merged = merge_blocks(blocks)
    else:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            if verbose_errors:
                print(f"  YAML parse failed in {path}: {exc}",
                      file=sys.stderr)
            doc = None
        merged = doc if isinstance(doc, dict) else {}

    # Yaml-native shape: expand `blocks: [{kind: K, …}]` into
    # `merged[K] = entry`. Keep this AFTER any legacy merge so an
    # old fenced file (where top-level `plan` is already set) is
    # unchanged when blocks list is absent.
    blocks_list = merged.get("blocks")
    if isinstance(blocks_list, list):
        for entry in blocks_list:
            if isinstance(entry, dict) and "kind" in entry:
                merged[entry["kind"]] = entry
    return merged


def find_task_file(project_dir: Path, task_id: str, queues: list[str]) -> Path | None:
    coord = project_dir / "coordination"
    # Normalize: if just seq given, accept any file starting with "<seq>-".
    seq_only = re.fullmatch(r"\d{1,4}", task_id)
    for q in queues:
        for f in _candidate_files(coord / q):
            if f.stem == task_id:
                return f
            if seq_only and f.stem.startswith(f"{int(task_id):04d}-"):
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
    """Return list of (path, parsed stand_result block) where evidence_for matches.

    Reads both yaml-native (top-level ``evidence_for``) and legacy fenced
    (``stand_result.evidence_for``) shapes via ``parse_task_file``.
    """
    found: list[tuple[Path, dict]] = []
    stand_done = project_dir / "coordination" / "stand_done"
    seq = task_id.split("-")[0] if "-" in task_id else task_id
    for f in _candidate_files(stand_done):
        merged = parse_task_file(f)
        stand_result = merged.get("stand_result")
        if not isinstance(stand_result, dict):
            continue
        # Yaml-native lifts evidence_for to the top of the evidence file;
        # legacy fenced kept it inside the stand_result block. Accept both.
        evidence_for = (
            stand_result.get("evidence_for")
            or merged.get("evidence_for")
            or []
        )
        if not isinstance(evidence_for, list):
            continue
        # Match by full id or by seq-prefix.
        match = any(
            isinstance(e, str)
            and (e == task_id or e.startswith(f"{seq}-") or e == seq)
            for e in evidence_for
        )
        if not match:
            # Legacy: also accept related_product_task scalar
            rpt = stand_result.get("related_product_task")
            if isinstance(rpt, str) and (
                rpt == task_id or rpt.startswith(f"{seq}-") or rpt == seq
            ):
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


@click.command(name="gate-check",
               short_help="check stand-evidence gate for a product task",
               help=__doc__)
@click.argument("task_id")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
@click.option("-v", "--verbose", is_flag=True, help="print diagnostic reasons")
def gate_check(task_id: str, project_dir: Path | None, canon_dir: Path | None,
               verbose: bool) -> None:
    from greatminds.cli._colors import err, info, ok, warn

    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    queues = load_schema_queues(canon_dir)
    task_path = find_task_file(project_dir, task_id, queues)
    if task_path is None:
        err(f"error: task '{task_id}' not found in any queue")
        raise click.exceptions.Exit(3)

    merged = parse_task_file(task_path, verbose_errors=verbose)
    if not merged:
        err(f"error: no parseable content in {task_path}")
        raise click.exceptions.Exit(3)
    plan = merged.get("plan")
    if not isinstance(plan, dict):
        info("missing")
        if verbose:
            warn(f"  reason: no plan block in {task_path}")
        raise click.exceptions.Exit(2)

    stand_required = plan.get("stand_required")
    task_id_full = merged.get("id") or task_path.stem

    if stand_required is False or stand_required is None:
        info("n/a")
        if verbose:
            warn(f"  reason: plan.stand_required is {stand_required!r}")
        return

    if stand_required is not True:
        info("missing")
        if verbose:
            warn(f"  reason: plan.stand_required is {stand_required!r}, expected true/false")
        raise click.exceptions.Exit(2)

    candidates = find_stand_evidence(project_dir, str(task_id_full))
    if not candidates:
        info("missing")
        if verbose:
            warn(f"  reason: no stand_done/*.{{yaml,md}} with evidence_for matching {task_id_full}")
        raise click.exceptions.Exit(2)

    task_commit = get_task_commit(merged)
    pass_any = False
    fail_reasons: list[str] = []
    for path, sr in candidates:
        result = sr.get("result")
        sr_commit = sr.get("commit")
        if result not in ("pass", "ok"):
            fail_reasons.append(f"{path.name}: result={result!r}")
            continue
        if task_commit and sr_commit and not str(sr_commit).startswith(str(task_commit)) \
                and not str(task_commit).startswith(str(sr_commit)):
            fail_reasons.append(f"{path.name}: commit mismatch (stand={sr_commit!r}, task={task_commit!r})")
            continue
        pass_any = True
        if verbose:
            info(f"  matched: {path.name}")
        break

    if pass_any:
        ok("pass")
        return
    warn("fail")
    if verbose:
        for r in fail_reasons:
            warn(f"  {r}")
    raise click.exceptions.Exit(1)


if __name__ == "__main__":
    gate_check()


if __name__ == "__main__":
    sys.exit(main())
