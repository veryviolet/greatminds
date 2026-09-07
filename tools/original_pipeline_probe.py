"""Run the archived native daemon against a deterministic local task fixture.

The parent only seeds and observes a disposable project. All task execution uses
the supplied archived wheel interpreter and its unmodified daemon/domain code.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

import yaml

from acp_pipeline_probe import create_project


ROLES = ['DEVELOPER', 'TESTER', 'ARCHITECT-REVIEWER']
AGENT = Path(__file__).resolve().parents[1] / 'tests/fixtures/original_pipeline_agent.py'


def run(python, timeout):
    identity = json.loads(subprocess.check_output([str(python), '-I', '-c',
        'import greatminds,json; from greatminds.cli import coordd; '
        'print(json.dumps({"package":greatminds.__file__,"native_driver":hasattr(coordd,"_spawn_driven_headless_turn")}))'], text=True))
    if not identity['native_driver'] or 'site-packages' not in Path(identity['package']).parts:
        raise ValueError('supply an installed original package interpreter')
    root, base = create_project({'version': 1, 'agents': {'unused': {'transport': 'acp',
        'argv': ['unused'], 'adapter_version': 'fixture', 'harness_version': 'fixture'}},
        'bindings': {str(i): {'role': role, 'agent': 'unused'} for i, role in enumerate(ROLES)}})
    # The archived daemon uses coord.yaml; it must not accidentally read a modern execution contract.
    (root / 'coordination/execution.yaml').unlink()
    (root / 'coordination/coord.yaml').write_text(yaml.safe_dump({'session': 'benchmark', 'windows': [
        {'name': role.lower(), 'role': role, 'tool': 'cline', 'mode': 'driven'} for role in ROLES]}))
    for queue in ('feature_test', 'feature_review', 'verified'):
        (root / '.greatminds' / queue).mkdir(exist_ok=True)
    canon = root / '.greatminds/benchmark-canon'
    shutil.copytree(Path(identity['package']).parent / 'data', canon)
    schema_path = canon / 'schema.yaml'
    original_schema = yaml.safe_load(schema_path.read_text())
    identity['original_schema_sha256'] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    original_schema.setdefault('auto_update', {})['mode'] = 'disabled'
    schema_path.write_text(yaml.safe_dump(original_schema, sort_keys=False))
    identity['effective_schema_sha256'] = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    identity['schema_override'] = {'auto_update.mode': 'disabled'}
    binaries = root / '.greatminds/benchmark-bin'
    binaries.mkdir()
    wrapper = binaries / 'cline'
    wrapper.write_text(f'#!{python}\nimport runpy\nrunpy.run_path({str(AGENT)!r}, run_name="__main__")\n')
    wrapper.chmod(0o700)
    environment = {'PATH': f'{binaries}:/usr/bin:/bin', 'GREATMINDS_PROJECT_DIR': str(root),
                   'GREATMINDS_CANON_DIR': str(canon),
                   'GREATMINDS_SKIP_PLUGIN_INSTALL': '1', 'PYTHONUNBUFFERED': '1'}
    log = root / '.greatminds/benchmark-daemon.log'
    started = time.perf_counter()
    print(json.dumps({'project': str(root)}), flush=True)
    with log.open('w') as output:
        process = subprocess.Popen([str(python), '-I', '-m', 'greatminds.cli.main', 'coordd',
            '--project-dir', str(root), '--interval-sec', '.2', '--verbose'], cwd=root,
            env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while not ((root / '.greatminds/verified/0001-clamp.yaml').is_file()
                       and not (root / '.worktrees/0001-clamp').exists()
                       and not list((root / '.greatminds/.locks').glob('driven-*.lock'))):
                if (process.poll() is not None or time.perf_counter() - started > timeout
                        or '  retry:' in log.read_text()):
                    raise RuntimeError(f'original pipeline incomplete; inspect {log}')
                time.sleep(.05)
            elapsed = time.perf_counter() - started
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    task = yaml.safe_load((root / '.greatminds/verified/0001-clamp.yaml').read_text())
    assert [b['kind'] for b in task['blocks']] == ['plan', 'implementation', 'tests', 'review']
    assert not (root / '.worktrees/0001-clamp').exists()
    assert subprocess.check_output(['git','diff','--name-only',base,'HEAD'],cwd=root,text=True).splitlines() == ['clamp.py']
    checks = [json.loads((root / '.greatminds/benchmark-checks' / f'{role}.json').read_text()) for role in ROLES]
    invocations = [json.loads(line) for line in (root / '.greatminds/benchmark-invocations.jsonl').read_text().splitlines()]
    assert [item['role'] for item in invocations if item['action'] == 'work'] == ROLES, invocations
    assert all(check['returncode'] == 0 and 'Ran 6 tests' in check['stderr'] for check in checks)
    subprocess.run([str(python), '-m', 'unittest', '-v', 'test_clamp'], cwd=root, check=True, capture_output=True)
    before_task = (root / '.greatminds/verified/0001-clamp.yaml').read_bytes()
    before_invocations = (root / '.greatminds/benchmark-invocations.jsonl').read_bytes()
    restart_log = root / '.greatminds/benchmark-restart.log'
    with restart_log.open('w') as output:
        restarted = subprocess.Popen([str(python), '-I', '-m', 'greatminds.cli.main', 'coordd',
            '--project-dir', str(root), '--interval-sec', '.2', '--verbose'], cwd=root,
            env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while not all(f'reconcile {role}: skip: no pending work' in restart_log.read_text() for role in ROLES):
                if restarted.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f'restart observation incomplete; inspect {restart_log}')
                time.sleep(.05)
            time.sleep(.4)
        finally:
            if restarted.poll() is None:
                os.killpg(restarted.pid, signal.SIGTERM)
                try:
                    restarted.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(restarted.pid, signal.SIGKILL)
                    restarted.wait()
    restart_unchanged = ((root / '.greatminds/verified/0001-clamp.yaml').read_bytes() == before_task
                        and (root / '.greatminds/benchmark-invocations.jsonl').read_bytes() == before_invocations
                        and not (root / '.worktrees/0001-clamp').exists())
    return {'project': str(root), 'implementation': identity, 'elapsed_seconds': elapsed,
            'verified': True, 'agent_owned_checks': 3, 'independent_test_cases': 6,
            'agent_invocations': len(invocations), 'idle_invocations': sum(item['action'] == 'idle' for item in invocations),
            'fixture_sha256': hashlib.sha256(AGENT.read_bytes()).hexdigest(),
            'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'project_builder_sha256': hashlib.sha256(Path(__file__).with_name('acp_pipeline_probe.py').read_bytes()).hexdigest(),
            'restart_unchanged': restart_unchanged, 'inference': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 300:
        parser.error('timeout must be 1..300 seconds')
    print(json.dumps(run(args.python.absolute(), args.timeout)), flush=True)


if __name__ == '__main__':
    main()
