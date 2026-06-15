from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from greatminds.cli import driven_log as dl


def test_append_and_read_events(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    dl.append_event(
        coord,
        event="turn_start",
        role="developer",
        tool="codex",
        task="0042-fix",
        message="accepted task",
        log_path="/tmp/developer.log",
    )

    events = dl.read_events(dl.event_log_path(coord), limit=10)

    assert len(events) == 1
    assert events[0]["event"] == "turn_start"
    assert events[0]["role"] == "DEVELOPER"
    assert events[0]["task"] == "0042-fix"


def test_format_event_is_informative_without_color() -> None:
    line = dl.format_event({
        "at": "2026-06-15T08:00:00Z",
        "event": "turn_finish",
        "role": "TESTER",
        "tool": "claude",
        "task": "9001-demo",
        "message": "completed",
        "log_path": "/tmp/tester-1.log",
    }, color=False, width=160)

    assert "\033[" not in line
    assert "08:00:00" in line
    assert "TESTER" in line
    assert "completed" in line
    assert "9001-demo" in line
    assert "log:tester-1.log" in line


def test_render_empty_events() -> None:
    out = dl.render_events([], color=False)
    assert "DRIVEN EVENTS" in out
    assert "no driven events yet" in out


def test_cli_once_prints_existing_events(tmp_path: Path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    path = dl.event_log_path(coord)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "at": "2026-06-15T08:00:00Z",
        "event": "turn_start",
        "role": "DEVELOPER",
        "message": "accepted task",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(dl, "find_coord_dir", lambda: coord)

    result = CliRunner().invoke(dl.driven_log, ["--no-follow", "--no-color"])

    assert result.exit_code == 0, result.output
    assert "DRIVEN EVENTS" in result.output
    assert "DEVELOPER" in result.output
