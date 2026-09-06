import json
from pathlib import Path
import subprocess
import sys


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
