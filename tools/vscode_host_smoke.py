"""Run the real Linux VS Code extension host in an isolated temporary profile.

Requires a supported VS Code executable, xvfb-run and an installed Greatminds CLI.
Does not download editors, install extensions, or invoke a model provider.
"""
import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code', required=True, type=Path)
    parser.add_argument('--cli', required=True, type=Path)
    args = parser.parse_args()
    extension = Path(__file__).resolve().parents[1] / 'vscode-extension'
    root = Path(tempfile.mkdtemp(prefix='greatminds-vscode-host-'))
    project = root / 'project with spaces'
    project.mkdir()
    cli = root / 'cli with spaces $(literal)'
    cli.symlink_to(args.cli.absolute())
    subprocess.run([str(cli), 'setup', '--project-dir', str(project)], check=True, capture_output=True)
    settings = root / 'user-data/User'
    settings.mkdir(parents=True)
    (settings / 'settings.json').write_text(json.dumps({
        'greatminds.cliPath': str(cli), 'telemetry.telemetryLevel': 'off',
        'update.mode': 'none', 'extensions.autoUpdate': False,
        'extensions.autoCheckUpdates': False, 'workbench.startupEditor': 'none',
        'terminal.integrated.enablePersistentSessions': False}))
    print(json.dumps({'artifacts': str(root)}), flush=True)
    with (root / 'editor.log').open('w') as output:
        process = subprocess.Popen(['xvfb-run', '-a', str(args.code.absolute()), '--no-sandbox',
            '--disable-gpu', '--disable-extensions', '--disable-workspace-trust',
            '--skip-welcome', '--skip-release-notes', '--user-data-dir', str(root/'user-data'),
            '--extensions-dir', str(root/'extensions'), '--new-window', '--wait',
            '--extensionDevelopmentPath', str(extension), '--extensionTestsPath',
            str(extension/'test/host-smoke.js'), str(project)],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=120)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            raise
    if code:
        raise subprocess.CalledProcessError(code, process.args)
    print((project / 'host-smoke-result.json').read_text())


if __name__ == '__main__':
    main()
