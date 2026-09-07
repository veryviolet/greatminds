"""Non-interactive SSH connection probe; no provisioning or remote edits."""
import os
from pathlib import Path
import re
import subprocess
import sys

from greatminds.core.service_environment import read_environment


def main():
    key = sys.argv[1]
    if not re.fullmatch(r'STAND_HOST(?:_[A-Za-z0-9_]+)?', key):
        return 2
    env = read_environment(Path(os.environ['GREATMINDS_PROJECT_DIR']) / '.greatminds/PROJECT.env')
    hosts = [h.strip() for h in env.get(key, '').split(',') if h.strip()]
    if not hosts or len(hosts) > 16:
        print('Configure 1–16 hosts before checking access.')
        return 2
    user = env.get(key.replace('STAND_HOST', 'STAND_USER'), '')
    if user and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', user):
        print('Invalid SSH user.')
        return 2
    for host in hosts:
        if not re.fullmatch(r'[A-Za-z0-9_\[][A-Za-z0-9_.:\]-]*', host):
            print('Use an SSH alias, hostname or address.')
            return 2
        if host in {'localhost', '127.0.0.1', '::1'}:
            print(f'{host}: local execution available')
            continue
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes']
        if user:
            argv += ['-l', user]
        result = subprocess.run([*argv, host, 'true'], timeout=12, check=False)
        print(f'{host}: SSH exit {result.returncode}', flush=True)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
