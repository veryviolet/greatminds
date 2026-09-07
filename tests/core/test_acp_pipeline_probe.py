import asyncio
import importlib.util
from pathlib import Path
import subprocess
import sys

import yaml
import pytest

from greatminds.runtime.presets import configure_preset

from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore


@pytest.mark.parametrize("use_preset", [False, True])
def test_three_role_pipeline_validates_merges_and_does_not_repeat(use_preset):
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('pipeline_probe', repo / 'tools/acp_pipeline_probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    roles = ['DEVELOPER', 'TESTER', 'ARCHITECT-REVIEWER']
    root, base = probe.create_project({'version': 1, 'agents': {'fixture': {
        'transport': 'acp', 'argv': [sys.executable, str(repo / 'tests/fixtures/acp_server.py'), 'pipeline'],
        'adapter_version': 'fixture', 'harness_version': 'fixture'}},
        'bindings': {f'role{i}': {'role': role, 'agent': 'fixture', 'timeout_seconds': 40}
                     for i, role in enumerate(roles)}})
    if use_preset:
        path = root / "coordination/execution.yaml"
        document = yaml.safe_load(path.read_text())
        document["bindings"] = {}
        path.write_text(yaml.safe_dump(document))
        configure_preset(root, "local", "fixture", apply=True)
    assert asyncio.run(probe.pipeline(root))
    state = RunStore(root / '.greatminds').snapshot()
    assert [r['role'] for r in sorted(state['runs'].values(), key=lambda run: run['sequence'])] == roles
    assert all(r['status'] == 'applied' for r in state['results'].values())
    assert len(state['commands']) == 3
    assert all(c['status'] == 'succeeded' for c in state['commands'].values())
    data = yaml.safe_load((root / '.greatminds/verified/0001-clamp.yaml').read_text())
    assert [b['kind'] for b in data['blocks']] == ['plan', 'implementation', 'tests', 'review']
    assert all(b['provenance']['applied_by'] == 'SYSTEM' for b in data['blocks'][1:])
    assert not (root / '.worktrees/0001-clamp').exists()
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip() != base
    assert subprocess.check_output(['git', 'diff', '--name-only', base, 'HEAD'], cwd=root, text=True).splitlines() == ['clamp.py']
    subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_clamp'], cwd=root, check=True, capture_output=True)
    restarted = asyncio.run(serve(root, once=True, environment={}))
    assert restarted['runs'] == state['runs'] and restarted['results'] == state['results']
