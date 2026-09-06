"""Source and local environment identities for managed profile execution."""
import hashlib
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.commands import source_identity, environment_identity


def deployment_inputs(runtime, workspace, profile, argv, environment, extra_vars):
    workspace = Path(workspace).resolve()
    profile = Path(profile).resolve()
    if not workspace.is_dir() or not profile.is_file():
        raise GreatMindsError('deployment source or profile is unavailable', exit_code=4)
    # Use the same private HMAC key and executable identity as configured checks.
    identity = environment_identity(runtime / '.runtime', environment, argv, runtime,
                                    context={'playbook_vars': extra_vars})
    digest = hashlib.sha256()
    with profile.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return {'workspace': str(workspace), 'source': source_identity(workspace, runtime),
            'profile': str(profile), 'profile_sha256': digest.hexdigest(), 'environment': identity}
