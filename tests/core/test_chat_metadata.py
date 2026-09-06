import json
from click.testing import CliRunner
from greatminds.cli.main import cli
from test_acp_conversations import setup


def test_chat_metadata_omits_private_messages_and_does_not_start_work(tmp_path):
    store=setup(tmp_path)
    store.enqueue('private prompt contents',request_id='first')
    before=store.path.read_bytes()
    runner=CliRunner();args=['chat','--project-dir',str(tmp_path)]
    bindings=runner.invoke(cli,[*args,'bindings'])
    assert bindings.exit_code==0,bindings.output
    assert json.loads(bindings.output)[0]['role']=='ARCHITECT-PLANNER'
    listed=runner.invoke(cli,[*args,'list'])
    assert listed.exit_code==0,listed.output
    row=json.loads(listed.output)[0]
    assert row['id']==store.id and row['turn_count']==1
    assert 'private prompt contents' not in listed.output
    assert store.path.read_bytes()==before
    assert not (tmp_path/'agent-starts.log').exists()
