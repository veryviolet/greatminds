"""GitHub issue #15: wake-check must flag a blocked dependency that can
never resolve because the target task sits in a TERMINAL queue other than
the one the dependency path expects (e.g. dep wants ``verified/<id>`` but
the task is in ``archive/``). Previously such a dep was reported as plain
"missing" — indistinguishable from a not-yet-created task — so the blocked
task sat forever with no actionable signal.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli import wake_check as wc
from greatminds.cli.wake_check import wake_check, find_dep_in_queues


def _make(tmp_path: Path) -> tuple[Path, Path]:
    """Minimal project (coordination/) + canon (schema.yaml) with verified
    and archive marked terminal."""
    project = tmp_path / "proj"
    coord = project / "coordination"
    for q in ("feature_blocked", "verified", "archive", "feature_dev"):
        (coord / q).mkdir(parents=True)
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "queues": {
            "feature_blocked": {"kind": "active"},
            "feature_dev": {"kind": "active"},
            "verified": {"kind": "terminal"},
            "archive": {"kind": "terminal"},
        }
    }), encoding="utf-8")
    return project, canon


def _blocked(coord: Path, tid: str, deps: list[str], resume_to="feature_dev"):
    (coord / "feature_blocked" / f"{tid}.yaml").write_text(yaml.safe_dump({
        "id": tid, "queue": "feature_blocked",
        "blocks": [{"kind": "blocked", "dependencies": deps,
                    "resume_to": resume_to}],
    }), encoding="utf-8")


def _run(project: Path, canon: Path):
    return CliRunner().invoke(
        wake_check,
        ["--project-dir", str(project), "--canon-dir", str(canon)],
    )


def test_find_dep_in_queues_locates_target(tmp_path):
    project, _ = _make(tmp_path)
    coord = project / "coordination"
    (coord / "archive" / "0123-foo.yaml").write_text("id: x\n")
    assert find_dep_in_queues(
        coord, "0123-foo",
        {"verified", "archive", "feature_dev"}) == ["archive"]


def test_dead_dep_target_in_other_terminal_queue(tmp_path):
    """Dep wants verified/0123 but 0123 is in archive (terminal) → DEAD,
    not 'ready' and not plain 'missing'."""
    project, canon = _make(tmp_path)
    coord = project / "coordination"
    (coord / "archive" / "0123-foo.yaml").write_text("id: x\n")
    _blocked(coord, "0200-waiter", ["verified/0123-foo.yaml"])

    res = _run(project, canon)
    assert res.exit_code == 0
    assert "DEAD DEPENDENCIES" in res.output
    assert "0200-waiter" in res.output
    assert "archive" in res.output
    # must NOT be misreported as ready to wake
    assert "READY TO WAKE" not in res.output


def test_satisfied_dep_in_expected_queue_is_ready(tmp_path):
    """Control: dep wants verified/0123 and 0123 IS in verified → ready,
    no dead finding."""
    project, canon = _make(tmp_path)
    coord = project / "coordination"
    (coord / "verified" / "0123-foo.yaml").write_text("id: x\n")
    _blocked(coord, "0200-waiter", ["verified/0123-foo.yaml"])

    res = _run(project, canon)
    assert "DEAD DEPENDENCIES" not in res.output
    assert "READY TO WAKE" in res.output and "0200-waiter" in res.output


def test_truly_missing_dep_is_not_dead(tmp_path):
    """Control: target task exists nowhere → still plain 'missing' (may be
    created later), NOT a dead-dependency finding."""
    project, canon = _make(tmp_path)
    coord = project / "coordination"
    _blocked(coord, "0200-waiter", ["verified/0999-ghost.yaml"])

    res = _run(project, canon)
    assert "DEAD DEPENDENCIES" not in res.output
    assert "missing" in res.output


def test_cascading_blocked_dep_surfaces_dead_root(tmp_path):
    """Cascade: A blocked on B (in feature_blocked, active → A not ready),
    B blocked on a dead dep (target in archive). wake-check must flag B as
    dead so fixing B unblocks A."""
    project, canon = _make(tmp_path)
    coord = project / "coordination"
    (coord / "archive" / "0123-foo.yaml").write_text("id: x\n")
    _blocked(coord, "0300-b", ["verified/0123-foo.yaml"])
    _blocked(coord, "0301-a", ["feature_blocked/0300-b.yaml"])

    res = _run(project, canon)
    assert "DEAD DEPENDENCIES" in res.output
    # the dead-dependency finding names the root (B), not the cascaded
    # waiter (A) — A is merely waiting on B which sits in an active queue.
    dead_section = res.output.split("DEAD DEPENDENCIES", 1)[1].split(
        "BLOCKED", 1)[0]
    assert "0300-b" in dead_section
    assert "0301-a" not in dead_section
    # A is reported as blocked/not-ready (waiting on B), never woken
    assert "0301-a" in res.output
    assert "READY TO WAKE" not in res.output
