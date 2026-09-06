"""Operator client for conversations executed by the project ACP daemon."""
import json
import os
from pathlib import Path
import time
import uuid
import unicodedata

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.interactions import ConversationStore


def terminal_text(value):
    """Display provider text without executing terminal controls, even across chunks."""
    return ''.join(c if c in '\n\t' or unicodedata.category(c) not in {'Cc', 'Cf'}
                   else f'\\u{ord(c):04x}' for c in str(value))


def render_events(page, turns):
    for event in page['events']:
        kind, turn_id = event['kind'], event['turn_id']
        if kind == 'text':
            click.echo(terminal_text(event['text']), nl=False)
        elif kind == 'queued':
            click.echo('\nUser [' + terminal_text(turn_id) + ']:')
            click.echo(terminal_text(turns[turn_id]['prompt']))
        elif kind == 'started':
            click.echo('\nAssistant [' + terminal_text(turn_id) + ']:')
        else:
            details = event.get('reason') or event.get('status') or ''
            click.echo('\n[' + terminal_text(kind) + (' ' + terminal_text(details) if details else '') + ']')


def answer_pending_permissions(runtime, conversation_id, seen):
    from greatminds.runtime.store import RunStore
    from greatminds.runtime.permissions import PermissionService
    runs = RunStore(runtime)
    state = runs.snapshot()
    service = PermissionService(runs)
    for request in state.get('permissions', {}).values():
        run = state['runs'].get(request['run_id'], {})
        if (run.get('conversation_id') != conversation_id or request['status'] != 'pending'
                or request['id'] in seen):
            continue
        seen.add(request['id'])
        options = [o for o in request['options'] if o['kind'] in {'allow_once', 'reject_once'}]
        click.echo('\nPermission: ' + terminal_text(request['tool'].get('title', request['id'])))
        click.echo(terminal_text(json.dumps(request['tool'], ensure_ascii=False, indent=2)))
        if not options:
            click.echo('No supported one-time options; inspect this request with run permission.')
            continue
        for index, option in enumerate(options, 1):
            click.echo(f"  {index}. {terminal_text(option.get('name', option['optionId']))} ({option['kind']})")
        selected = click.prompt('Choose an option', type=click.Choice([str(i) for i in range(1, len(options)+1)]))
        try:
            service.answer(request['id'], options[int(selected)-1]['optionId'])
        except GreatMindsError:
            click.echo('Permission is no longer answerable; inspect its current status with run permission.')


@click.group()
@click.option('--project-dir', type=click.Path(file_okay=False, path_type=Path))
@click.pass_context
def chat(ctx, project_dir):
    """Queue user messages and attach to daemon-owned ACP conversations."""
    if os.environ.get('GREATMINDS_RUN_ID') or os.environ.get('GREATMINDS_RUN_TOKEN'):
        raise GreatMindsError('chat controls require an operator context', exit_code=3)
    project = (project_dir or find_project_dir()).resolve()
    ctx.obj = {'project': project, 'runtime': project_runtime_dir(project)}


@chat.command('create')
@click.argument('binding_id')
@click.option('--task', 'task_id', help='Pin an exact workflow task ID and its current revision.')
@click.pass_obj
def create(context, binding_id, task_id):
    """Create a conversation pinned to an existing role binding."""
    schema = load_schema_snapshot()
    config = load_execution_config(context['project']/'coordination/execution.yaml',
                                   roles=set(schema.document['roles']))
    binding = next((b for b in config.bindings if b.id == binding_id), None)
    if binding is None:
        raise GreatMindsError('unknown conversation binding', exit_code=2)
    task = None
    if task_id is not None:
        from greatminds.core.storage import safe_name
        from greatminds.runtime.store import TaskRevision
        paths = list(context['runtime'].glob(f'*/{safe_name(task_id)}.yaml'))
        if len(paths) != 1:
            raise GreatMindsError('task ID must identify exactly one workflow file', exit_code=2)
        task = TaskRevision.capture(context['runtime'], paths[0])
        if task.path.split('/')[0] not in schema.document['roles'][binding.role].get('claims_from', []):
            raise GreatMindsError('binding role cannot claim this task queue', exit_code=3)
    store = ConversationStore.create(context['runtime'], binding=binding,
        config_sha256=config.sha256, schema_sha256=schema.sha256,
        workspace=binding.workspace_path(context['project']), task=task)
    click.echo(json.dumps({'conversation_id': store.id, 'binding_id': binding.id}))


