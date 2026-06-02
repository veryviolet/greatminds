"""Tests for task 0345 (part 2): orphaned intent files are reaped.

The ``intent-clean`` reaper existed but nothing ran it, so orphaned
intents (e.g. 0331-architect-reviewer-*.json) lingered for hours on a
long-lived stand and surfaced as watchdog findings. 0345 extracts the
reaping core into ``reap_orphan_intents`` (shared by the CLI and coordd's
periodic reaper) and wires coordd to run it on a cadence.

Reaping is safe-by-default: only intents older than ``min_age_sec`` whose
task has already LEFT its ``from`` queue are removed; recent intents and
still-in-flight tasks are kept.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from greatminds.cli.intent_clean import reap_orphan_intents


def _coord(tmp_path) -> Path:
    coord = tmp_path / "coordination"
    (coord / "intent").mkdir(parents=True)
    (coord / "feature_dev").mkdir()
    return coord


def _intent(coord, name, task, from_q, age_seconds):
    p = coord / "intent" / name
    p.write_text(json.dumps({"task": task, "from": from_q,
                             "to": "feature_test"}), encoding="utf-8")
    past = time.time() - age_seconds
    os.utime(p, (past, past))
    return p


def test_reaps_orphan_whose_task_left_from_queue(tmp_path):
    coord = _coord(tmp_path)
    # task 0331 is NOT in feature_dev (moved/withdrawn) → orphan
    orphan = _intent(coord, "0331-architect-reviewer-abc.json", "0331",
                     "feature_dev", age_seconds=8 * 3600)
    counts = reap_orphan_intents(coord, min_age_sec=300)
    assert counts["removed"] == 1
    assert not orphan.exists()


def test_keeps_recent_intent(tmp_path):
    coord = _coord(tmp_path)
    p = _intent(coord, "0400-developer-x.json", "0400", "feature_dev",
                age_seconds=30)  # younger than min-age
    counts = reap_orphan_intents(coord, min_age_sec=300)
    assert counts["removed"] == 0
    assert counts["kept_recent"] == 1
    assert p.exists()


def test_keeps_in_flight_intent(tmp_path):
    coord = _coord(tmp_path)
    # task still sits in its from-queue → long op in progress, keep
    (coord / "feature_dev" / "0401.yaml").write_text("id: 0401\n",
                                                     encoding="utf-8")
    p = _intent(coord, "0401-developer-y.json", "0401", "feature_dev",
                age_seconds=8 * 3600)
    counts = reap_orphan_intents(coord, min_age_sec=300)
    assert counts["removed"] == 0
    assert counts["kept_active"] == 1
    assert p.exists()


def test_dry_run_does_not_delete(tmp_path):
    coord = _coord(tmp_path)
    p = _intent(coord, "0331-architect-reviewer-z.json", "0331",
                "feature_dev", age_seconds=8 * 3600)
    counts = reap_orphan_intents(coord, min_age_sec=300, dry_run=True)
    assert counts["removed"] == 1  # counted as would-remove
    assert p.exists()  # but not actually deleted


def test_no_intent_dir_is_noop(tmp_path):
    coord = tmp_path / "coordination"
    coord.mkdir()
    counts = reap_orphan_intents(coord, min_age_sec=300)
    assert counts == {"removed": 0, "kept_active": 0, "kept_recent": 0}


def test_cli_intent_clean_uses_shared_core(tmp_path):
    """The intent-clean CLI must still work via the extracted core."""
    from click.testing import CliRunner
    from greatminds.cli.intent_clean import intent_clean
    coord = _coord(tmp_path)
    _intent(coord, "0331-architect-reviewer-q.json", "0331", "feature_dev",
            age_seconds=8 * 3600)
    res = CliRunner().invoke(intent_clean, [
        "--project-dir", str(tmp_path)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "removed=1" in res.output
