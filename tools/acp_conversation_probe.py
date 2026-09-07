"""Live public-CLI conversation/restart acceptance in a disposable project.

Uses model usage with the supplied manifest. No permissions are granted. Raw
provider output stays in the temporary runtime; stdout contains only assertions.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile

import yaml

from greatminds.runtime.interactions import ConversationStore
from greatminds.runtime.processes import group_members, process_identity, terminate_group
from greatminds.runtime.store import RunStore


def invoke(project, *argv, timeout=30):
    with subprocess.Popen([sys.executable, '-m', 'greatminds.cli.main', *argv],
                          cwd=project, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.send_signal(signal.SIGINT)
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            raise
        if child.returncode:
            # Keep raw diagnostics private, including possible provider account data.
            (project / 'failed-command.stderr').write_text(stderr)
            raise RuntimeError(f'public command {argv[0]} exited {child.returncode}')
        return stdout


def exited(run):
    identity = run.get('process')
    return bool(identity) and process_identity(identity['pid']) != identity and not group_members(identity)


def probe(args):
    project = Path(tempfile.mkdtemp(prefix='greatminds-conversation-live-'))
    print(json.dumps({'project': str(project)}), flush=True)
    execution = yaml.safe_load(args.config.read_text())
    manifest = execution['agents'][args.agent]
    roles = args.roles or ['ARCHITECT-PLANNER', 'LIVE-DEVELOPER']
    config = {'version': 1, 'max_running': 1, 'agents': {args.agent: manifest},
              'bindings': {f'chat-{index}': {'agent': args.agent, 'role': role,
                  'permission': 'deny', 'timeout_seconds': args.timeout,
                  'workspace': '.', 'scheduling': 'on-demand'}
                  for index, role in enumerate(roles)}}
    (project / 'coordination').mkdir()
    (project / '.greatminds').mkdir()
    (project / 'coordination/execution.yaml').write_text(yaml.safe_dump(config))
    runs = RunStore(project / '.greatminds')
    rows = []
    try:
        for index, role in enumerate(roles):
            chat_args = ('chat', '--project-dir', str(project))
            conversation_id = json.loads(invoke(project, *chat_args, 'create', f'chat-{index}'))['conversation_id']
            store = ConversationStore(project / '.greatminds', conversation_id)
            token = args.token or secrets.token_hex(12)
            first = f'Remember the token {token} for this conversation. Reply with exactly that token and do not use tools.'
            second = 'Return exactly the token from my previous message. Do not use tools or add any other text.'
            invoke(project, *chat_args, 'send', conversation_id, '--request-id', 'first', '--message', first)
            invoke(project, 'coordd', '--project-dir', str(project), '--once', timeout=args.timeout + 45)
            page1 = json.loads(invoke(project, *chat_args, 'attach', conversation_id))
            before = store.path.read_bytes()
            empty = json.loads(invoke(project, *chat_args, 'attach', conversation_id, '--after', str(page1['cursor'])))
            attach_read_only = before == store.path.read_bytes() and not empty['events']
            invoke(project, *chat_args, 'send', conversation_id, '--request-id', 'second', '--message', second)
            # Exact duplicate delivery must not create another model turn.
            invoke(project, *chat_args, 'send', conversation_id, '--request-id', 'second', '--message', second)
            invoke(project, 'coordd', '--project-dir', str(project), '--once', timeout=args.timeout + 45)
            page2 = json.loads(invoke(project, *chat_args, 'attach', conversation_id, '--after', str(page1['cursor'])))
            state = runs.snapshot()
            selected = sorted((r for r in state['runs'].values() if r.get('conversation_id') == conversation_id),
                              key=lambda r: r['sequence'])
            journal = store.snapshot()
            def reply(page, turn):
                return ''.join(event.get('text', '') for event in page['events']
                               if event['kind'] == 'text' and event['turn_id'] == turn).strip()
            row = {'role': role, 'run_states': [r['state'] for r in selected],
                   'first_reply_exact': reply(page1, 'first') == token,
                   'resumed_reply_exact': reply(page2, 'second') == token,
                   'same_session': len(selected) == 2 and bool(selected[0].get('session_id'))
                       and selected[0]['session_id'] == selected[1].get('session_id'),
                   'session_strategies': [r.get('outcome', {}).get('session_strategy') for r in selected],
                   'attach_read_only': attach_read_only,
                   'cursor_excludes_previous_turn': all(e['turn_id'] != 'first' for e in page2['events']),
                   'exactly_two_turns': len(journal['turns']) == 2,
                   'turns_completed': all(t['status'] == 'completed' for t in journal['turns'].values()),
                   'process_groups_exited': all(exited(r) for r in selected),
                   'no_domain_results': not state['results']}
            invoke(project, 'coordd', '--project-dir', str(project), '--once', timeout=args.timeout + 45)
            # Startup observations may change; authoritative work identities must not.
            restarted = runs.snapshot()
            row['idle_restart_no_work'] = all(restarted.get(key, {}) == state.get(key, {})
                                              for key in ('runs', 'results', 'commands'))
            row['passed'] = (row['run_states'] == ['completed', 'completed']
                and row['session_strategies'] == ['new_context', 'loaded']
                and all(row[key] for key in ('first_reply_exact', 'resumed_reply_exact', 'same_session',
                    'attach_read_only', 'cursor_excludes_previous_turn', 'exactly_two_turns',
                    'turns_completed', 'process_groups_exited', 'no_domain_results', 'idle_restart_no_work')))
            rows.append(row)
            print(json.dumps(row), flush=True)
        return 0 if all(row['passed'] for row in rows) else 1
    finally:
        # A failed outer command must not leave a model process behind.
        for run in runs.snapshot()['runs'].values():
            if run.get('process'):
                asyncio.run(terminate_group(run['process']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--agent', required=True)
    parser.add_argument('--role', dest='roles', action='append')
    parser.add_argument('--token', help='Override the random recall token for deterministic fixture testing')
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args()
    if not 5 <= args.timeout <= 600:
        parser.error('timeout must be 5..600 seconds')
    return probe(args)


if __name__ == '__main__':
    raise SystemExit(main())
