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
    """Delegate to the unified task.find_task (0114) then filter by the
    product-pipeline queue allowlist gate-check intentionally restricts
    to. Keeps gate-check's narrower scope (no stand_*/bot_*) while
    eliminating the resolution divergence REVIEWER flagged."""
    from greatminds.cli.task import find_task as _unified_find_task

    coord = project_dir / "coordination"
    allowed = set(queues)
    found = _unified_find_task(coord, task_id)
    if found is None:
        return None
    path, q = found
    if q not in allowed:
        return None
    return path


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


def extract_lease_evidence_from_tests(merged: dict) -> dict | None:
    """0246 (0242d / Phase 5): read lease-release evidence from the
    product task's latest tests block.

    Post-0245 the FSM transports stand-result evidence directly on
    the tests block via ``stand_evidence`` carrying the lease's
    structured fields: ``lease_id``, ``result``, ``commit``,
    ``ready_at``, ``released_at`` (and the existing free-form
    fields like ``observed_with_fix``, ``tester_observations``).

    This helper returns a dict in the same SHAPE that the legacy
    ``find_stand_evidence`` scan produced — so the gate_check pass/
    fail loop reuses the same comparison logic across both code
    paths. Returns None when:
    - no tests block, or
    - the tests block lacks ``stand_evidence``, or
    - ``stand_evidence`` lacks ``lease_id`` (pre-0246 evidence
      shape; caller falls back to the legacy stand_done scan).
    """
    tests = merged.get("tests")
    if not isinstance(tests, dict):
        return None
    ev = tests.get("stand_evidence")
    if not isinstance(ev, dict):
        return None
    if not ev.get("lease_id"):
        return None
    # Map test_result → stand_result-shaped result key for the
    # unified comparison loop.
    test_result = tests.get("test_result") or ev.get("result")
    return {
        "lease_id": ev.get("lease_id"),
        "result": test_result,
        "commit": ev.get("commit") or tests.get("gate_check_commit"),
        "worktree_fingerprint": ev.get("worktree_fingerprint"),
        "observed_with_fix": ev.get("observed_with_fix"),
        "tester_observations": ev.get("tester_observations"),
        "ready_at": ev.get("ready_at"),
        "released_at": ev.get("released_at"),
    }


def get_task_worktree_fingerprint(merged: dict) -> str | None:
    """0229: latest worktree_fingerprint known for the task.

    The fingerprint captures the uncommitted overlay at impl-mv-to-
    feature_test time (when DEV refiles the impl block). Compared
    against stand_result.worktree_fingerprint to decouple "what was
    tested" from "what is committed at base_commit".

    Returns None when the impl block has no fingerprint — backwards-
    compat for tasks shipped before 0229.
    """
    impl = merged.get("implementation")
    if isinstance(impl, dict):
        fp = impl.get("worktree_fingerprint")
        if isinstance(fp, str) and fp.strip():
            return fp.strip()
    return None


def compute_worktree_fingerprint(project_dir: Path) -> str | None:
    """0229/0383: sha256 of ``project_dir``'s uncommitted overlay.

    Captures the tracked uncommitted diff (``git diff HEAD``) PLUS the
    content of untracked new files (0383 — otherwise a new-files-only
    overlay is invisible and collides). ``project_dir`` must be the
    PER-TASK worktree, not the overlay-free main tree. Returns None when
    the project isn't a git repo, when git isn't on PATH, or when the
    overlay is empty (no changes — caller omits the field).

    Empty-diff case returns "clean" string (not None) so the caller
    can distinguish "no overlay computed yet" (None) from "computed
    and there was nothing pending" ("clean"). gate_check uses both
    branches.
    """
    import hashlib
    import subprocess

    def _git(args: list[str]) -> subprocess.CompletedProcess | None:
        try:
            cp = subprocess.run(
                ["git", *args], cwd=str(project_dir),
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return cp if cp.returncode == 0 else None

    diff_cp = _git(["diff", "HEAD"])
    if diff_cp is None:
        return None
    parts = [diff_cp.stdout or ""]
    # 0383: untracked NEW files are invisible to ``git diff HEAD``, so a
    # task whose overlay is purely new files (or whose tracked diff happens
    # to match another's) must still fingerprint uniquely. Fold each
    # untracked file's path + content into the hash. Without this the
    # fingerprint collapses to the bare tracked diff and unrelated
    # worktrees collide (the c474b1e3 collision when the diff was computed
    # over a clean tree).
    unt_cp = _git(["ls-files", "--others", "--exclude-standard"])
    if unt_cp is not None:
        for rel in sorted((unt_cp.stdout or "").splitlines()):
            rel = rel.strip()
            if not rel:
                continue
            parts.append(f"\n+++ untracked {rel}\n")
            try:
                parts.append(
                    (project_dir / rel).read_text(
                        encoding="utf-8", errors="replace")
                )
            except OSError:
                pass
    blob = "".join(parts)
    if not blob.strip():
        return "clean"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


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

    # 0246 + 0247 (1.3.0): gate_check reads lease evidence from the
    # product task's tests block. The pre-0246 stand_done scan
    # fallback was removed in 0247 — the queues are gone, no files
    # to scan. Tasks shipped before 0246 that lack
    # tests.stand_evidence.lease_id fail with "missing" and require
    # a refile path through the lease API.
    lease_evidence = extract_lease_evidence_from_tests(merged)
    if lease_evidence is None:
        info("missing")
        if verbose:
            warn(
                f"  reason: no tests.stand_evidence with lease_id "
                f"on task {task_id_full} (post-0247 lease model)"
            )
        raise click.exceptions.Exit(2)
    candidates = [(
        type("SyntheticPath", (), {"name": "tests.stand_evidence"})(),
        lease_evidence,
    )]

    task_commit = get_task_commit(merged)
    task_fingerprint = get_task_worktree_fingerprint(merged)
    pass_any = False
    fail_reasons: list[str] = []
    for path, sr in candidates:
        result = sr.get("result")
        sr_commit = sr.get("commit")
        sr_fingerprint = sr.get("worktree_fingerprint")
        if result not in ("pass", "ok"):
            fail_reasons.append(f"{path.name}: result={result!r}")
            continue
        # Commit-match check (existing behavior; commit drift always
        # wins as the primary signal).
        if task_commit and sr_commit and not str(sr_commit).startswith(str(task_commit)) \
                and not str(task_commit).startswith(str(sr_commit)):
            fail_reasons.append(f"{path.name}: commit mismatch (stand={sr_commit!r}, task={task_commit!r})")
            continue
        # 0229 fingerprint check: when BOTH sides carry a
        # worktree_fingerprint, they must match. If either side
        # lacks the field (pre-0229 task / pre-0229 stand), fall
        # back to commit-only (backwards-compat).
        if task_fingerprint and isinstance(sr_fingerprint, str) and sr_fingerprint:
            if task_fingerprint != sr_fingerprint:
                fail_reasons.append(
                    f"{path.name}: worktree_fingerprint mismatch "
                    f"(stand={sr_fingerprint!r}, "
                    f"task={task_fingerprint!r}) — iter-N overlay drift"
                )
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
