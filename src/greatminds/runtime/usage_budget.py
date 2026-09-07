"""Session cost continuity and reactive limits for reported ACP cost."""

import copy

from greatminds.core.errors import GreatMindsError


class UsageBudgetHeld(GreatMindsError):
    def __init__(self, details):
        self.details = details
        super().__init__('reported usage budget holds this session', exit_code=2)


def initial(previous, *, unknown_history=False):
    ledger = copy.deepcopy(previous) if previous else {
        'amount': None, 'currency': None, 'error': None, 'completed_prompts': 0,
        'pending': False, 'fresh': False, 'status': 'missing'}
    if ledger['pending'] or unknown_history:
        ledger['error'] = ledger['error'] or 'session_history_unknown'
        ledger['fresh'] = False
    # A previous run's uncertain prompt is evidence, not a live prompt owned by
    # this run. Disabled budgets must not create a new implicit execution gate.
    ledger['pending'] = False
    return ledger


def observe(ledger, cost):
    ledger['status'] = cost['status']
    ledger['fresh'] = cost['status'] == 'reported'
    if cost['status'] == 'invalid':
        ledger['error'] = ledger['error'] or 'invalid_cost'
    if cost['status'] != 'reported':
        return
    if ledger['currency'] is not None and ledger['currency'] != cost['currency']:
        ledger['error'] = ledger['error'] or 'currency_changed'
    elif ledger['amount'] is not None and cost['amount'] < ledger['amount']:
        ledger['error'] = ledger['error'] or 'cost_regressed'
    else:
        ledger['amount'], ledger['currency'] = cost['amount'], cost['currency']


def verdict(ledger, binding, *, phase):
    limit = binding.max_reported_session_cost
    if limit is None:
        return None
    reason = ledger['error']
    if reason is None and ledger['currency'] is not None and ledger['currency'] != binding.reported_cost_currency:
        reason = 'currency_mismatch'
    if reason is None and ledger['amount'] is not None and ledger['amount'] >= limit:
        reason = 'cost_limit_reached'
    # One explicitly documented bootstrap prompt can establish the first report.
    # Afterwards, a completed turn without a fresh report cannot unlock more work.
    if reason is None and ((phase == 'finish' and not ledger['fresh']) or
                           (phase == 'begin' and ledger['completed_prompts'] and
                            (not ledger['fresh'] or ledger['status'] != 'reported'))):
        reason = 'cost_unavailable'
    if reason is None:
        return None
    return {'budget': 'reported_session_cost', 'reason': reason, 'limit': limit,
            'amount': ledger['amount'], 'currency': ledger['currency'],
            'required_currency': binding.reported_cost_currency}
