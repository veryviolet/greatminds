"""Operator client for conversations executed by the project ACP daemon."""
import json
import os
from pathlib import Path
import time
import uuid

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.interactions import ConversationStore


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
@click.pass_obj
def create(context, binding_id):
    """Create a conversation pinned to an existing role binding."""
    schema = load_schema_snapshot()
    config = load_execution_config(context['project']/'coordination/execution.yaml',
                                   roles=set(schema.document['roles']))
    binding = next((b for b in config.bindings if b.id == binding_id), None)
    if binding is None:
        raise GreatMindsError('unknown conversation binding', exit_code=2)
    store = ConversationStore.create(context['runtime'], binding=binding,
        config_sha256=config.sha256, schema_sha256=schema.sha256,
        workspace=binding.workspace_path(context['project']))
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
@click.pass_obj
def attach(context, conversation_id, after, follow):
    """Read output/events without starting, cancelling, or resending a turn."""
    store = ConversationStore(context['runtime'], conversation_id)
    first = True
    try:
        while True:
            page = store.events(after=after)
            if first or page['events']:
                click.echo(json.dumps(page, ensure_ascii=False))
            first = False
            after = page['cursor']
            if page['has_more']:
                continue
            if not follow:
                break
            time.sleep(.2)
    except KeyboardInterrupt:
        return


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
