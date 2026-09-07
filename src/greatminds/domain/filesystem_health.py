"""Shared non-mutating filesystem health observations for operator surfaces."""
from pathlib import Path
import re
import stat
import time

DEFAULT_THRESHOLDS = {'intent_orphan_seconds': 300,
                      'task_stale_in_active_queue_seconds': 86400,
                      'task_stale_in_review_queue_seconds': 43200}
REVIEW_QUEUES = {'feature_review', 'feature_docs_review'}


def thresholds(schema):
    return {**DEFAULT_THRESHOLDS, **(schema.get('watchdog') or {})}


def age(path, now):
    try:
        return max(0, now - path.stat().st_mtime)
    except FileNotFoundError:
        return None  # A concurrent committed transition can remove a candidate.


def candidates(directory, suffixes):
    try:
        entries = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    result = []
    for path in entries:
        if path.suffix not in suffixes:
            continue
        try:
            if stat.S_ISREG(path.stat().st_mode):
                result.append(path)
        except FileNotFoundError:
            continue
    return result


def orphan_intents(runtime, schema, *, now=None):
    now = time.time() if now is None else now
    threshold = thresholds(schema)['intent_orphan_seconds']
    rows = []
    for path in candidates(Path(runtime)/'intent', {'.json'}):
        seconds = age(path, now)
        if seconds is not None and seconds > threshold:
            rows.append({'name': path.name, 'age_seconds': seconds, 'threshold_seconds': threshold})
    return rows


def stale_tasks(runtime, schema, *, now=None):
    now = time.time() if now is None else now
    limits = thresholds(schema)
    rows = []
    for queue, metadata in schema.get('queues', {}).items():
        if not isinstance(metadata, dict) or metadata.get('kind') != 'active':
            continue
        directory = Path(runtime)/queue
        threshold = limits['task_stale_in_review_queue_seconds' if queue in REVIEW_QUEUES
                           else 'task_stale_in_active_queue_seconds']
        for path in candidates(directory, {'.yaml', '.md'}):
            if path.stem == '_TEMPLATE':
                continue
            seconds = age(path, now)
            if seconds is not None and seconds > threshold:
                rows.append({'queue': queue, 'name': path.name, 'task_id': path.stem,
                             'age_seconds': seconds, 'threshold_seconds': threshold})
    return rows


def orphan_worktrees(project, runtime, schema):
    from greatminds.cli.worktree import load_worktree_policy
    policy = load_worktree_policy(Path(project), schema_document=schema)
    base = Path(project)/policy.base_path
    try:
        worktrees = sorted(base.iterdir())
    except FileNotFoundError:
        return []
    active = set()
    for queue, metadata in schema.get('queues', {}).items():
        if not isinstance(metadata, dict):
            continue
        retained_terminal = (queue == 'verified' and not policy.cleanup_on_verified) or (
            queue == 'archive' and not policy.cleanup_on_archive)
        if metadata.get('kind') not in {'active', 'parking'} and not retained_terminal:
            continue
        directory = Path(runtime)/queue
        for path in candidates(directory, {'.yaml', '.md'}):
            if path.stem == '_TEMPLATE':
                continue
            active.add(path.stem)
            # The worktree CLI also accepts a task's four-digit sequence.
            if re.match(r'^\d{4}(?:-|$)', path.stem):
                active.add(path.stem[:4])
    return [{'name': path.name, 'path': str(path)} for path in worktrees
            if path.is_dir() and path.name not in active]
