from __future__ import annotations

import json
from pathlib import Path

from greatminds.cli import coordd as cd
from greatminds.cli import driven_log as dl


def test_claude_spawn_seam_writes_driven_events(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()

    ok, _diag = cd._spawn_driven_turn(
        coord,
        "developer",
        "",
        None,
        None,
        None,
        False,
        spawn=lambda _argv: None,
        force_fresh=True,
    )

    assert ok is True
    events = dl.read_events(dl.event_log_path(coord), limit=10)
    assert [e["event"] for e in events] == ["turn_start", "turn_finish"]
    assert all(e["role"] == "DEVELOPER" for e in events)
    assert all(e["tool"] == "claude" for e in events)


def test_codex_transport_error_writes_driven_error(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()

    def transport(_request: dict) -> dict:
        raise RuntimeError("boom")

    ok, diag = cd._spawn_driven_codex_turn(
        coord,
        "tester",
        "contract",
        str(tmp_path),
        False,
        transport=transport,
    )

    assert ok is False
    assert "boom" in diag
    events = dl.read_events(dl.event_log_path(coord), limit=10)
    assert [e["event"] for e in events] == ["turn_start", "error"]
    assert events[-1]["role"] == "TESTER"
    assert events[-1]["tool"] == "codex"
    assert "boom" in events[-1]["detail"]


def test_driven_event_task_refs_ignore_wake_markers(tmp_path: Path,
                                                    monkeypatch) -> None:
    coord = tmp_path / "coordination"
    (coord / "feature_dev").mkdir(parents=True)
    (coord / "inbox" / "developer").mkdir(parents=True)
    (coord / "feature_dev" / "0042-real.yaml").write_text("id: 0042-real\n")
    (coord / "inbox" / "developer" / "wake-20260615T000000Z-0042.yaml").write_text(
        "kind: wake\n")
    monkeypatch.setattr(cd, "load_schema_roles",
                        lambda _coord: {"DEVELOPER": {"claims_from": [
                            "feature_dev"]}})

    refs = cd._driven_event_task_refs(coord, "developer")

    assert refs == "0042-real"
