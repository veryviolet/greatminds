"""Run with a wheel-installed Python to verify setup and daemon-only resume.

Example (from any working directory):
    /tmp/wheel-venv/bin/python /path/to/repo/tests/smoke/installed_dependency_resume.py

Creates a synthetic project in the system temporary directory and prints its
path. No provider process, user service, or real project is used.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml
import greatminds

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--without-stands', action='store_true',
                    help='also verify a minimal install and the optional-dependency error')
options = parser.parse_args()
if options.without_stands:
    assert importlib.util.find_spec('ansible') is None
    assert importlib.util.find_spec('inotify_simple') is None
    from greatminds.cli.stand_executor import _ansible_playbook_path
    from greatminds.core.errors import GreatMindsError
    saved_path = os.environ.get('PATH')
    try:
        os.environ['PATH'] = ''
        try:
            _ansible_playbook_path()
        except GreatMindsError as exc:
            assert 'greatminds[stands]' in str(exc)
        else:
            raise AssertionError('minimal install unexpectedly resolved Ansible')
    finally:
        if saved_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = saved_path

project = Path(tempfile.mkdtemp(prefix='greatminds-dependency-wheel-project-'))
cli = [sys.executable, "-m", "greatminds.cli.main"]
env = {k: v for k, v in os.environ.items() if not k.startswith('GREATMINDS_')}
def run(*args):
    result = subprocess.run([*cli, *args], cwd=project, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout
assert Path(greatminds.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), (
    "Run this smoke check with the wheel installed in an isolated environment, not an editable checkout")
run('setup')
contract = project / 'coordination/execution.yaml'
original = contract.read_bytes()
run('setup')
assert contract.read_bytes() == original
runtime = project / '.greatminds'
def task(task_id, blocks):
    return {'id': task_id, 'title': 'Wheel smoke', 'stream': 'product', 'kind': 'research',
            'scope': 'backend', 'reporter': 'USER', 'opened_at': '2026-09-06T10:00:00Z',
            'priority': 'normal', 'blocks': blocks}
blocks = [{'kind': 'plan', 'by': 'ARCHITECT-PLANNER', 'at': '2026-09-06T10:00:00Z',
           'base_commit': 'fixture', 'assignee_role': 'DEVELOPER', 'stand_required': False,
           'plan_kind': 'full', 'mode': 'A', 'ready_for_implementation': True},
          {'kind': 'blocked', 'by': 'DEVELOPER', 'at': '2026-09-06T10:00:00Z',
           'reason': 'waiting for prerequisite', 'dependencies': ['verified/0002-dep.yaml'],
           'resume_to': 'feature_dev'}]
source = runtime / 'feature_blocked/0001-waiter.yaml'
source.write_text(yaml.safe_dump(task('0001-waiter', blocks)))
(runtime / 'verified/0002-dep.yaml').write_text(yaml.safe_dump(task('0002-dep', [])))
assert json.loads(run('wake-check', '--json'))['tasks']['0001-waiter']['status'] == 'ready'
assert '0001-waiter: ready' in run('wake-check')
run('coordd', '--once')
assert not source.exists()
assert (runtime / 'feature_dev/0001-waiter.yaml').exists()
state = json.loads(run('run', 'status'))
assert state['runs'] == {}
run('coordd', '--once')
journal = [json.loads(line) for line in (runtime / 'journal.ndjson').read_text().splitlines()]
assert len(journal) == 1 and journal[0]['actor'] == 'SYSTEM'
assert 'All clear.' in run('watchdog')
print(json.dumps({'installed_module': greatminds.__file__, 'project': str(project),
                  'setup_idempotent': True, 'system_resumes': len(journal), 'agent_runs': len(state['runs'])}))
