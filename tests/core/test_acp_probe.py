import json
from pathlib import Path
import subprocess
import sys

import pytest


PROBE = Path(__file__).resolve().parents[2] / "tools" / "acp_probe.py"
SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "acp_server.py"


def test_probe_distinguishes_initialization_session_and_incompatible_protocol():
    for arguments, status, code in [([], "protocol_ok", 0), (["--session"], "session_ok", 0)]:
        result = subprocess.run([sys.executable, str(PROBE), *arguments, "--", sys.executable, str(SERVER), "echo"],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == code, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == status and report["inference"] is False
        assert report["process_exited"] is True
        assert report.get("session_created", False) == bool(arguments)
        assert (Path(report["artifact_dir"]) / "report.json").stat().st_mode & 0o777 == 0o600
    result = subprocess.run([sys.executable, str(PROBE), "--", sys.executable, str(SERVER), "bad-version"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "failed" and report["error_type"] == "ValueError"
    assert report["process_exited"] is True


def test_prompt_probe_requires_observed_response_and_does_not_print_message_content():
    for expected, code in [("hello", 0), ("different", 1)]:
        result = subprocess.run([sys.executable, str(PROBE), "--prompt", "synthetic", "--expect", expected,
                                 "--", sys.executable, str(SERVER), "echo"],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == code, result.stderr
        report = json.loads(result.stdout)
        assert report["expected_text_matches"] == (code == 0)
        assert report["inference"] and report["stop_reason"] == "end_turn"
        assert report["assistant_bytes"] == len("hello")
        assert '"hello"' not in result.stdout


@pytest.mark.parametrize("scenario,arguments,status", [
    ("model", ["--model", "chosen"], "session_ok"),
    ("model-ignore", ["--model", "chosen"], "failed"),
    ("echo", ["--model", "chosen"], "failed"),
    ("resume", ["--resume", "--prompt", "synthetic", "--expect", "hello"], "resume_ok"),
    ("echo", ["--resume"], "resume_failed"),
    ("cancel", ["--prompt", "synthetic", "--cancel-after", "0.1"], "cancel_ok"),
    ("cancel-output", ["--prompt", "synthetic", "--cancel-after", "0.1", "--cancel-on-output"], "cancel_ok"),
    ("echo", ["--prompt", "synthetic", "--cancel-after", "0.5"], "cancel_not_confirmed"),
])
def test_probe_exercises_selection_restart_and_cancellation(scenario, arguments, status):
    result = subprocess.run([sys.executable, str(PROBE), *arguments, "--", sys.executable, str(SERVER), scenario],
                            capture_output=True, text=True, timeout=20)
    report = json.loads(result.stdout)
    assert report["status"] == status, result.stderr
    assert result.returncode == (0 if status in {"session_ok", "resume_ok", "cancel_ok"} else 1)
    assert report["process_exited"]
    if "resume" in report:
        assert report["resume"]["process_exited"]
        assert report["resume"]["process"]["pid"] != report["process"]["pid"]
    if status == "resume_ok":
        workspace = Path(report["artifact_dir"])
        assert (workspace / "agent-starts.log").read_text().splitlines() == ["resume", "resume"]
        assert (workspace / "loads.log").read_text().splitlines() == ["test-session"]
        assert report["resume"]["expected_text_matches"]
    if status == "cancel_ok":
        assert report["cancel_sent"] and report["stop_reason"] == "cancelled"
        if "--cancel-on-output" in arguments:
            assert report["output_before_cancel"] and report["assistant_bytes"] > 0
