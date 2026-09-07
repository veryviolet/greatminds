"""Bounded startup retries derived from durable outcomes, never provider prose."""


def startup_failure(run):
    outcome = run.get('outcome', {})
    return (run['state'] == 'failed' and not run.get('conversation_id')
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
