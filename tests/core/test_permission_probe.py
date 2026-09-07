import json
from pathlib import Path
import subprocess
import sys
import time

from greatminds.runtime.permissions import PermissionService
from greatminds.runtime.store import RunStore


def test_permission_probe_cancels_pending_callback_without_side_effect():
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(repo / 'tools/acp_permission_probe.py'),
        '--adapter-version', 'fixture', '--harness-version', 'fixture', '--timeout', '10',
        '--cancel-while-pending', '--', sys.executable,
        str(repo / 'tests/fixtures/acp_server.py'), 'permission-marker'],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.splitlines()[-1])
    assert report['passed'] and report['state'] == 'cancelled'
    assert report['marker'] is None and report['process_group_exited']
    assert all(item['status'] == 'cancelled' for item in report['permissions'])


def test_permission_probe_observes_operator_gate_and_fixture_side_effect(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    output = tmp_path / "probe.jsonl"
    with output.open("w") as handle:
        process = subprocess.Popen([sys.executable, str(repo / "tools/acp_permission_probe.py"),
            "--adapter-version", "fixture", "--harness-version", "fixture", "--timeout", "10", "--",
            sys.executable, str(repo / "tests/fixtures/acp_server.py"), "permission-marker"],
            stdout=handle, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        request = None
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read().decode() if process.poll() is not None else ""
            lines = output.read_text().splitlines()
            if lines:
                initial = json.loads(lines[0])
                store = RunStore(Path(initial["runtime"]))
                requests = list(store.snapshot().get("permissions", {}).values())
                if requests:
                    request = requests[0]
                    break
            time.sleep(.05)
        assert request is not None
        assert not (Path(initial["project"]) / "acp-permission-check.txt").exists()
        # Let the probe observe its pending request before the operator answers.
        time.sleep(.1)
        PermissionService(store).answer(request["id"], "yes")
        _, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, stderr.decode()
        result = json.loads(output.read_text().splitlines()[-1])
        assert result["passed"] and result["permission_observed_before_marker"]
        assert result["permissions"][0]["status"] == "consumed"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
