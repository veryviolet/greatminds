import json
import shlex
from subprocess import CompletedProcess

import pytest
from click.testing import CliRunner
from greatminds.cli.main import cli
from greatminds.core.errors import GreatMindsError
from greatminds.runtime.frontends import launch
from test_acp_bootstrap import manifest
from greatminds.runtime.bootstrap import bootstrap


def project(root):
    path=root/'project with spaces';bootstrap(path,manifest(root));return path


@pytest.mark.parametrize('target',['vscode','cursor-ide'])
def test_acp_launch_uses_process_tasks_and_preserves_unrelated_settings(tmp_path,target):
    root=project(tmp_path)
    legacy=root/'.vscode/tasks.json';legacy.parent.mkdir();legacy.write_text('user-owned file')
    path=root/'greatminds-acp.code-workspace'
    path.write_text(json.dumps({'folders':[{'path':'.'}],'settings':{'editor.fontSize':16},
        'tasks':{'version':'2.0.0','tasks':[{'label':'user check','type':'process','command':'custom'}]}}))
    result=CliRunner().invoke(cli,['launch','--project-dir',str(root),'--target',target])
    assert result.exit_code==0,result.output
    document=json.loads(path.read_text())
    assert document['settings']=={'editor.fontSize':16}
    assert document['tasks']['tasks'][0]['command']=='custom'
    generated=document['tasks']['tasks'][1:]
    assert len(generated)==3 and all(t['type']=='process' for t in generated)
    assert all(str(root) in t['args'] for t in generated)
    assert not any(word in json.dumps(generated) for word in ['start-agent','claude','codex','send-keys'])
    before=path.read_bytes();launch(root,target=target);assert path.read_bytes()==before
    assert legacy.read_text()=='user-owned file'
    assert not (root/'.greatminds/.runtime/state.json').exists()


def test_tmux_launch_only_starts_coordd_and_operator_shell_without_keystrokes(tmp_path,monkeypatch):
    root=project(tmp_path);calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        return CompletedProcess(argv,1 if argv[1]=='has-session' else 0,'','')
    monkeypatch.setattr('greatminds.runtime.frontends.subprocess.run',run)
    result=launch(root,target='tmux')
    assert result['status']=='created'
    assert [c[1] for c in calls if c[0]=='tmux']==['has-session','new-session','new-window']
    command=calls[1]
    assert command[-4:]==['daemon','start','--project-dir',str(root)]
    assert not any('start-agent' in str(c) or 'send-keys' in str(c) for c in calls)
    calls.clear()
    monkeypatch.setattr('greatminds.runtime.frontends.subprocess.run',lambda argv,**kw:CompletedProcess(argv,0,'',''))
    assert launch(root,target='tmux')['status']=='existing'


def test_acp_launch_rejects_destructive_recreate_and_user_task_collisions(tmp_path):
    root=project(tmp_path)
    with pytest.raises(GreatMindsError,match='does not kill'):
        launch(root,target='tmux',recreate=True)
    path=root/'greatminds-acp.code-workspace'
    path.write_text(json.dumps({'folders':[{'path':'.'}],'tasks':{'tasks':[{'label':'greatminds ACP: Daemon','command':'user'}]}}))
    before=path.read_bytes()
    with pytest.raises(GreatMindsError,match='user-owned'):
        launch(root,target='vscode')
    assert path.read_bytes()==before


def test_coord_yaml_from_another_directory_cannot_bypass_acp_launch(tmp_path,monkeypatch):
    import yaml
    root=project(tmp_path)
    config=root/'coordination/coord.yaml'
    config.write_text(yaml.safe_dump({'project_dir':str(root),'windows':[]}))
    monkeypatch.chdir(tmp_path)
    result=CliRunner().invoke(cli,['launch','--config',str(config),'--target','vscode'])
    assert result.exit_code!=0 and 'No such option' in result.output and '--config' in result.output
    assert not (root/'.vscode/tasks.json').exists()
