"""Allowlisted SDK-decoded observations, never billing or inferred token totals.

ACP schema v1.16.0 calls PromptResponse.usage both per-turn and cumulative.
Until a manifest explicitly declares a verified interpretation, retain the latest
sample with unknown scope. In particular, never sum samples or token categories.
"""

import math
import re


TOKEN_FIELDS = ('totalTokens', 'inputTokens', 'outputTokens', 'thoughtTokens',
                'cachedReadTokens', 'cachedWriteTokens')


def _count(value):
    return type(value) is int and 0 <= value <= 2**64 - 1


def _sample(data, fields, required):
    if data is None:
        return {'status': 'missing'}
    if not isinstance(data, dict):
        return {'status': 'invalid'}
    if any(not _count(data.get(key)) for key in required):
        return {'status': 'invalid'}
    if any(value is not None and not _count(value)
           for key in fields for value in [data.get(key)]):
        return {'status': 'invalid'}
    return {'status': 'reported', 'values': {
        key: data[key] for key in fields if data.get(key) is not None}}


def normalize(kind, data):
    if kind == 'tokens':
        return {**_sample(data, TOKEN_FIELDS, TOKEN_FIELDS[:3]), 'scope': 'unknown'}
    if kind != 'session':
        raise ValueError('unknown usage observation kind')
    context = _sample(data, ('used', 'size'), ('used', 'size'))
    cost_data = data.get('cost') if isinstance(data, dict) else None
    cost = {'status': 'missing', 'scope': 'session'}
    if cost_data is not None:
        if (isinstance(cost_data, dict)
                and type(cost_data.get('amount')) in (int, float)
                and 0 <= cost_data['amount'] <= 1e100
                and math.isfinite(cost_data['amount'])
                and isinstance(cost_data.get('currency'), str)
                and re.fullmatch('[A-Z]{3}', cost_data['currency'])):
            cost.update(status='reported', amount=cost_data['amount'], currency=cost_data['currency'])
        else:
            cost['status'] = 'invalid'
    return {'context': {**context, 'scope': 'context'}, 'cost': cost}
