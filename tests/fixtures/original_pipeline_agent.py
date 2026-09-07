"""Deterministic agent for the archived pre-ACP package, not a real harness."""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

root = Path(os.environ['GREATMINDS_PROJECT_DIR'])
role = os.environ['GREATMINDS_ROLE']
def cli(*args):
    result = subprocess.run([sys.executable, '-I', '-m', 'greatminds.cli.main', *args],
                            text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'archived CLI failed: {result.stderr}')
    return result

def acknowledge():
    for path in sorted((root / '.greatminds/inbox' / role.lower()).glob('*')):
        if path.is_file() and not path.name.startswith('processed-'):
            cli('inbox', 'ack', str(path))

queue = {'DEVELOPER': 'feature_dev', 'TESTER': 'feature_test', 'ARCHITECT-REVIEWER': 'feature_review'}[role]
has_work = (root / '.greatminds' / queue / '0001-clamp.yaml').exists()
with (root / '.greatminds/benchmark-invocations.jsonl').open('a') as log:
    log.write(json.dumps({'role': role, 'action': 'work' if has_work else 'idle', 'at': time.time()}) + '\n')
acknowledge()
if not has_work:
    print(json.dumps({'type': 'result', 'is_error': False, 'result': 'no pending fixture task'}))
    raise SystemExit(0)
identifier = '0001-clamp'
if role == 'DEVELOPER':
    cli('worktree', 'create', identifier, '--project-dir', str(root))
workspace = root / '.worktrees' / identifier
os.chdir(workspace)
if role == 'DEVELOPER':
    Path('clamp.py').write_text("def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('invalid bounds')\n    return min(max(value, lower), upper)\n")
started = time.perf_counter()
checked = subprocess.run([sys.executable, '-m', 'unittest', '-v', 'test_clamp'], capture_output=True, text=True)
assert checked.returncode == 0 and 'Ran 6 tests' in checked.stderr, checked.stderr
evidence = root / '.greatminds' / 'benchmark-checks'
evidence.mkdir(exist_ok=True)
(evidence / f'{role}.json').write_text(json.dumps({'role': role, 'returncode': checked.returncode,
    'seconds': time.perf_counter() - started, 'stderr': checked.stderr}))
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
if role == 'DEVELOPER':
    target, kind, fields = 'feature_test', 'implementation', {
        'base_commit': commit, 'files': ['clamp.py'], 'ready_for_test': True}
elif role == 'TESTER':
    target, kind, fields = 'feature_review', 'tests', {
        'base_commit': commit, 'test_files': ['test_clamp.py'],
        'functional_probes': [shlex.join([sys.executable, '-m', 'unittest', '-v', 'test_clamp'])],
        'stand_evidence': {'tester_observations': 'Local worktree tests; no stand required by the plan.\n' + checked.stderr},
        'test_command': shlex.join([sys.executable, '-m', 'unittest', '-v', 'test_clamp']),
        'test_result': 'pass',
        'gate_check_result': 'n/a', 'gate_check_commit': commit,
        'gate_check_at': datetime.now(timezone.utc).isoformat(), 'ready_for_review': True}
elif role == 'ARCHITECT-REVIEWER':
    target, kind, fields = 'verified', 'review', {'outcome': 'approved', 'commit': commit}
else:
    raise ValueError('unexpected fixture role')
arguments = ['task', 'append-block', kind, '--id', identifier]
for key, value in fields.items():
    arguments.extend(['--field', key + '=' + (value if isinstance(value, str) else json.dumps(value))])
cli(*arguments)
cli('task', 'mv', identifier, target)
os.chdir(root)
acknowledge()
print(json.dumps({'type': 'result', 'is_error': False, 'result': 'fixture task advanced'}))
