import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.parametrize('scenario', ['resume', 'echo'])
def test_public_conversation_probe_restarts_daemon_and_resumes_cursor(tmp_path, scenario):
    repo = Path(__file__).resolve().parents[2]
    config = tmp_path / 'execution.yaml'
    config.write_text(yaml.safe_dump({'agents': {'fixture': {
        'transport': 'acp', 'argv': [sys.executable, str(repo / 'tests/fixtures/acp_server.py'), scenario],
        'adapter_version': 'fixture', 'harness_version': 'fixture'}}}))
    result = subprocess.run([sys.executable, str(repo / 'tools/acp_conversation_probe.py'),
        '--config', str(config), '--agent', 'fixture', '--token', 'hello', '--timeout', '10'],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == (0 if scenario == 'resume' else 1), (result.stdout, result.stderr)
    rows = [json.loads(line) for line in result.stdout.splitlines()][1:]
    assert [row['role'] for row in rows] == ['ARCHITECT-PLANNER', 'LIVE-DEVELOPER']
    if scenario == 'resume':
        assert all(row['passed'] for row in rows)
        assert all(row['session_strategies'] == ['new_context', 'loaded'] for row in rows)
    else:
        assert all(not row['passed'] and not row['resumed_reply_exact'] for row in rows)
        assert all(row['run_states'] == ['completed', 'failed'] for row in rows)
