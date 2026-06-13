from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml
from click.testing import CliRunner

from greatminds.cli.watchdog import watchdog


def test_watchdog_reports_stuck_driven_lock_with_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    coord = project / "coordination"
    locks = coord / ".locks"
    locks.mkdir(parents=True)
    (coord / "intent").mkdir()
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "watchdog": {
            "intent_orphan_seconds": 300,
            "task_stale_in_active_queue_seconds": 86400,
            "task_stale_in_review_queue_seconds": 43200,
        },
        "heartbeat": {"hang_threshold_seconds": 300},
        "queues": {},
    }), encoding="utf-8")

    log = coord / ".turns" / "developer-20260613T000000Z.log"
    lock = locks / "driven-developer.lock"
    lock.write_text(json.dumps({
        "role": "DEVELOPER",
        "driver": "codex",
        "log_path": str(log),
    }) + "\n", encoding="utf-8")
    old = time.time() - 400
    os.utime(lock, (old, old))
    (locks / "driven-developer.pending").write_text("", encoding="utf-8")

    result = CliRunner().invoke(watchdog, [
        "--project-dir", str(project),
        "--canon-dir", str(canon),
    ])

    assert result.exit_code == 0
    assert "STUCK DRIVEN TURNS (1" in result.output
    assert "developer: lock 6m old" in result.output
    assert "driver=codex" in result.output
    assert "log=developer-20260613T000000Z.log" in result.output
    assert "pending" in result.output
    assert "no fresh turn log" in result.output
