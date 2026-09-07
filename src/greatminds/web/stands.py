"""Stand dashboard: existing domain state and project-owned Ansible files."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import re

from greatminds.cli.stand_state import read_stand_state
from greatminds.cli.stand_profile_registry import load_registry
from greatminds.core.errors import GreatMindsError
from greatminds.core.service_environment import read_environment, encode_environment
from greatminds.core.storage import atomic_bytes, file_lock
from greatminds.domain.stand_deployments import DeploymentLedger
from greatminds.runtime.stand_operations import StandOperations


class Stands:
    def __init__(self, service):
        self.service = service
        self.project, self.runtime = service.project, service.runtime
        self.operations = StandOperations(self.project, self.runtime)

    def snapshot(self):
        env = read_environment(self.runtime / 'PROJECT.env')
        connections = {k: v for k, v in env.items() if re.fullmatch(r'STAND_(HOST|USER)(_[A-Za-z0-9_]+)?', k)}
        try:
            registry = load_registry(self.runtime)
            profiles = [asdict(p) for p in registry.profiles.values()]
            error = None
        except GreatMindsError as exc:
            profiles, error = [], str(exc)
        config_files = ['coordination/stand-profiles.yaml']
        config_files += ['coordination/stand-profiles/' + p['file'] for p in profiles]
        config_files += [str(p.relative_to(self.project)) for p in
                         [self.project / 'coordination/ansible.cfg', self.project / '.greatminds/ansible.cfg'] if p.is_file()]
        import configparser
        for config_name in list(config_files):
            if not config_name.endswith('ansible.cfg'):
                continue
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read(self.project / config_name)
                for name in parser.get('defaults', 'inventory', fallback='').split(','):
                    inventory = (self.project / config_name).parent / name.strip()
                    if name.strip() and inventory.is_file() and inventory.resolve().is_relative_to(self.project):
                        config_files.append(str(inventory.relative_to(self.project)))
            except configparser.Error:
                pass
        try:
            from greatminds.runtime.stand_scheduler import StandScheduler
            _, config = self.service.config()
            scheduler = StandScheduler(self.service.store, config.stand).inspect()
        except GreatMindsError as exc:
            scheduler = {'status': 'configuration_error', 'error': str(exc)}
        return {'state': read_stand_state(self.runtime), 'connections': connections,
                'connection_revision': self.connection_revision(), 'profiles': profiles,
                'profile_error': error, 'files': list(dict.fromkeys(config_files)),
                'deployments': DeploymentLedger(self.runtime).snapshot()['attempts'],
                'operations': {k: v for k, v in self.operations.snapshot()['operations'].items() if not v.get('archived')}, 'scheduler': scheduler,
                'environment_path': str(self.runtime / 'PROJECT.env'),
                'ssh_config': str(Path.home() / '.ssh/config')}

    def connection_revision(self):
        p = self.runtime / 'PROJECT.env'
        return hashlib.sha256(p.read_bytes() if p.exists() else b'').hexdigest()

    def save_connections(self, body):
        values = body['connections']
        if not isinstance(values, dict) or len(values) > 64 or any(
            not re.fullmatch(r'STAND_(HOST|USER)(_[A-Za-z0-9_]+)?', k) or
            not isinstance(v, str) or len(v) > 4096 or '\n' in v or '\x00' in v for k, v in values.items()):
            raise GreatMindsError('Use STAND_HOST/STAND_USER variables or named suffixes.', exit_code=2)
        with file_lock(self.runtime / 'setup.lock', label='stand connections'):
            if body['revision'] != self.connection_revision():
                raise GreatMindsError('Connection settings changed. Reload before saving.', exit_code=4)
            env = read_environment(self.runtime / 'PROJECT.env')
            for key in list(env):
                if re.fullmatch(r'STAND_(HOST|USER)(_[A-Za-z0-9_]+)?', key):
                    del env[key]
            env.update(values)
            atomic_bytes(self.runtime / 'PROJECT.env', encode_environment(env).encode())
        return self.snapshot()

    def file(self, name):
        if name not in self.snapshot()['files']:
            raise GreatMindsError('Unknown stand configuration file.', exit_code=2)
        path = (self.project / name).resolve()
        if not path.is_relative_to(self.project) or (path.exists() and path.stat().st_size > 262144):
            raise GreatMindsError('Stand file must be inside the project and at most 256 KiB.', exit_code=2)
        raw = path.read_bytes() if path.exists() else b''
        return {'name': name, 'text': raw.decode(), 'revision': hashlib.sha256(raw).hexdigest()}

    def save_file(self, body):
        if not isinstance(body['text'], str) or len(body['text'].encode()) > 262144:
            raise GreatMindsError('Stand file exceeds 256 KiB.', exit_code=2)
        with file_lock(self.runtime / 'setup.lock', label='stand file'):
            current = self.file(body['name'])
            if body['revision'] != current['revision']:
                raise GreatMindsError('Stand file changed. Reload before saving.', exit_code=4)
            if body['name'].endswith('.yaml'):
                import yaml
                yaml.safe_load(body['text'])
            atomic_bytes(self.project / body['name'], body['text'].encode())
        return self.file(body['name'])

    def deployment_output(self, identity):
        from greatminds.core.storage import safe_name
        from greatminds.runtime.permissions import redact
        import os
        import stat
        identity = safe_name(identity)
        attempt = DeploymentLedger(self.runtime).snapshot()['attempts'].get(identity)
        if not attempt:
            raise GreatMindsError('Unknown deployment.', exit_code=2)
        values = {**os.environ, **read_environment(self.runtime / 'PROJECT.env')}
        secrets = [v for k, v in values.items() if re.search(r'password|secret|token|api.?key', k, re.I)]
        result = {}
        for name, record in attempt.get('output', {}).items():
            path = self.runtime / '.stand/deployment-output' / identity / name
            size = record.get('captured_bytes')
            if (name not in {'stdout', 'stderr'} or path.resolve() != path
                    or str(path) != record.get('path') or type(size) is not int or not 0 <= size <= 67108864):
                raise GreatMindsError('Invalid deployment output artifact.', exit_code=4)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise GreatMindsError('Deployment output is not a regular file.', exit_code=4)
                raw = stream.read(size + 1)
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != record.get('captured_sha256'):
                raise GreatMindsError('Deployment output changed.', exit_code=4)
            text = redact(raw.decode('utf-8', errors='replace'), secrets)
            result[name] = {'text': text[:8192], 'truncated': record.get('truncated', False) or len(text) > 8192}
        return result
