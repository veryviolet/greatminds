"""Measure real daemon idle passes on local synthetic conversation/run journals.

No harness is started, no credentials are read, and no user project is changed.
Setup uses real store operations; setup time is excluded. Run before and after
an optimization with identical arguments on the same host.
"""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

import yaml

from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.daemon import serve
from greatminds.runtime.interactions import ConversationStore
from greatminds.runtime.store import RunStore, TaskRevision


def create_fixture(root, conversations=50, history=100):
    (root / 'coordination').mkdir()
    path = root / 'coordination/execution.yaml'
    path.write_text(yaml.safe_dump({'version': 1, 'agents': {'fixture': {
        'transport': 'acp', 'argv': ['unused-acp-fixture'],
        'adapter_version': 'fixture', 'harness_version': 'fixture'}},
        'bindings': {'planner': {'agent': 'fixture', 'role': 'ARCHITECT-PLANNER',
                                 'scheduling': 'on-demand'}}}))
    schema = load_schema_snapshot()
    config = load_execution_config(path, roles=set(schema.document['roles']))
    binding = config.bindings[0]
    runtime = root / '.greatminds'
    chats = [ConversationStore.create(runtime, binding=binding, config_sha256=config.sha256,
             schema_sha256=schema.sha256, workspace=root) for _ in range(conversations)]
    store = RunStore(runtime)
    for index in range(history):
        chat = chats[index % len(chats)]
        claim = store.claim(task=TaskRevision.conversation(runtime, chat.id), binding=binding,
                            config=config, schema=schema, project=root, owner_id='benchmark',
                            conversation_id=chat.id)
        store.transition(claim.run['id'], owner_id='benchmark', event_id=f'cancel-{index}',
                         target='cancelled', reason='benchmark_history')
    return store


def measure(root, repeats=5):
    samples = []
    original = RunStore._read
    def forbidden(*args, **kwargs):
        raise AssertionError('idle benchmark attempted to launch a harness')
    for _ in range(repeats):
        reads = 0
        def counted(store):
            nonlocal reads
            reads += 1
            return original(store)
        started = time.perf_counter()
        with patch.object(RunStore, '_read', counted), patch(
                'greatminds.runtime.acp_transport.AcpTransport.__aenter__', forbidden):
            state = asyncio.run(serve(root, once=True, environment={}))
        samples.append({'seconds': time.perf_counter() - started, 'runtime_reads': reads})
    return {'samples': samples, 'median_seconds': statistics.median(s['seconds'] for s in samples),
            'run_count': len(state['runs']), 'result_count': len(state['results'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--conversations', type=int, default=50)
    parser.add_argument('--history', type=int, default=100)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if not (1 <= args.conversations <= 1000 and 0 <= args.history <= 10000 and 1 <= args.repeats <= 20):
        parser.error('require conversations 1..1000, history 0..10000, repeats 1..20')
    with tempfile.TemporaryDirectory(prefix='greatminds-idle-benchmark-') as directory:
        root = Path(directory)
        store = create_fixture(root, args.conversations, args.history)
        measure(root, 1)  # Initialize deterministic maintenance findings; exclude warm-up.
        before = store.snapshot()
        measured = measure(root, args.repeats)
        assert store.snapshot() == before, 'idle passes changed runtime history'
        print(json.dumps({'version': 1, 'fixture': vars(args), **measured}, indent=2))


if __name__ == '__main__':
    main()
