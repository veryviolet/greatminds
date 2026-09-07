"""Operator action descriptions; execution stays in existing validated services."""


def recovery_actions(component, code, evidence, *, project):
    operation = evidence.get('operation_id')
    if not operation:
        return []
    def action(name, argv, effect, preconditions, required_options=()):
        return {'id': name, 'argv': ['greatminds', *argv], 'effect': effect,
                'preconditions': preconditions, 'required_options': list(required_options),
                'automatic': False, 'cwd': str(project),
                'environment': {'GREATMINDS_PROJECT_DIR': str(project)}}
    operator = ['operator context without run credentials']
    if component == 'commands' and code == 'operation_needs_recovery':
        return [
            action('inspect_command', ['run', 'command-status', operation], 'read_only', []),
            action('resolve_command', ['run', 'command-resolve', operation], 'acknowledge_external_uncertainty',
                   operator + ['command is needs_recovery', 'operator has inspected external effects'], ['--reason']),
        ]
    if component == 'maintenance' and code == 'operation_needs_recovery':
        return [
            action('inspect_operation', ['run', 'status'], 'read_only', []),
            action('reconcile_operation', ['run', 'repair', '--operation', operation], 'request_daemon_reconciliation',
                   operator + ['operation is needs_recovery', 'daemon rechecks pinned revisions, dependencies and gates']),
            action('abandon_operation', ['run', 'repair', '--operation', operation, '--abandon'], 'abandon_uncommitted_intent',
                   operator + ['operation is needs_recovery', 'source task remains a regular file',
                               'destination does not exist'], ['--reason']),
        ]
    if component == 'deployments' and code == 'deployment_unresolved':
        return [
            action('inspect_deployment', ['stand', 'deployment-status'], 'read_only', []),
            action('recover_deployment_process', ['stand', 'deployment-recover', operation], 'clean_tracked_process_group',
                   operator + ['operator or permitted stand control role', 'exclusive deployment lock is available',
                               'tracked process identity is verified before termination']),
            action('resolve_deployment', ['stand', 'deployment-resolve', operation], 'acknowledge_external_uncertainty',
                   operator + ['operator or permitted stand control role', 'exclusive deployment lock is available',
                               'process cleanup is confirmed', 'tracked process group is not alive',
                               'operator has inspected external effects'], ['--reason']),
        ]
    return []
