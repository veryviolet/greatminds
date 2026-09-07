"""Bounded retries, connectivity delay and task progress from durable outcomes."""


def startup_failure(run, *, include_conversations=False):
    outcome = run.get('outcome', {})
    return (run['state'] == 'failed' and (include_conversations or not run.get('conversation_id'))
            and outcome.get('prompt_started') is False
            and outcome.get('pre_prompt_activity') is False
            and run.get('reason') in {'transport_failure', 'timeout'}
            and outcome.get('error_type') not in {'FileNotFoundError', 'PermissionError'})


def no_progress(snapshot, binding, task):
    """Count completed queue turns since a task handoff, not token activity.

    Content-only edits within the same queue do not reset this budget. Applied
    handoff/blocked receipts and intervening queue stages are durable progress.
    """
    history = sorted((r for r in snapshot['runs'].values() if r['task_id'] == task.task_id),
                     key=lambda r: (r['created_at'], r.get('sequence', 0)), reverse=True)
    advanced = {r['envelope']['run_id'] for r in snapshot['results'].values()
                if r['status'] == 'applied' and r['envelope']['decision'] in {'handoff', 'blocked'}}
    count = 0
    for run in history:
        if run['task_path'] != task.path or run['id'] in advanced:
            break
        if run['state'] == 'completed' and not run.get('conversation_id'):
            count += 1
    return {'reason': 'no_progress_limit' if count >= binding.max_no_progress_turns else 'ready',
            'turns': count, 'max_turns': binding.max_no_progress_turns}


def retry_admission(snapshot, binding, task, config, schema, now):
    """Return a shared scheduling verdict; missing outcome evidence cannot retry."""
    history = sorted((r for r in snapshot['runs'].values() if r['task_id'] == task.task_id),
                     key=lambda r: (r['created_at'], r.get('sequence', 0)))
    if history and history[-1].get('retry_authorized'):
        return {'reason': 'ready'}
    if history:
        last = history[-1]
        if last['task_path'] != task.path:
            return {'reason': 'ready'}
        receipts = [r for r in snapshot['results'].values() if r['envelope']['run_id'] == last['id']]
        for receipt in receipts:
            if receipt['status'] == 'rejected':
                return {'reason': 'result_rejected'}
            if receipt['envelope']['decision'] == 'needs_input':
                return {'reason': 'human_input_required'}
            if receipt['envelope']['decision'] == 'no_change':
                return {'reason': 'no_change_reported'}
        if any(item['run_id'] == last['id'] for item in snapshot.get('commands', {}).values()):
            return {'reason': 'revision_already_attempted'}
    progress = no_progress(snapshot, binding, task)
    if progress['reason'] != 'ready':
        return progress
    if not history:
        return {'reason': 'ready'}
    latest = history[-1]
    if (latest['binding_sha256'] != binding.sha256 or latest['config_sha256'] != config.sha256
            or latest['schema_sha256'] != schema.sha256 or latest.get('conversation_id')):
        return {'reason': 'revision_already_attempted'}
    if (any(item['envelope']['run_id'] == latest['id'] for item in snapshot['results'].values())
            or any(item['run_id'] == latest['id'] for item in snapshot.get('commands', {}).values())):
        return {'reason': 'revision_already_attempted'}
    if latest['state'] == 'completed' and latest.get('reason') == 'turn_ended':
        next_at = latest['updated_at'] + binding.retry_initial_seconds
        return {**progress, 'reason': 'no_progress_backoff' if now < next_at else 'ready',
                'next_at': next_at, 'continue_after': latest['id']}
    if not startup_failure(latest) or latest["task_revision"] != task.sha256:
        return {'reason': 'revision_already_attempted'}
    # Count attempts across all contracts and manual retries for this unchanged
    # revision. An explicit retry permits one run; it never resets this budget.
    failures = sum(startup_failure(run) for run in history if run["task_revision"] == task.sha256)
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
