"""Tests for task 0338 (DOD2): ``greatminds journal tail`` read-only
CLI with -n / --role / --task filters.

The append-only coordination journal was only reachable by a raw
``tail`` of journal.ndjson, which the CLI-only rule (0337) forbids.
``journal tail`` gives a clean, read-only, filterable view.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import journal as journal_mod


_ENTRIES = [
    {"t": "2026-06-02T00:00:01Z", "actor": "DEVELOPER", "task": "0337",
     "from": "feature_dev", "to": "feature_test", "reason": "impl"},
    {"t": "2026-06-02T00:00:02Z", "actor": "TESTER", "task": "0337",
     "from": "feature_test", "to": "feature_review", "reason": "gate pass"},
    {"t": "2026-06-02T00:00:03Z", "role": "ARCHITECT-PLANNER", "task": "",
     "from": "inbox", "to": "inbox/developer", "reason": "ask: x"},
    {"t": "2026-06-02T00:00:04Z", "actor": "DEVELOPER",
     "task": "0338-journal-tail", "from": "feature_dev",
     "to": "feature_test", "reason": "impl"},
]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    coord = proj / "coordination"
    coord.mkdir(parents=True)
    # the journal-detection helper keys on stand_requests/ existing for
    # a bare coordination dir; create the standard layout marker.
    (coord / "stand_requests").mkdir()
    with (coord / "journal.ndjson").open("w", encoding="utf-8") as f:
        for e in _ENTRIES:
            f.write(json.dumps(e) + "\n")
    monkeypatch.chdir(proj)
    return proj


def _run(args):
    return CliRunner().invoke(journal_mod.journal, args,
                              catch_exceptions=False)


def test_tail_default_shows_all_in_order(project) -> None:
    res = _run(["tail"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 4
    assert "0337" in lines[0] and "DEVELOPER" in lines[0]
    assert "feature_test → feature_review" in lines[1]


def test_tail_n_limits_to_last_n(project) -> None:
    res = _run(["tail", "-n", "2"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 2
    # last two entries
    assert "ARCHITECT-PLANNER" in lines[0]
    assert "0338-journal-tail" in lines[1]


def test_tail_filter_by_role(project) -> None:
    res = _run(["tail", "--role", "developer"])  # case-insensitive
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all("DEVELOPER" in ln for ln in lines)


def test_tail_filter_by_role_matches_role_field(project) -> None:
    """The mv writer uses ``role`` not ``actor`` — must still match."""
    res = _run(["tail", "--role", "ARCHITECT-PLANNER"])
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and "ARCHITECT-PLANNER" in lines[0]


def test_tail_filter_by_task_short_id_matches_full(project) -> None:
    """--task 0338 matches the full ``0338-journal-tail`` entry."""
    res = _run(["tail", "--task", "0338"])
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and "0338-journal-tail" in lines[0]


def test_tail_filter_by_task_exact(project) -> None:
    res = _run(["tail", "--task", "0337"])
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all("0337" in ln for ln in lines)


def test_tail_role_and_task_combined(project) -> None:
    res = _run(["tail", "--role", "TESTER", "--task", "0337"])
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and "TESTER" in lines[0]


def test_tail_is_read_only(project) -> None:
    """journal tail must not modify the journal."""
    jp = project / "coordination" / "journal.ndjson"
    before = jp.read_bytes()
    _run(["tail"])
    assert jp.read_bytes() == before


def test_tail_missing_journal_errors(tmp_path, monkeypatch) -> None:
    proj = tmp_path / "p"
    (proj / "coordination" / "stand_requests").mkdir(parents=True)
    monkeypatch.chdir(proj)
    res = CliRunner().invoke(journal_mod.journal, ["tail"],
                             catch_exceptions=True)
    assert res.exit_code != 0
    out = (res.output or "") + str(res.exception or "")
    assert "journal not found" in out


def test_tail_rejects_nonpositive_n(project) -> None:
    res = CliRunner().invoke(journal_mod.journal, ["tail", "-n", "0"],
                             catch_exceptions=True)
    assert res.exit_code != 0
