"""Bounded startup retries derived from durable outcomes, never provider prose."""


def startup_failure(run, *, include_conversations=False):
    outcome = run.get('outcome', {})
    return (run['state'] == 'failed' and (include_conversations or not run.get('conversation_id'))
            and outcome.get('prompt_started') is False
            and outcome.get('pre_prompt_activity') is False
            and run.get('reason') in {'transport_failure', 'timeout'}
            and outcome.get('error_type') not in {'FileNotFoundError', 'PermissionError'})


def startup_retry(snapshot, binding, task, config, schema, now):
    """Return a shared scheduling verdict; missing outcome evidence cannot retry."""
    previous = sorted((r for r in snapshot['runs'].values()
                       if r['task_id'] == task.task_id and r['task_revision'] == task.sha256),
                      key=lambda r: (r['created_at'], r.get('sequence', 0)))
    if not previous:
        return {'reason': 'ready'}
    latest = previous[-1]
    if latest.get('retry_authorized'):
        return {'reason': 'ready'}
    if (not startup_failure(latest) or latest['binding_sha256'] != binding.sha256
            or latest['config_sha256'] != config.sha256 or latest['schema_sha256'] != schema.sha256):
        return {'reason': 'revision_already_attempted'}
    if (any(item['envelope']['run_id'] == latest['id'] for item in snapshot['results'].values())
            or any(item['run_id'] == latest['id'] for item in snapshot.get('commands', {}).values())):
        return {'reason': 'revision_already_attempted'}
    # Count attempts across all contracts and manual retries for this unchanged
    # revision. An explicit retry permits one run; it never resets this budget.
    failures = sum(startup_failure(run) for run in previous)
    if failures > binding.max_startup_retries:
        return {'reason': 'startup_retry_limit', 'failures': failures,
                'max_retries': binding.max_startup_retries}
    delay = min(binding.retry_max_seconds,
                binding.retry_initial_seconds * 2 ** min(failures - 1, 20))
    next_at = latest['updated_at'] + delay
    return {'reason': 'retry_backoff' if now < next_at else 'ready',
            'next_at': next_at, 'failures': failures, 'max_retries': binding.max_startup_retries,
            'retry_of': latest['id']}


def account_backoff(snapshot, account, config, now):
    """One startup failure streak per account across bindings, tasks and sessions.

    A durable prompt start proves startup recovery. No token stream or successful
    prompt is treated as domain progress by this narrow connectivity controller.
    """
    runs = sorted((r for r in snapshot['runs'].values() if r['account'] == account),
                  key=lambda r: (r['updated_at'], r.get('sequence', 0)))
    failures, latest = 0, None
    for run in runs:
        if run.get('outcome', {}).get('prompt_started') is True or (
                run['state'] == 'running' and run.get('session_id')):
            failures, latest = 0, None
        elif startup_failure(run, include_conversations=True):
            failures += 1
            latest = run
    if latest is None:
        return {'reason': 'ready', 'account': account, 'failures': 0}
    delay = min(config.account_retry_max_seconds,
                config.account_retry_initial_seconds * 2 ** min(failures - 1, 20))
    next_at = latest['updated_at'] + delay
    return {'reason': 'account_backoff' if now < next_at else 'ready',
            'account': account, 'failures': failures, 'next_at': next_at,
            'source_run': latest['id']}
