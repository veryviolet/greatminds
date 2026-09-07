"""Measure a fixed offline ACP task with real daemon checks, merge and recovery.

Run this same file with independently installed benchmark Python environments.
The fixture agent is deterministic; this measures orchestration, not inference.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

import yaml

import greatminds
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.daemon import serve
from greatminds.runtime.interactions import ConversationStore
from greatminds.runtime.store import RunStore, TaskRevision


HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / 'tests/fixtures/acp_server.py'
ROLES = ['DEVELOPER', 'TESTER', 'ARCHITECT-REVIEWER']


def package_identity():
    root = Path(greatminds.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            digest.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    return {'version': importlib.metadata.version('greatminds'), 'package_root': str(root),
            'package_sha256': digest.hexdigest(), 'python': sys.version.split()[0],
            'dependencies': {name: importlib.metadata.version(name) for name in (
                'agent-client-protocol', 'pydantic', 'pydantic-core', 'pyyaml', 'click', 'packaging')},
            'schema_sha256': load_schema_snapshot().sha256,
            'installed': 'site-packages' in root.parts}


def create_fixture(conversations, history):
    spec = importlib.util.spec_from_file_location('benchmark_pipeline', HERE / 'acp_pipeline_probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    root, base = probe.create_project({'version': 1, 'agents': {'fixture': {
        'transport': 'acp', 'argv': [sys.executable, str(SERVER), 'pipeline'],
        'adapter_version': 'deterministic-benchmark', 'harness_version': 'deterministic-benchmark'}},
        'bindings': {f'role{i}': {'role': role, 'agent': 'fixture', 'timeout_seconds': 60}
                     for i, role in enumerate(ROLES)}})
    config_path = root / 'coordination/execution.yaml'
    document = yaml.safe_load(config_path.read_text())
    document['bindings']['idle'] = {'role': 'ARCHITECT-PLANNER', 'agent': 'fixture', 'scheduling': 'on-demand'}
    config_path.write_text(yaml.safe_dump(document))
    schema = load_schema_snapshot()
    config = load_execution_config(config_path, roles=set(schema.document['roles']))
    binding = next(item for item in config.bindings if item.id == 'idle')
    store = RunStore(root / '.greatminds')
    chats = [ConversationStore.create(store.runtime, binding=binding, config_sha256=config.sha256,
             schema_sha256=schema.sha256, workspace=root) for _ in range(conversations)]
    for index in range(history):
        chat = chats[index % len(chats)]
        claim = store.claim(task=TaskRevision.conversation(store.runtime, chat.id), binding=binding,
                            config=config, schema=schema, project=root, owner_id='benchmark', conversation_id=chat.id)
        store.transition(claim.run['id'], owner_id='benchmark', event_id=f'cancel-{index}',
                         target='cancelled', reason='benchmark_history')
    return root, base, store


def measure(conversations=50, history=100):
    root, base, store = create_fixture(conversations, history)
    original_state = store.snapshot()
    reads = 0
    original_read = RunStore._read
    def counted(instance):
        nonlocal reads
        reads += 1
        return original_read(instance)
    started = time.perf_counter()
    stages = []
    with patch.object(RunStore, '_read', counted):
        for _ in ROLES:
            stage_started = time.perf_counter()
            state = asyncio.run(serve(root, once=True, interval=.2, environment={}))
            stages.append(time.perf_counter() - stage_started)
            new_runs = [run for key, run in state['runs'].items() if key not in original_state['runs']]
            if any(run['state'] != 'completed' for run in new_runs):
                raise AssertionError(f'pipeline did not complete in {root}')
    elapsed = time.perf_counter() - started
    runs = sorted(new_runs, key=lambda run: run['sequence'])
    assert [run['role'] for run in runs] == ROLES
    assert len(state['results']) == 3 and all(r['status'] == 'applied' for r in state['results'].values())
    assert len(state['commands']) == 3 and all(c['status'] == 'succeeded' for c in state['commands'].values())
    assert all(state['runs'][key] == value for key, value in original_state['runs'].items())
    task = yaml.safe_load((root / '.greatminds/verified/0001-clamp.yaml').read_text())
    assert [block['kind'] for block in task['blocks']] == ['plan', 'implementation', 'tests', 'review']
    assert all(block['provenance']['applied_by'] == 'SYSTEM' for block in task['blocks'][1:])
    assert not (root / '.worktrees/0001-clamp').exists()
    changed = subprocess.check_output(['git', 'diff', '--name-only', base, 'HEAD'], cwd=root, text=True).splitlines()
    assert changed == ['clamp.py']
    subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_clamp'], cwd=root,
                   check=True, capture_output=True)
    restarted = asyncio.run(serve(root, once=True, environment={}))
    for key in ('runs', 'results', 'commands'):
        assert restarted[key] == state[key], f'restart repeated {key}'
    receipts = {r['envelope']['run_id']: r for r in state['results'].values()}
    return {'project': str(root), 'elapsed_seconds': elapsed, 'daemon_runtime_reads': reads,
            'stage_seconds': stages, 'run_count': len(runs), 'applied_results': 3,
            'successful_commands': 3, 'independent_test_cases': 6, 'restart_unchanged': True,
            'roles': [{'role': run['role'], 'context_bytes': run.get('outcome', {}).get('context_bytes'),
                       'timings': run.get('timings'), 'queue': run.get('queue_observation'),
                       'progress': run.get('domain_progress'),
                       'validation': receipts[run['id']].get('validation'),
                       'result_timings': receipts[run['id']].get('timings')} for run in runs]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--conversations', type=int, default=50)
    parser.add_argument('--history', type=int, default=100)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, help='write the final report to a new JSON file')
    parser.add_argument('--allow-source', action='store_true', help='development checks only; not an installed comparison')
    args = parser.parse_args()
    if not (0 <= args.conversations <= 1000 and 0 <= args.history <= 10000 and 1 <= args.repeats <= 20):
        parser.error('invalid fixture size or repetition count')
    if args.history and not args.conversations:
        parser.error('history requires conversations')
    if args.output is not None and args.output.exists():
        parser.error('output file already exists')
    if any(os.environ.get(name) for name in ('GREATMINDS_CANON_DIR', 'GREATMINDS_PROJECT_DIR',
                                            'PYTHONPATH', 'PYTHONHOME')):
        parser.error('unset canon/project/Python import overrides before benchmarking')
    identity = package_identity()
    if not identity['installed'] and not args.allow_source:
        parser.error('use an independently installed wheel environment, or --allow-source for development')
    samples = []
    for _ in range(args.repeats):
        sample = measure(args.conversations, args.history)
        samples.append(sample)
        print(json.dumps({'sample': sample}), flush=True)
    report = {'version': 1, 'implementation': identity,
        'fixture_sha256': hashlib.sha256(SERVER.read_bytes()).hexdigest(),
        'project_builder_sha256': hashlib.sha256((HERE / 'acp_pipeline_probe.py').read_bytes()).hexdigest(),
        'benchmark_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'inference': False, 'conversations': args.conversations, 'history': args.history,
        'samples': samples, 'median_seconds': statistics.median(s['elapsed_seconds'] for s in samples)}
    if args.output is not None:
        with args.output.open('x') as handle:
            handle.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
