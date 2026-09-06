"""ACP frontends launch the supervisor and operator clients, never harness TUIs."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.core.storage import atomic_json, file_lock
from .config import load_execution_config

MARKER = 'Managed by greatminds ACP launch'


def launch(project, *, target, venv=None, recreate=False):
    project = project.resolve()
    schema = load_schema_snapshot()
    load_execution_config(project/'coordination/execution.yaml', roles=set(schema.document['roles']))
    python = str(venv.resolve()/'bin/python') if venv else sys.executable
    if not Path(python).is_file() or not os.access(python, os.X_OK):
        raise GreatMindsError('ACP frontend requires an executable Python environment', exit_code=2)
    if venv:
        result = subprocess.run([python, '-I', '-c', 'from greatminds.runtime.daemon import serve; from greatminds.cli.chat import chat'],
                                capture_output=True, timeout=15)
        if result.returncode:
            raise GreatMindsError('selected environment lacks the ACP CLI/runtime', exit_code=2)
    cli = [python, '-I', '-m', 'greatminds.cli.main']
    if target in {'vscode', 'cursor-ide'}:
        path = project/'greatminds-acp.code-workspace'
        generated = []
        for label, args in (
            ('Daemon', ['coordd','--project-dir',str(project)]),
            ('Run status', ['run','status','--project-dir',str(project)]),
            ('Conversation list', ['chat','--project-dir',str(project),'list']),
        ):
            generated.append({'label':'greatminds ACP: '+label,'type':'process','command':python,
                              'args':cli[1:]+args,'options':{'cwd':str(project)},'detail':MARKER,
                              'problemMatcher':[],'presentation':{'reveal':'always','panel':'dedicated'}})
        with file_lock(project_runtime_dir(project)/'frontend.lock', label='ACP frontend'):
            try:
                document = json.loads(path.read_text()) if path.exists() else {'folders':[{'path':'.'}]}
            except (OSError, ValueError) as exc:
                raise GreatMindsError('cannot read ACP workspace document', exit_code=2) from exc
            if not isinstance(document, dict) or not isinstance(document.get('tasks', {}), dict):
                raise GreatMindsError('invalid ACP workspace document', exit_code=2)
            folders = document.get('folders')
            if (not isinstance(folders, list) or not folders or not isinstance(folders[0], dict)
                    or folders[0].get('path') not in {'.', str(project)}):
                raise GreatMindsError('ACP workspace must have the project root as its first folder', exit_code=2)
            existing = document.get('tasks', {}).get('tasks', [])
            if not isinstance(existing, list) or any(not isinstance(t, dict) for t in existing):
                raise GreatMindsError('invalid workspace tasks', exit_code=2)
            retained = [t for t in existing if t.get('detail') != MARKER]
            if {t.get('label') for t in retained} & {t['label'] for t in generated}:
                raise GreatMindsError('workspace contains a user-owned ACP task label; rename it before generation', exit_code=2)
            document['tasks'] = {**document.get('tasks', {}), 'version':'2.0.0', 'tasks':retained+generated}
            atomic_json(path, document)
        return {'target':target,'workspace':str(path),'tasks':len(generated),
                'chat':'Use New ACP Chat or Attach ACP Chat from the extension command palette.'}
    if target != 'tmux':
        raise GreatMindsError('unsupported ACP frontend', exit_code=2)
    if recreate:
        raise GreatMindsError('ACP launch does not kill sessions; stop coordd and close its tmux session explicitly', exit_code=2)
    session = 'gm-acp-' + hashlib.sha256(str(project).encode()).hexdigest()[:12]
    def tmux(*args):
        try:
            return subprocess.run(['tmux',*args],capture_output=True,text=True,timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GreatMindsError('tmux operation failed; inspect session '+session, exit_code=2) from exc
    if tmux('has-session','-t',session).returncode == 0:
        return {'target':target,'session':session,'status':'existing'}
    result = tmux('new-session','-d','-s',session,'-n','coordd','-c',str(project),
                  shlex.join(cli+['coordd','--project-dir',str(project)]))
    if result.returncode:
        raise GreatMindsError('cannot create ACP tmux session', exit_code=2)
    result = tmux('new-window','-t',session,'-n','operator','-c',str(project))
    if result.returncode:
        raise GreatMindsError('daemon session created but operator window failed; inspect tmux session '+session,exit_code=2)
    return {'target':target,'session':session,'status':'created',
            'chat':'Use greatminds chat create BINDING, then greatminds chat talk CONVERSATION.'}
