"""Run a real local three-role pipeline using a supplied ACP execution config.

Uses model/provider usage. Operator permissions remain explicit. The input config
is copied into a new temporary Git project; its commands are replaced by the local
unit test command. No deployment, publishing, or global configuration changes.
"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import yaml
from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore

def create_project(execution):
    root=Path(tempfile.mkdtemp(prefix='greatminds-mixed-pipeline-'))
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=root,text=True).strip()
    git('init','-b','main');git('config','user.name','ACP Integration');git('config','user.email','acp-test@example.invalid')
    (root/'.gitignore').write_text('.greatminds/\n.worktrees/\ncoordination/\n__pycache__/\n')
    (root/'clamp.py').write_text('def clamp(value, lower, upper):\n    raise NotImplementedError\n')
    (root/'test_clamp.py').write_text(textwrap.dedent('''\
    import unittest
    from clamp import clamp
    class ClampTests(unittest.TestCase):
        def test_in_range(self): self.assertEqual(clamp(3, 1, 5), 3)
        def test_below(self): self.assertEqual(clamp(-8, -3, 2), -3)
        def test_above(self): self.assertEqual(clamp(9, 1, 5), 5)
        def test_boundary(self): self.assertEqual(clamp(1, 1, 1), 1)
        def test_float(self): self.assertEqual(clamp(0.5, 0.1, 0.8), 0.5)
        def test_invalid_bounds(self):
            with self.assertRaises(ValueError): clamp(2, 5, 1)
    '''))
    git('add','.');git('commit','-m','Seed synthetic clamp task')
    base=git('rev-parse','HEAD')
    config = dict(execution)
    config['max_running'] = 1
    config['commands'] = {'unit-tests': {'argv': [sys.executable, '-m', 'unittest', '-v', 'test_clamp'],
        'roles': ['DEVELOPER', 'TESTER', 'ARCHITECT-REVIEWER'], 'timeout_seconds': 30}}
    expected = {'DEVELOPER', 'TESTER', 'ARCHITECT-REVIEWER'}
    if {item['role'] for item in config['bindings'].values()} != expected or len(config['bindings']) != 3:
        raise ValueError('provide exactly DEVELOPER, TESTER, ARCHITECT-REVIEWER bindings')
    config['bindings'] = {key: {**value, 'workspace': '.', 'scheduling': 'queue'}
                          for key, value in config['bindings'].items()}
    (root/'coordination').mkdir();(root/'coordination/execution.yaml').write_text(yaml.safe_dump(config))
    queue=root/'.greatminds/feature_dev';queue.mkdir(parents=True)
    (queue/'0001-clamp.yaml').write_text(yaml.safe_dump({'id':'0001-clamp','stream':'product','kind':'feature','scope':'backend','reporter':'USER','opened_at':'2026-09-06T16:00:00Z','priority':'normal','title':'Implement and independently validate a local numeric clamp function',
     'description':'Synthetic local integration task. Implement clamp(value, lower, upper): return value clipped to inclusive bounds; raise ValueError when lower > upper. Existing unittest cases define the required behavior. No network, deployment, external services or credentials are needed. DEVELOPER: implement clamp.py, run configured unit-tests, submit handoff to feature_test with an implementation block and artifact clamp.py. TESTER: independently inspect and run configured unit-tests, submit handoff to feature_review with a tests block referencing the command_request_id. Use gate_check_result n/a and empty stand_evidence because the plan explicitly requires no stand; record the actual current UTC gate_check_at and HEAD as gate_check_commit/base_commit, list test_clamp.py, ready_for_review true only on passing evidence. REVIEWER: inspect implementation and tests, run configured unit-tests, and approve to verified only if correct; provide review block outcome approved and current HEAD commit plus command_evidence. Do not commit, merge or move task files: the daemon owns those steps. Keep result JSON outside the source worktree.',
     'blocks':[{'kind':'plan','by':'ARCHITECT-PLANNER','at':'2026-09-06T16:00:00Z','base_commit':base,'assignee_role':'DEVELOPER','stand_required':False,'stand_reason':'Pure local Python function; configured unit tests cover behavior.','plan_kind':'full','mode':'A','ready_for_implementation':True}]}))
    return root, base

async def pipeline(root):
    for stage in range(3):
        state = await serve(root, once=True, interval=.2)
        print(json.dumps({'stage': stage,
            'runs': [{k: r.get(k) for k in ('id', 'role', 'agent_id', 'state', 'reason')}
                     for r in sorted(state['runs'].values(), key=lambda run: run['sequence'])],
            'results': [{k: r.get(k) for k in ('role', 'status', 'details')}
                        for r in state['results'].values()]}), flush=True)
        if (any(r['status'] != 'applied' for r in state['results'].values())
                or any(r['state'] != 'completed' for r in state['runs'].values())):
            break
    verified = (root / '.greatminds/verified/0001-clamp.yaml').exists()
    print(json.dumps({'verified': verified, 'project': str(root)}), flush=True)
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    root, base = create_project(yaml.safe_load(args.config.read_text()))
    print(json.dumps({'project': str(root), 'base_commit': base}), flush=True)
    return 0 if asyncio.run(pipeline(root)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
