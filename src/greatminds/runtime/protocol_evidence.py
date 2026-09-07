"""Allowlisted ACP facts: no messages, tool arguments, paths or server metadata."""
import hashlib

TOOL_EVENT_LIMIT = 64
PHASES = {'preflight', 'initialize', 'authenticate', 'session_new', 'session_load',
          'session_configure', 'prompt', 'unknown'}
TOOL_KINDS = {'read', 'edit', 'delete', 'move', 'search', 'execute', 'think',
              'fetch', 'switch_mode', 'other'}
TOOL_STATUSES = {'pending', 'in_progress', 'completed', 'failed', 'unknown'}
STOP_REASONS = {'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled', 'other'}
ERROR_TYPES = {'RequestError', 'TimeoutError', 'OSError', 'ValueError', 'RuntimeError',
               'GreatMindsError', 'FileNotFoundError', 'PermissionError', 'other'}
CAPABILITIES = ('loadSession', 'promptCapabilities.image', 'promptCapabilities.audio',
                'promptCapabilities.embeddedContext', 'mcpCapabilities.http', 'mcpCapabilities.sse',
                'sessionCapabilities.list', 'sessionCapabilities.fork', 'sessionCapabilities.resume')


def choice(value, allowed, default):
    return value if isinstance(value, str) and value in allowed else default


def integer(value):
    return value if type(value) is int and -(2**31) <= value < 2**31 else None


def capability_facts(document):
    result = {}
    for path in CAPABILITIES:
        value = document
        for key in path.split('.'):
            value = value.get(key) if isinstance(value, dict) else None
        if type(value) is bool:
            result[path] = value
        elif isinstance(value, dict):
            result[path] = True  # Presence advertises these optional method groups.
    return result


def normalize(kind, data, *, run_id):
    if not isinstance(data, dict):
        raise ValueError('protocol evidence must be a mapping')
    phase = choice(data.get('phase'), PHASES, 'unknown')
    if kind == 'negotiated':
        return {'phase': phase, 'protocol_version': integer(data.get('protocol_version')),
                'capabilities': capability_facts(data.get('capabilities', {}))}
    if kind == 'error':
        return {'phase': phase, 'rpc_code': integer(data.get('rpc_code')),
                'exception': choice(data.get('exception'), ERROR_TYPES, 'other')}
    if kind == 'stop':
        return {'phase': phase, 'reason': choice(data.get('reason'), STOP_REASONS, 'other')}
    if kind != 'tool':
        raise ValueError('unknown protocol evidence kind')
    update = data.get('update', {})
    if not isinstance(update, dict):
        raise ValueError('protocol tool update must be a mapping')
    identifier = update.get('toolCallId')
    reference = (hashlib.sha256((run_id + '\0' + identifier).encode()).hexdigest()[:24]
                 if isinstance(identifier, str) and 0 < len(identifier) <= 4096 else None)
    content = update.get('content')
    locations = update.get('locations')
    return {'phase': phase, 'tool_reference': reference,
            'update': choice(update.get('sessionUpdate'), {'tool_call', 'tool_call_update'}, 'other'),
            'kind': choice(update.get('kind'), TOOL_KINDS, 'other'),
            'status': choice(update.get('status'), TOOL_STATUSES, 'unknown'),
            'content_count': len(content) if isinstance(content, list) else 0,
            'location_count': len(locations) if isinstance(locations, list) else 0}


def summary(document):
    """Revalidate stored data before copying a small subset into a private bundle."""
    if not isinstance(document, dict):
        return None
    result = {'tool_events_truncated': document.get('tool_events_truncated') is True}
    events = document.get('tool_events', [])
    result['tool_events_retained'] = min(len(events), TOOL_EVENT_LIMIT) if isinstance(events, list) else None
    for kind in ('negotiated', 'error', 'stop'):
        entry = document.get(kind)
        source = entry.get('data') if isinstance(entry, dict) else None
        if isinstance(source, dict):
            if kind == 'negotiated':
                capabilities = source.get('capabilities')
                capabilities = capabilities if isinstance(capabilities, dict) else {}
                result[kind] = {'protocol_version': integer(source.get('protocol_version')),
                    'capabilities': {key: value for key, value in capabilities.items()
                                     if key in CAPABILITIES and type(value) is bool}}
            else:
                result[kind] = normalize(kind, source, run_id='')
    return result
