"""Shared admission from explicitly configured ACP error codes, never prose."""
from dataclasses import dataclass
import copy
import uuid

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import safe_name


@dataclass(frozen=True)
class AccountLimitError:
    code: int
    kind: str
    cooldown_seconds: int | None = None


def parse_rules(raw):
    if not isinstance(raw, list) or len(raw) > 16:
        raise GreatMindsError('account_limit_errors must be an array of at most 16 rules', exit_code=2)
    rules = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {'code', 'kind', 'cooldown_seconds'}:
            raise GreatMindsError('invalid account limit error rule fields', exit_code=2)
        code, kind, cooldown = item.get('code'), item.get('kind'), item.get('cooldown_seconds')
        if (type(code) is not int or not -(2**31) <= code < 2**31
                or -32768 <= code <= -32000 or any(r.code == code for r in rules)):
            raise GreatMindsError('account limit code must be a unique application integer outside the reserved JSON-RPC range', exit_code=2)
        if kind not in ('rate_limit', 'quota_exhausted'):
            raise GreatMindsError('account limit kind must be rate_limit or quota_exhausted', exit_code=2)
        if ((kind == 'rate_limit' and (type(cooldown) is not int or not 1 <= cooldown <= 86400))
                or (kind == 'quota_exhausted' and cooldown is not None)):
            raise GreatMindsError('rate_limit requires cooldown_seconds 1..86400; quota_exhausted requires operator resume', exit_code=2)
        rules.append(AccountLimitError(code, kind, cooldown))
    return tuple(rules)


def admission(snapshot, account, now):
    hold = snapshot.get('account_holds', {}).get(account)
    if (not hold or hold['status'] == 'resolved'
            or (hold['until'] is not None and now >= hold['until'])):
        return {'reason': 'ready', 'account': account}
    return {'reason': 'account_' + hold['kind'], 'account': account,
            'hold_id': hold['id'], 'next_at': hold['until'], 'source_run': hold['source_run']}


class AccountLimitHeld(Exception):
    def __init__(self, details):
        self.details = details
        super().__init__(details['reason'])


class AccountLimits:
    def __init__(self, store):
        self.store = store

    def record(self, run_id, *, owner_id, rpc_code):
        """Pin meaning to the reporting run's validated launch contract."""
        from .store import TERMINAL
        with self.store._transaction() as state:
            run = self.store._run(state, run_id)
            if run['owner_id'] != owner_id or run['state'] in TERMINAL:
                raise GreatMindsError('account signal requires the active run owner', exit_code=3)
            agent = next(a for a in self.store.contracts(run_id)['execution']['agents']
                         if a['id'] == run['agent_id'])
            rule = next((r for r in parse_rules(agent.get('account_limit_errors', []))
                         if type(rpc_code) is int and r.code == rpc_code), None)
            if rule is None:
                return None
            if run.get('account_limit_observation'):
                return copy.deepcopy(run['account_limit_observation'])
            now = self.store.clock()
            holds = state.setdefault('account_holds', {})
            old = holds.get(run['account'])
            hold = {'id': uuid.uuid4().hex, 'account': run['account'], 'status': 'active',
                    'kind': rule.kind, 'until': now + rule.cooldown_seconds if rule.cooldown_seconds else None,
                    'source_run': run_id, 'rpc_code': rpc_code, 'observed_at': now}
            if admission(state, run['account'], now)['reason'] != 'ready':
                # A concurrent signal cannot shorten a cooldown or clear a quota
                # hold. A new identity invalidates previously printed actions.
                if old['until'] is None:
                    hold.update(kind=old['kind'], until=None, source_run=old['source_run'], rpc_code=old['rpc_code'])
                elif hold['until'] is not None:
                    hold['until'] = max(old['until'], hold['until'])
            holds[run['account']] = hold
            observation = {'hold_id': hold['id'], 'kind': rule.kind, 'rpc_code': rpc_code,
                           'observed_at': now, 'account': run['account']}
            run['account_limit_observation'] = observation
            self.store._event(state, 'account_limit_observed', run_id, observation)
            return copy.deepcopy(observation)

    def resume(self, account, *, hold_id, reason):
        safe_name(account)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise GreatMindsError('account resume requires an explanation of at most 1000 characters')
        with self.store._transaction() as state:
            hold = state.get('account_holds', {}).get(account)
            if not hold or hold['id'] != hold_id:
                raise GreatMindsError('account hold changed; inspect the current hold before resuming')
            if hold['status'] == 'resolved':
                if hold['resolution'] != reason:
                    raise GreatMindsError('account hold was resolved with a different explanation')
                return copy.deepcopy(hold)
            hold.update(status='resolved', resolution=reason, resolved_at=self.store.clock())
            self.store._event(state, 'account_resumed', None, {'account': account, 'hold_id': hold_id})
            return copy.deepcopy(hold)
