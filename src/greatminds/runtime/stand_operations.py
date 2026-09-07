"""Durable operator requests consumed by coordd, independent of the web server."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import time

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_json, file_lock, safe_name
from greatminds.runtime.processes import process_identity, terminate_group


def arguments(body):
    action = body.get('action')
    fields = {'check': ('machine_key',), 'doctor': (), 'deploy': ('lease_id',), 'up': ('reason',), 'down': ('reason',),
              'lease': ('task_id', 'profile', 'role'), 'release': ('lease_id', 'result', 'role'),
              'reclaim': ('lease_id',), 'deployment-recover': ('attempt_id',),
              'deployment-resolve': ('attempt_id', 'reason')}
    if action not in fields:
        raise GreatMindsError('Unknown stand operation.', exit_code=2)
    for key in fields[action]:
        if not isinstance(body.get(key), str) or not body[key].strip() or len(body[key]) > 2000 or '\x00' in body[key]:
            raise GreatMindsError(f'Stand operation requires {key}.', exit_code=2)
    argv = ['stand', 'profiles', 'doctor'] if action == 'doctor' else ['stand', action]
    for key in fields[action]:
        if key == 'role':
            if body[key] not in {'EXPLORER','TESTER','DEVELOPER','ARCHITECT-PLANNER','MAINTAINER','ARCHITECT-REVIEWER','LIVE-DEVELOPER'}:
                raise GreatMindsError('Invalid stand holder role.', exit_code=2)
        elif key == 'attempt_id':
            argv.append(safe_name(body[key]))
        else:
            argv += ['--' + key.replace('_', '-'), body[key]]
    if action == 'deploy':
        argv += ['--timeout', '900', '--output-limit', '262144']
    return argv


class StandOperations:
    def __init__(self, project, runtime):
        self.project, self.runtime = Path(project), Path(runtime)
        self.path = self.runtime / '.stand/operations.json'
        self.lock = self.runtime / '.stand/operations.lock'
        self.future = None
        self.recovered = False

    def snapshot(self):
        if not self.path.exists():
            return {'version': 1, 'operations': {}}
        doc = json.loads(self.path.read_text())
        if doc.get('version') != 1 or not isinstance(doc.get('operations'), dict):
            raise GreatMindsError('Invalid stand operation journal.', exit_code=4)
        return doc

    def submit(self, body):
        arguments(body)
        identity = safe_name(body['request_id'])
        payload = {k: v for k, v in body.items() if k != 'request_id'}
        with file_lock(self.lock, label='stand operations'):
            doc = self.snapshot()
            old = doc['operations'].get(identity)
            if old:
                if old['request'] != payload:
                    raise GreatMindsError('Request ID already has another operation.', exit_code=4)
                return old
            if sum(o['status'] == 'queued' for o in doc['operations'].values()) >= 32:
                raise GreatMindsError('Stand operation queue is full.', exit_code=4)
            item = {'id': identity, 'request': payload, 'status': 'queued', 'at': time.time(), 'log': ''}
            doc['operations'][identity] = item
            # Keep pending/uncertain work and the newest 100 completed receipts.
            completed = [o for o in doc['operations'].values() if o['status'] in {'completed', 'failed'}]
            for o in sorted(completed, key=lambda o: o['at'])[:-100]:
                o.update(log='', archived=True)
            atomic_json(self.path, doc)
            return item

    def update(self, identity, **values):
        with file_lock(self.lock, label='stand operations'):
            doc = self.snapshot()
            doc['operations'][identity].update(values)
            atomic_json(self.path, doc)

    async def poll(self):
        if not self.recovered:
            for item in self.snapshot()['operations'].values():
                if item['status'] == 'running':
                    if item.get('process'):
                        await terminate_group(item['process'])
                    self.update(item['id'], status='needs_review', log='Daemon restarted during operation. Inspect the stand and deployment ledger before retrying.')
            self.recovered = True
        if self.future:
            if not self.future.done():
                return
            self.future.result()
            self.future = None
        queued = [o for o in self.snapshot()['operations'].values() if o['status'] == 'queued']
        if queued:
            item = min(queued, key=lambda o: o['at'])
            self.update(item['id'], status='running')
            self.future = asyncio.create_task(self.execute(item))

    async def execute(self, item):
        identity = item['id']
        env = {k: v for k, v in os.environ.items() if not k.startswith('GREATMINDS_RUN_')}
        env.update(GREATMINDS_PROJECT_DIR=str(self.project),
                   GREATMINDS_ROLE=item['request'].get('role', 'MAINTAINER'))
        gate_read, gate_write = os.pipe()
        process = None
        tracked = None
        guard = contextlib.ExitStack()
        try:
            if item['request']['action'] in {'up', 'down'}:
                guard.enter_context(file_lock(self.runtime / '.stand/deployment.lock', label='stand availability', timeout=0))
                from greatminds.domain.stand_deployments import DeploymentLedger
                DeploymentLedger(self.runtime).require_resolved()
            process = await asyncio.create_subprocess_exec(sys.executable,
                str(Path(__file__).with_name('agent_exec.py')), str(gate_read),
                sys.executable, '-m',
                *(['greatminds.runtime.stand_check', item['request']['machine_key']]
                  if item['request']['action'] == 'check' else ['greatminds.cli.main', *arguments(item['request'])]),
                cwd=self.project, env=env, pass_fds=(gate_read,), start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            tracked = process_identity(process.pid)
            if not tracked:
                raise RuntimeError('Cannot track stand operation process')
            self.update(identity, process=tracked)
            os.write(gate_write, b'1')
            output = bytearray()
            async with asyncio.timeout(930):
                while chunk := await process.stdout.read(8192):
                    output.extend(chunk)
                    del output[:-262144]
                waiter = asyncio.create_task(process.wait())
                try:
                    while not waiter.done():
                        await asyncio.wait({waiter}, timeout=0.1)
                    rc = waiter.result()
                finally:
                    if not waiter.done():
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
            # CLI diagnostics may contain project paths, but never retain raw
            # environment values returned by tools. Apply shared redaction.
            import re
            from greatminds.runtime.permissions import redact
            from greatminds.core.service_environment import read_environment
            values = {**env, **read_environment(self.runtime / 'PROJECT.env')}
            secrets = [v for k, v in values.items() if re.search(r'password|secret|token|api.?key', k, re.I)]
            log = redact(output.decode('utf-8', errors='replace'), secrets)
            self.update(identity, status='completed' if rc == 0 else 'failed', exit_code=rc, log=log)
        except (Exception, asyncio.CancelledError) as exc:
            self.update(identity, status='needs_review', log=f'Operation interrupted ({type(exc).__name__}). Inspect state before retrying.')
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            os.close(gate_read)
            os.close(gate_write)
            if tracked:
                await terminate_group(tracked)
            if process:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 3)
            guard.close()

    async def close(self):
        if self.future:
            self.future.cancel()
            await asyncio.gather(self.future, return_exceptions=True)
