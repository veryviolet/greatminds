"""Project daemon lifecycle shared by CLI and browser; no service manager required."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_runtime_dir
from greatminds.core.storage import atomic_json, file_lock
from .processes import process_identity, signal_member


def directory(project):
    return project_runtime_dir(Path(project).resolve()) / '.runtime'


def publish_presence(project, revision, state):
    project = Path(project).resolve()
    record = {'project': str(project), 'process': process_identity(os.getpid()),
              'revision': revision, 'state': state, 'heartbeat': time.monotonic()}
    atomic_json(directory(project) / 'daemon.json', record)


@asynccontextmanager
async def presence(project, *, revision):
    """Publish only while holding the supervisor lease, after recovery succeeds."""
    project = Path(project).resolve()
    path = directory(project) / 'daemon.json'
    record = {'project': str(project), 'process': process_identity(os.getpid()),
              'revision': revision}
    def publish(state):
        atomic_json(path, record | {'state': state, 'heartbeat': time.monotonic()})
    publish('ready')
    async def heartbeat():
        while True:
            await asyncio.sleep(1)
            publish('ready')
    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        publish('stopping')


def status(project):
    project = Path(project).resolve()
    root = directory(project)
    locked = False
    holder = None
    try:
        with (root / 'supervisor.lock').open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
            except BlockingIOError:
                locked = True
                holder = stream.read(64).decode('ascii', errors='replace').strip()
    except FileNotFoundError:
        pass
    try:
        record = json.loads((root / 'daemon.json').read_text())
        identity = record['process']
        valid = (record['project'] == str(project) and isinstance(identity, dict)
                 and type(identity['pid']) is int and identity['pid'] > 0
                 and holder == str(identity['pid'])
                 and process_identity(identity['pid']) == identity)
        responsive = valid and 0 <= time.monotonic() - record['heartbeat'] < 10
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        record, identity, valid, responsive = {}, None, False, False
    config = project / 'coordination/execution.yaml'
    revision = hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
    return {'running': locked, 'managed': locked and valid,
            'responsive': locked and responsive,
            'state': (record.get('state', 'unknown') if valid else 'unknown') if locked else 'stopped',
            'starting': locked and (not valid or record.get('state') == 'starting'),
            'process': identity if locked and valid else None,
            'restart_required': bool(locked and valid and record.get('revision') != revision),
            'log': str(root / 'daemon.log')}


def _start(project, timeout, interval=1, project_name=None):
    current = status(project)
    if current['running']:
        return current
    from greatminds.runtime.observation import configuration
    from greatminds.cli.daemon import execution_environment
    configuration(project)
    execution_environment(project, project_name)  # Fail before detaching on invalid environment.
    child = subprocess.Popen([sys.executable, '-I', '-m', __name__, str(project), str(interval), project_name or ''],
                             cwd=project, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status(project)
        if current['managed'] and current['responsive'] and current['state'] == 'ready':
            return current
        if child.poll() is not None:
            raise GreatMindsError(f'Daemon startup failed (exit {child.returncode}); inspect {current["log"]}', exit_code=4)
        time.sleep(.05)
    raise GreatMindsError(f'Daemon startup timed out; inspect status and {current["log"]} before retrying.', exit_code=4)


def _stop(project, timeout, force=False):
    current = status(project)
    if not current['running']:
        return current
    if not current['managed']:
        raise GreatMindsError('Daemon identity is unavailable; refusing to signal an unknown process.', exit_code=4)
    identity = current['process']
    signal_member(identity, signal.SIGKILL if force else signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status(project)
        if not current['running'] and process_identity(identity['pid']) != identity:
            return current
        if current['process'] and current['process'] != identity:
            raise GreatMindsError('Daemon changed during stop; inspect its status.', exit_code=4)
        time.sleep(.05)
    raise GreatMindsError('Daemon is still stopping; inspect status and logs. Force stop must be explicit.', exit_code=4)


def control(project, action, *, timeout=30, force=False, interval=1, project_name=None):
    project = Path(project).resolve()
    if action == 'status':
        return status(project)
    if action not in {'start', 'stop', 'restart'}:
        raise ValueError('unknown daemon action')
    if os.environ.get('GREATMINDS_RUN_ID') or os.environ.get('GREATMINDS_RUN_TOKEN'):
        raise GreatMindsError('Daemon control requires an operator context.', exit_code=2)
    if action in {'start', 'restart'}:
        from greatminds.runtime.observation import configuration
        from greatminds.cli.daemon import execution_environment
        configuration(project)
        execution_environment(project, project_name)
    with file_lock(directory(project) / 'daemon-control.lock', label='daemon control', timeout=timeout):
        if action in {'stop', 'restart'}:
            result = _stop(project, timeout, force)
            if action == 'stop':
                return result
        return _start(project, timeout, interval, project_name)


class LogStream:
    def __init__(self, logger):
        self.logger = logger
    def write(self, text):
        # Bound a single write as well as total retained log files.
        for offset in range(0, len(text), 8192):
            chunk = text[offset:offset + 8192].rstrip()
            if chunk:
                self.logger.info(chunk)
        return len(text)
    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()


def main():
    project = Path(sys.argv[1]).resolve()
    os.umask(0o077)
    root = directory(project)
    root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(root / 'daemon.log', maxBytes=2 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger('greatminds.daemon')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    sys.stdout = sys.stderr = LogStream(logger)
    logging.getLogger().addHandler(handler)
    from greatminds.cli.coordd import coordd
    logger.info('Starting daemon for %s', project)
    try:
        args = ['--project-dir', str(project), '--foreground', '--interval-sec', sys.argv[2]]
        if sys.argv[3]:
            args += ['--project', sys.argv[3]]
        coordd.main(args=args, standalone_mode=False)
    except BaseException:
        logger.exception('Daemon exited with an error')
        raise
    finally:
        logger.info('Daemon stopped')


if __name__ == '__main__':
    main()
