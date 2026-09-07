"""Serve the packaged local browser workspace."""
import os
import signal
from pathlib import Path
import click
from greatminds.core.paths import find_project_dir
from greatminds.core.errors import GreatMindsError


@click.command()
@click.option('--project-dir', type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option('--host', default='127.0.0.1', show_default=True)
@click.option('--port', default=8765, type=click.IntRange(0, 65535), show_default=True,
              help='HTTP port; 0 selects a free port.')
@click.option('--daemon/--no-daemon', default=True, show_default=True,
              help='Start coordd if no project daemon is running.')
def web(project_dir, host, port, daemon):
    """Run the local web workspace without application authentication."""
    if os.environ.get('GREATMINDS_RUN_ID') or os.environ.get('GREATMINDS_RUN_TOKEN'):
        raise click.ClickException('web requires an operator context')
    from greatminds.web.service import WebService
    from greatminds.web.server import WebServer
    project = (project_dir or find_project_dir()).resolve()
    service = WebService(project)
    try:
        server = WebServer((host, port), service)
    except OSError as exc:
        raise click.ClickException(f'Cannot listen on {host}:{port}: {exc.strerror}') from exc
    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        if daemon and service.config_path.exists():
            try:
                service.start_daemon()
            except GreatMindsError as exc:
                click.echo(f'Daemon not started: {exc}. Repair settings in the web workspace.', err=True)
        display_host = '127.0.0.1' if host == '0.0.0.0' else host
        click.echo(f'Greatminds: http://{display_host}:{server.server_port}\nProject: {project}')
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous_term)
