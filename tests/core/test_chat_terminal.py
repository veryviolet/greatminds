import asyncio
import sys

import pytest
from click.testing import CliRunner

from greatminds.cli.main import cli
from greatminds.cli.chat import terminal_text
from greatminds.runtime.daemon import serve
from greatminds.runtime.store import RunStore
from test_acp_conversations import setup


def test_readable_attach_escapes_controls_and_leaves_journal_untouched(tmp_path):
    store=setup(tmp_path)
    store.acquire('owner',config_sha256=store.snapshot()['config_sha256'],schema_sha256=store.snapshot()['schema_sha256'])
    store.enqueue('user\x1b[2J',request_id='one');store.claim_next('owner')
    store.append_text('owner','one','answer\x1b]52;c;secret\x07\r\u202e')
    store.finish('owner','one',status='completed',reason='end_turn')
    before=store.path.read_bytes()
    result=CliRunner().invoke(cli,['chat','--project-dir',str(tmp_path),'attach',store.id,'--text'])
    assert result.exit_code==0,result.output
    assert 'User [one]' in result.output and 'Assistant [one]' in result.output
    assert '\\u001b' in result.output and '\\u202e' in result.output
    assert '\x1b' not in result.output and '\x07' not in result.output and '\r' not in result.output
    assert 'Cursor:' in result.output
    assert store.path.read_bytes()==before
    assert terminal_text('\x1b')+terminal_text('[2J')=='\\u001b[2J'


def test_detach_during_output_does_not_cancel_or_resend(tmp_path,monkeypatch):
    store=setup(tmp_path)
    store.acquire('owner',config_sha256=store.snapshot()['config_sha256'],schema_sha256=store.snapshot()['schema_sha256'])
    store.enqueue('work',request_id='one');store.claim_next('owner')
    before=store.path.read_bytes()
    def detach(_):
        raise KeyboardInterrupt
    monkeypatch.setattr('greatminds.cli.chat.time.sleep',detach)
    result=CliRunner().invoke(cli,['chat','--project-dir',str(tmp_path),'talk',store.id])
    assert result.exit_code==0,result.output
    assert 'Reconnect with chat talk' in result.output
    assert store.path.read_bytes()==before


@pytest.mark.parametrize('scenario',['echo','permission-execute'])
def test_terminal_client_exchanges_messages_with_real_daemon_process_transport(tmp_path,scenario):
    store=setup(tmp_path,scenario=scenario)
    async def check():
        daemon=asyncio.create_task(serve(tmp_path,interval=.2,environment={}))
        client=None
        try:
            client=await asyncio.create_subprocess_exec(sys.executable,'-m','greatminds.cli.main',
                'chat','--project-dir',str(tmp_path),'talk',store.id,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            async with asyncio.timeout(15):
                stdout,stderr=await client.communicate(b'hello\n'+(b'2\n' if scenario=='permission-execute' else b'')+b'/detach\n')
            assert client.returncode==0,(stdout,stderr)
            assert b'Assistant [' in stdout
            assert b'Reconnect with chat talk' in stdout
            assert len(store.snapshot()['turns'])==1
            assert next(iter(store.snapshot()['turns'].values()))['status']=='completed'
            assert not store.snapshot()['closed']
            if scenario=='permission-execute':
                request=next(iter(RunStore(tmp_path/'.greatminds').snapshot()['permissions'].values()))
                assert request['status']=='consumed' and request['option_id']=='no'
        finally:
            if client is not None and client.returncode is None:
                client.kill();await client.wait()
            daemon.cancel();await asyncio.gather(daemon,return_exceptions=True)
    asyncio.run(check())
