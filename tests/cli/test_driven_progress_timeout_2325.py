"""2.6.1 driven progress timeout regressions."""
from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

from greatminds.cli import coordd as cd


def test_progress_runner_refreshes_heartbeat_on_output(tmp_path: Path):
    coord = tmp_path / "coordination"
    coord.mkdir()
    script = tmp_path / "emit.py"
    script.write_text("print('progress')\n", encoding="utf-8")

    rc, stdout, stderr, timed_out, detail = cd._run_progress_subprocess(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=os.environ.copy(),
        coord=coord,
        role_lower="developer",
        absolute_timeout=10,
        idle_timeout=10,
    )

    assert rc == 0
    assert stdout.strip() == "progress"
    assert stderr == ""
    assert timed_out is False
    assert detail == ""
    assert (coord / "heartbeat.developer").is_file()


def test_progress_runner_allows_long_turn_with_worktree_writes(
        tmp_path: Path):
    coord = tmp_path / "coordination"
    coord.mkdir()
    wt = tmp_path / ".worktrees" / "0001"
    wt.mkdir(parents=True)
    script = tmp_path / "write_wt.py"
    script.write_text(textwrap.dedent(f"""
        import pathlib, time
        p = pathlib.Path({str(wt / "progress.txt")!r})
        for i in range(3):
            p.write_text(str(i))
            time.sleep(0.35)
    """), encoding="utf-8")

    rc, _stdout, _stderr, timed_out, detail = cd._run_progress_subprocess(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=os.environ.copy(),
        coord=coord,
        role_lower="developer",
        absolute_timeout=10,
        idle_timeout=0.6,
    )

    assert rc == 0
    assert timed_out is False
    assert detail == ""


def test_progress_runner_kills_idle_process(tmp_path: Path):
    coord = tmp_path / "coordination"
    coord.mkdir()
    script = tmp_path / "idle.py"
    script.write_text("import time; time.sleep(5)\n", encoding="utf-8")

    started = time.monotonic()
    rc, _stdout, _stderr, timed_out, detail = cd._run_progress_subprocess(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=os.environ.copy(),
        coord=coord,
        role_lower="tester",
        absolute_timeout=10,
        idle_timeout=0.3,
    )

    assert time.monotonic() - started < 3
    assert rc != 0
    assert timed_out is True
    assert "no observed progress" in detail