@chat.command('send')
@click.argument('conversation_id')
@click.option('--message', required=True)
@click.option('--request-id', help='Reuse this ID when retrying the same delivery.')
@click.pass_obj
def send(context, conversation_id, message, request_id):
    """Queue one message; the running coordd process executes it."""
    store = ConversationStore(context['runtime'], conversation_id)
    turn = store.enqueue(message, request_id=request_id or uuid.uuid4().hex)
    click.echo(json.dumps({'conversation_id': store.id, 'request_id': turn['id'], 'status': turn['status']}))


@chat.command('attach')
@click.argument('conversation_id')
@click.option('--after', default=0, type=click.IntRange(min=0), help='Last received event cursor.')
@click.option('--follow', is_flag=True, help='Stream JSON pages until Ctrl-C; detaching leaves the turn running.')
@click.option('--text', 'text_mode', is_flag=True, help='Render readable messages instead of JSON pages.')
@click.pass_obj
def attach(context, conversation_id, after, follow, text_mode):
    """Read output/events without starting, cancelling, or resending a turn."""
    store = ConversationStore(context['runtime'], conversation_id)
    first = True
    try:
        while True:
            page = store.events(after=after)
            if first or page['events']:
                if text_mode:
                    render_events(page, store.snapshot()['turns'])
                else:
                    click.echo(json.dumps(page, ensure_ascii=False))
            first = False
            after = page['cursor']
            if page['has_more']:
                continue
            if not follow or page['closed']:
                break
            time.sleep(.2)
    except KeyboardInterrupt:
        return
    finally:
        if text_mode:
            click.echo(f'\n[Cursor: {after}]')


@chat.command('talk')
@click.argument('conversation_id')
@click.option('--after', default=0, type=click.IntRange(min=0))
@click.pass_obj
def talk(context, conversation_id, after):
    """Read responses, send messages, and answer one-time permissions in a terminal."""
    store = ConversationStore(context['runtime'], conversation_id)
    seen = set()
    click.echo('Chat requires running coordd. /detach exits; /close cancels and closes. Ctrl-C detaches.')
    try:
        while True:
            page = store.events(after=after)
            document = store.snapshot()
            render_events(page, document['turns'])
            after = page['cursor']
            if page['has_more']:
                continue
            if document['closed'] or document.get('close_requested'):
                break
            answer_pending_permissions(context['runtime'], conversation_id, seen)
            if any(t['status'] in {'queued', 'running'} for t in document['turns'].values()):
                time.sleep(.1)
                continue
            message = click.prompt('\nYou', default='', show_default=False)
            if message == '/detach':
                break
            if message == '/close':
                store.request_close()
                click.echo('Closure requested; coordd will release the session.')
                break
            if message.strip():
                request_id = uuid.uuid4().hex
                click.echo(f'[Request: {request_id}]')
                store.enqueue(message, request_id=request_id)
    except (KeyboardInterrupt, click.Abort):
        pass
    finally:
        click.echo(f'\n[Detached. Reconnect with chat talk {store.id} --after {after}]')


@chat.command('interrupt')
@click.argument('conversation_id')
@click.argument('request_id')
@click.pass_obj
def interrupt(context, conversation_id, request_id):
    """Cancel a queued message or request cancellation of its active ACP turn."""
    store = ConversationStore(context['runtime'], conversation_id)
    store.cancel(request_id)
    turn = store.snapshot()['turns'][request_id]
    click.echo(json.dumps({'request_id': request_id, 'status': turn['status'],
                          'cancel_requested': turn['cancel_requested']}))


@chat.command('close')
@click.argument('conversation_id')
@click.pass_obj
def close(context, conversation_id):
    """Stop admission and ask the daemon to cancel work and release the session."""
    store = ConversationStore(context['runtime'], conversation_id)
    store.request_close()
    click.echo(json.dumps({'conversation_id': store.id, 'closed': store.snapshot()['closed'],
                          'close_requested': True}))
