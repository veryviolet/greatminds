"""Read-only terminal dashboard over durable ACP run and assignment state."""
import json
import sys
import time
from pathlib import Path

import click

from greatminds.core.paths import find_project_dir
from greatminds.runtime.observation import snapshot
from greatminds.cli.chat import terminal_text


def render_dashboard(state):
    lines = ['GREATMINDS ACP | dispatch ' + ('paused' if state['paused'] else 'enabled'), '', 'BINDINGS / RUNS']
    for row in state['agents']:
        lines.append(f"{row['role']} [{row['binding_id']}] {row['agent_id']} | {row['state']} | "
                     f"task={row['task_id'] or '-'} run={row['run_id'] or '-'} | {row['reason']}")
        if row['run_id'] and not row['config_current']:
            lines.append('  pinned configuration differs from current configuration')
        if row['state'] == 'running' and row['alive'] is not True:
            lines.append('  process liveness is absent or unknown; daemon reconciliation required')
    if not state['agents']:
        lines.append('(no ACP bindings configured)')
    lines += ['', 'ASSIGNMENTS']
    for row in state['assignments']:
        lines.append(f"{row['task_id']} -> {row['binding']} | {row['reason']}")
    lines += ['', 'TASK QUEUES']
    for row in state['tasks']:
        lines.append(f"{row['id']} | {row['queue']} | {row.get('error') or row.get('title', '')}")
    lines += ['', 'STAND', json.dumps(state['stand_lease'], ensure_ascii=False),
              '', 'MAINTENANCE', json.dumps(state['maintenance_findings'], ensure_ascii=False)]
    return terminal_text('\n'.join(lines)) + '\n'


@click.command(name="dashboard", short_help="show ACP runs, assignments and holds")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--interval", type=click.FloatRange(min=.2), default=2.0)
@click.option("--once", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def dashboard(project_dir, interval, once, as_json):
    project = project_dir.resolve() if project_dir else find_project_dir()
    interactive = sys.stdout.isatty() and not once and not as_json
    try:
        while True:
            state = snapshot(project)
            if interactive:
                click.echo('\x1b[2J\x1b[H', nl=False)
            click.echo(json.dumps(state, ensure_ascii=False, indent=2) if as_json else render_dashboard(state), nl=as_json)
            if once or as_json:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
