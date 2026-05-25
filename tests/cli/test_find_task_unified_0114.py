"""Regression tests for task 0114: unified CLI task lookup.

Pre-0114 each subcommand had its own lookup helper with subtly
different semantics:
  - task.find_task: exact stem only (no short-id support → the 0097
    race incident).
  - plan.find_task_queue: exact stem only.
  - amend._find_task: exact stem only (yaml only).
  - gate_check.find_task_file: short-id supported but queues filtered.

0114 unifies on task.find_task with three id shapes (exact, short
numeric, slug-prefix) and migrates the others to thin wrappers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli.task import find_task


def _make_task(coord: Path, queue: str, stem: str) -> Path:
    qdir = coord / queue
    qdir.mkdir(parents=True, exist_ok=True)
    p = qdir / f"{stem}.yaml"
    p.write_text(f"id: {stem}\n", encoding="utf-8")
    return p


# ---------- exact-stem (legacy behavior preserved) ----------


def test_find_task_exact_stem(tmp_path: Path) -> None:
    _make_task(tmp_path, "feature_dev", "0001-foo")
    found = find_task(tmp_path, "0001-foo")
    assert found is not None
    path, q = found
    assert q == "feature_dev"
    assert path.name == "0001-foo.yaml"


# ---------- short-numeric id (the 0097 race incident fix) ----------


def test_find_task_short_numeric_matches_full_slug(tmp_path: Path) -> None:
    """The 0097 incident: 'task mv 0097 X' failed with 'not found in
    any queue' even though 0097-portable-fs-watch-... existed. After
    0114, short-id matches any file starting with the zero-padded seq.
    """
    _make_task(tmp_path, "feature_test", "0097-portable-fs-watch-wake")
    found = find_task(tmp_path, "0097")
    assert found is not None
    path, q = found
    assert q == "feature_test"
    assert path.stem == "0097-portable-fs-watch-wake"


def test_find_task_short_numeric_zero_pads(tmp_path: Path) -> None:
    """Short id '5' must match '0005-…' (zero-padded to 4 digits)."""
    _make_task(tmp_path, "feature_dev", "0005-tiny-task")
    found = find_task(tmp_path, "5")
    assert found is not None
    assert found[0].stem == "0005-tiny-task"


def test_find_task_short_numeric_no_match_returns_none(tmp_path: Path) -> None:
    _make_task(tmp_path, "feature_dev", "0042-foo")
    assert find_task(tmp_path, "0099") is None


# ---------- slug-prefix ----------


def test_find_task_slug_prefix(tmp_path: Path) -> None:
    """Non-numeric prefix: '0091-move' matches '0091-move-rules-…'.
    Useful when the user types a partial slug from memory.
    """
    _make_task(tmp_path, "feature_dev", "0091-move-rules-from-coordinate")
    found = find_task(tmp_path, "0091-move")
    assert found is not None
    assert found[0].stem == "0091-move-rules-from-coordinate"


def test_find_task_slug_prefix_no_partial_word(tmp_path: Path) -> None:
    """Prefix matching requires a '-' boundary, so '0091-mo' does NOT
    match (avoids accidentally matching half-typed slugs)."""
    _make_task(tmp_path, "feature_dev", "0091-move-rules")
    # 0091-mo would match if we allowed any-char prefix; we require
    # '<prefix>-' boundary.
    assert find_task(tmp_path, "0091-mo") is None


# ---------- exact wins over prefix ----------


def test_find_task_exact_preferred_over_prefix(tmp_path: Path) -> None:
    """If both an exact-match and a prefix-match exist, the exact one
    wins. Defensive — unlikely in practice but pin the semantics.
    """
    _make_task(tmp_path, "feature_dev", "0042-task")
    _make_task(tmp_path, "feature_test", "0042-task-longer-slug")
    # Exact id '0042-task' must return the exact file, not the longer one.
    found = find_task(tmp_path, "0042-task")
    assert found is not None
    assert found[0].stem == "0042-task"


# ---------- queue scope: all subdirs scanned (not just one) ----------


def test_find_task_scans_all_known_queues(tmp_path: Path) -> None:
    """The 0097 incident was partly because role-scoped lookup
    couldn't see a task that REVIEWER had just moved to feature_test
    (out of DEVELOPER's claim queues). find_task must scan ALL queues
    regardless of caller role."""
    _make_task(tmp_path, "verified", "0010-already-done")
    _make_task(tmp_path, "archive", "0011-archived")
    _make_task(tmp_path, "stand_done", "0012-stand")
    assert find_task(tmp_path, "0010") is not None
    assert find_task(tmp_path, "0011") is not None
    assert find_task(tmp_path, "0012") is not None


def test_find_task_skips_intent_inbox_locks(tmp_path: Path) -> None:
    """Coordination scratch dirs (intent/, inbox/, .locks/) must not
    masquerade as queues."""
    (tmp_path / "intent").mkdir()
    (tmp_path / "intent" / "0001-foo.yaml").write_text("x", encoding="utf-8")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "0001-foo.yaml").write_text("x", encoding="utf-8")
    (tmp_path / ".locks").mkdir()
    (tmp_path / ".locks" / "0001-foo.lock").write_text("x", encoding="utf-8")
    # No real queue contains 0001-foo → must return None.
    assert find_task(tmp_path, "0001-foo") is None


# ---------- wrapper consistency ----------


def test_plan_find_task_queue_uses_unified_find_task(tmp_path: Path) -> None:
    from greatminds.cli.plan import find_task_queue
    _make_task(tmp_path, "feature_test", "0091-foo")
    assert find_task_queue(tmp_path, "0091") == "feature_test"
    assert find_task_queue(tmp_path, "0091-foo") == "feature_test"
    assert find_task_queue(tmp_path, "9999") is None


# Note: amend.py is owned by task 0089 (mid-task acceptance broadcast)
# and is intentionally NOT migrated here — REVIEWER 0114 iter explicitly
# flagged the cross-task contamination. Once 0089 lands, a follow-up
# task should migrate amend._find_task to the unified helper.


def test_gate_check_find_task_file_uses_unified_find_task(
    tmp_path: Path,
) -> None:
    """REVIEWER 0114 iter blocker: gate-check must share the same
    lookup helper as everyone else, not its own divergent copy.
    Short-id resolution must work; product-queue filtering must
    still apply (no stand_*/bot_*).
    """
    from greatminds.cli.gate_check import find_task_file
    project = tmp_path
    coord = project / "coordination"
    coord.mkdir()
    # Task with long slug in a product-pipeline queue: short-id resolves.
    (coord / "feature_test").mkdir()
    (coord / "feature_test" / "0109-make-schema-stuff.yaml").write_text(
        "id: 0109-make-schema-stuff\n", encoding="utf-8",
    )
    path = find_task_file(
        project, "0109", ["feature_test", "feature_review", "verified"],
    )
    assert path is not None
    assert path.stem == "0109-make-schema-stuff"


def test_gate_check_find_task_file_filters_to_allowed_queues(
    tmp_path: Path,
) -> None:
    """A task in stand_done must NOT be returned by gate_check when
    the queues list excludes stand_done (product-pipeline scope)."""
    from greatminds.cli.gate_check import find_task_file
    project = tmp_path
    coord = project / "coordination"
    coord.mkdir()
    (coord / "stand_done").mkdir()
    (coord / "stand_done" / "0050-stand-evidence.yaml").write_text(
        "id: 0050-stand-evidence\n", encoding="utf-8",
    )
    # gate_check passes only product-pipeline queues — stand_done
    # is excluded.
    path = find_task_file(
        project, "0050", ["feature_test", "feature_review", "verified"],
    )
    assert path is None


# ---------- coord missing / nonexistent ----------


def test_find_task_returns_none_when_coord_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert find_task(missing, "0001-foo") is None
