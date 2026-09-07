"""Bounded event-log tails; authoritative records and evidence are never pruned."""
DEFAULT_MAX_EVENTS = 10000


def next_sequence(state):
    return state['events'][-1]['sequence'] + 1 if state['events'] else 1


def prune_events(state, now):
    policy = state.get('event_retention', {})
    limit = policy.get('max_events', DEFAULT_MAX_EVENTS)
    if type(limit) is not int or not 100 <= limit <= 1000000:
        raise ValueError('invalid persisted runtime event retention limit')
    events = state['events']
    if len(events) <= limit:
        return
    # Keep headroom so an overflow does not add one pruning event per new event.
    count = len(events) - limit * 4 // 5
    through = events[count - 1]['sequence']
    sequence = next_sequence(state)
    state['events'] = events[count:]
    state['event_retention'] = {**policy, 'max_events': limit,
        'discarded_count': policy.get('discarded_count', 0) + count,
        'discarded_through': through, 'updated_at': now}
    state['events'].append({'sequence': sequence, 'at': now, 'kind': 'events_pruned',
        'run_id': None, 'data': {'discarded_count': count, 'discarded_through': through,
                                'max_events': limit}})
