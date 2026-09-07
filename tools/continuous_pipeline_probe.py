"""Measure one continuously running ACP daemon on the shared offline task."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import yaml

from productive_benchmark import create_fixture, package_identity, ROLES, SERVER
from greatminds.runtime.daemon import serve


def main():
    identity = package_identity()
    if not identity['installed']:
        raise ValueError('run with an independently installed wheel interpreter')
    root, base, store = create_fixture(0, 0)
    log = root / '.greatminds/benchmark-daemon.log'
    print(json.dumps({'project': str(root)}), flush=True)
    started = time.perf_counter()
    with log.open('w') as output:
        process = subprocess.Popen([sys.executable, '-I', '-m', 'greatminds.cli.main', 'coordd', '--foreground',
            '--project-dir', str(root), '--interval-sec', '.2'], cwd=root,
            env={'PATH': '/usr/bin:/bin', 'PYTHONUNBUFFERED': '1'}, stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                state = store.snapshot()
                runs = sorted(state['runs'].values(), key=lambda r: r['sequence'])
                if any(r['state'] in {'failed', 'interrupted', 'cancelled'} for r in runs):
                    raise RuntimeError(f'pipeline run failed; inspect {root}')
                if (len(runs) == 3 and all(r['state'] == 'completed' for r in runs)
                        and len(state['results']) == 3 and all(r['status'] == 'applied' for r in state['results'].values())
                        and not (root / '.worktrees/0001-clamp').exists()):
                    break
                if process.poll() is not None or time.perf_counter() - started > 180:
                    raise RuntimeError(f'pipeline incomplete; inspect {log}')
                time.sleep(.05)
            elapsed = time.perf_counter() - started
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    assert [r['role'] for r in runs] == ROLES
    assert len(state['commands']) == 3 and all(c['status'] == 'succeeded' for c in state['commands'].values())
    task = yaml.safe_load((root / '.greatminds/verified/0001-clamp.yaml').read_text())
    assert [b['kind'] for b in task['blocks']] == ['plan','implementation','tests','review']
    assert all(b['provenance']['applied_by'] == 'SYSTEM' for b in task['blocks'][1:])
    assert subprocess.check_output(['git','diff','--name-only',base,'HEAD'],cwd=root,text=True).splitlines() == ['clamp.py']
    subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_clamp'], cwd=root, check=True, capture_output=True)
    restarted = asyncio.run(serve(root, once=True, environment={}))
    assert all(restarted[key] == state[key] for key in ('runs','results','commands'))
    print(json.dumps({'project': str(root), 'implementation': identity, 'elapsed_seconds': elapsed,
        'verified': True, 'daemon_owned_checks': 3, 'independent_test_cases': 6, 'agent_invocations': 3,
        'idle_invocations': 0, 'restart_unchanged': True, 'inference': False,
        'fixture_sha256': hashlib.sha256(SERVER.read_bytes()).hexdigest(),
        'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'project_builder_sha256': hashlib.sha256(Path(__file__).with_name('acp_pipeline_probe.py').read_bytes()).hexdigest(),
        'roles': [{'role':r['role'],'timings':r.get('timings'),'queue':r.get('queue_observation'),
                  'progress':r.get('domain_progress')} for r in runs]}), flush=True)


if __name__ == '__main__':
    main()
