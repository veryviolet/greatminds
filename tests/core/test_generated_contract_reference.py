import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools/generate_contract_reference.py'


def generator():
    spec = importlib.util.spec_from_file_location('contract_reference', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_checked_in_reference_is_reproducible_and_covers_all_contracts():
    module = generator()
    rendered = module.render()
    assert module.OUTPUT.read_text() == rendered
    for name in module.load_schema_snapshot(ROOT / 'src/greatminds/data').document['roles']:
        assert '### ' + name + '\n' in rendered
    for name in ('AgentManifest', 'RoleBinding', 'CommandDefinition', 'ExecutionConfig',
                 'AccountLimitError', 'StandDeploymentPolicy', 'TaskRevision', 'ResultEnvelope'):
        assert '### ' + name + '\n' in rendered
    assert module.render() == rendered


@pytest.mark.parametrize('fault', ['role', 'queue', 'validator'])
def test_invalid_contract_references_cannot_be_published(fault):
    module = generator()
    doc = module.load_schema_snapshot(ROOT / 'src/greatminds/data').document
    if fault == 'role':
        doc['transitions'][0]['by'] = 'UNKNOWN'
    elif fault == 'queue':
        doc['roles']['TESTER']['claims_from'].append('unknown_queue')
    else:
        doc['transitions'][0]['requires'].append('unknown_validator')
    with pytest.raises(ValueError, match='unknown|unregistered'):
        module.validate(doc)


def test_check_reports_drift_without_repairing_output(tmp_path):
    output = tmp_path / 'reference.md'
    output.write_text('stale document\n')
    result = subprocess.run([sys.executable, str(TOOL), '--check', '--output', str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 1 and 'stale' in result.stderr
    assert output.read_text() == 'stale document\n'
    generated = subprocess.run([sys.executable, str(TOOL), '--output', str(output)], capture_output=True, text=True)
    assert generated.returncode == 0, generated.stderr
    assert subprocess.run([sys.executable, str(TOOL), '--check', '--output', str(output)]).returncode == 0


def test_generation_rejects_ambient_canon_override(tmp_path):
    result = subprocess.run([sys.executable, str(TOOL), '--check'], capture_output=True, text=True,
                            env={**os.environ, 'GREATMINDS_CANON_DIR': str(tmp_path)})
    assert result.returncode == 1 and 'unset GREATMINDS_CANON_DIR' in result.stderr
