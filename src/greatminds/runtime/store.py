"""Durable run identities, claims, and result receipts on a local filesystem.

One short flock transaction replaces the metadata snapshot atomically. Task
locks are acquired before the store lock whenever both are needed. Process
launch and domain result application are deliberately outside this store:
an accepted receipt is not a task transition or a correctness verdict.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import SchemaSnapshot
from greatminds.core.storage import atomic_json, file_lock, safe_name, task_lock
from .config import ExecutionConfig, RoleBinding, fingerprint
from .event_retention import next_sequence, prune_events


TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
TRANSITIONS = {
    "claimed": {"starting", "cancelled", "interrupted"},
    "starting": {"running", "waiting_auth", "waiting_input", "failed", "cancelling", "interrupted"},
    "running": {"waiting_input", "waiting_auth", "completed", "failed", "cancelling", "interrupted"},
    "waiting_input": {"running", "cancelling", "failed", "interrupted"},
    "waiting_auth": {"starting", "cancelling", "failed", "interrupted"},
    "cancelling": {"cancelled", "failed", "interrupted"},
}


def _error(message: str, code: int = 2):
    raise GreatMindsError(message, exit_code=code)


@dataclass(frozen=True)
class TaskRevision:
    """Pinned execution subject; an empty path denotes a conversation, not a file."""
    task_id: str
    path: str
    sha256: str

    @classmethod
    def conversation(cls, runtime: Path, conversation_id: str) -> TaskRevision:
        from .interactions import ConversationStore
        document = ConversationStore(runtime, conversation_id).snapshot()
        if document.get('task') is not None:
            return cls(**document['task'])
        identity = {key: document[key] for key in (
            'id', 'binding_id', 'binding_sha256', 'config_sha256', 'schema_sha256', 'workspace')}
        return cls('chat-' + conversation_id, '', fingerprint(identity))

    @classmethod
    def capture(cls, runtime: Path, path: Path) -> TaskRevision:
        root = runtime.resolve()
        resolved = path.resolve()
        if path.absolute() != resolved:
            _error("task paths must not contain symlinks or traversal")
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            _error("task must be inside the runtime directory")
        if len(relative.parts) != 2 or relative.parts[0].startswith(".") or path.suffix != ".yaml":
            _error("task must be a YAML file directly inside a queue")
        safe_name(relative.parts[0])
        task_id = safe_name(path.stem)
        content = resolved.read_bytes()
        # Moving queues changes the revision even when file contents do not.
        digest = hashlib.sha256(str(relative).encode() + b"\0" + content).hexdigest()
        return cls(task_id, str(relative), digest)


@dataclass(frozen=True)
class Claim:
    run: dict
    token: str


@dataclass(frozen=True)
class ResultEnvelope:
    result_id: str
    run_id: str
    task_id: str
    task_revision: str
    schema_sha256: str
    decision: str
    payload: dict

    def document(self) -> dict:
        for value in (self.result_id, self.run_id, self.task_id):
            safe_name(value)
        if self.decision not in {"handoff", "blocked", "needs_input", "no_change"}:
            _error("unknown result decision")
        if not isinstance(self.payload, dict):
            _error("result payload must be a mapping")
        # Role and provenance are derived from the authenticated run.
        if {"role", "by", "run_id", "task_revision", "schema_sha256"} & self.payload.keys():
            _error("result payload cannot override run identity", 3)
        document = asdict(self)
        try:
            fingerprint(document)
        except (TypeError, ValueError) as exc:
            _error(f"result must contain finite JSON values: {exc}")
        return document


class RunStore:
    def __init__(self, runtime: Path, *, clock: Callable[[], float] = time.time):
        self.runtime = runtime.resolve()
        self.directory = self.runtime / ".runtime"
        self.path = self.directory / "state.json"
        self.clock = clock

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("runs"), dict)
                    or not isinstance(state.get("events"), list)
                    or not isinstance(state.get("results"), dict)
                    or not isinstance(state.get("project_id"), str)
                    or type(state.get("paused")) is not bool):
                raise ValueError("invalid or unsupported runtime state")
            return state
        except (OSError, ValueError) as exc:
            _error(f"cannot read runtime state {self.path}: {exc}", 4)

    @contextmanager
    def _transaction(self) -> Iterator[dict]:
        with file_lock(self.directory / "store.lock", label="runtime"):
            state = self._read() or {
                "version": 1, "project_id": uuid.uuid4().hex, "paused": False,
                "runs": {}, "events": [], "results": {},
            }
            before = copy.deepcopy(state)
            yield state
            if state != before or not self.path.exists():
                prune_events(state, self.clock())
                atomic_json(self.path, state)

    def snapshot(self) -> dict:
        """Read-only operator view; never return credential hashes."""
        state = self._read()
        if state is None:
            return {"version": 1, "project_id": None, "paused": False,
                    "runs": {}, "events": [], "results": {}}
        for run in state["runs"].values():
            run.pop("token_sha256", None)
        return state

    def authorize(self, run_id: str, token: str) -> dict:
        state = self._read() or {"runs": {}}
        run = self._run(state, run_id)
        if not hmac.compare_digest(run["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
            _error("invalid run credential", 3)
        if run["state"] not in {"running", "waiting_input"}:
            _error("run is not authorized for domain operations", 3)
        return {key: copy.deepcopy(value) for key, value in run.items() if key != "token_sha256"}

    def _event(self, state: dict, kind: str, run_id: str | None, data: dict) -> None:
        state["events"].append({"sequence": next_sequence(state),
                                "at": self.clock(), "kind": kind,
                                "run_id": run_id, "data": copy.deepcopy(data)})

    def record_protocol(self, run_id: str, *, owner_id: str, kind: str, data: dict) -> bool:
        from .protocol_evidence import normalize, TOOL_EVENT_LIMIT
        normalized = normalize(kind, data, run_id=run_id)
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run['owner_id'] != owner_id or run['state'] in TERMINAL:
                _error('protocol observation requires the active supervisor', 3)
            protocol = run.setdefault('protocol', {'version': 1, 'tool_events': [],
                                                   'tool_events_truncated': False})
            if kind == 'tool':
                events = protocol['tool_events']
                if len(events) >= TOOL_EVENT_LIMIT:
                    if not protocol['tool_events_truncated']:
                        protocol['tool_events_truncated'] = True
                        self._event(state, 'protocol_trace_truncated', run_id, {'limit': TOOL_EVENT_LIMIT})
                    return False
                if events and events[-1]['data'] == normalized:
                    return True
            elif protocol.get(kind, {}).get('data') == normalized:
                return True
            entry = {'at': self.clock(), 'data': normalized}
            if kind == 'tool':
                protocol['tool_events'].append(entry)
            else:
                protocol[kind] = entry
            self._event(state, 'protocol_observed', run_id, {'kind': kind, **normalized})
        return True

    def record_usage(self, run_id: str, *, owner_id: str, kind: str, data) -> None:
        from .usage_observations import normalize
        normalized = normalize(kind, data)
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run['owner_id'] != owner_id or run['state'] in TERMINAL:
                _error('usage observation requires the active supervisor', 3)
            usage = run.setdefault('usage', {'version': 1, 'source': 'acp_sdk_decoded'})
            usage[kind] = {'at': self.clock(), 'data': normalized}
            if kind == 'session' and 'cost_accounting' in usage:
                from .usage_budget import observe
                observe(usage['cost_accounting'], normalized['cost'])
            # Latest samples are bounded. Missing reports replace previous samples
            # explicitly; old values must not appear current after another turn.
            self._event(state, 'usage_observed', run_id, {'kind': kind, **normalized})

    def prepare_usage_session(self, run_id: str, *, owner_id: str, loaded: bool) -> None:
        from .usage_budget import initial, observe
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run['owner_id'] != owner_id or run['state'] != 'running' or not run.get('session_id'):
                _error('usage accounting requires the running session supervisor', 3)
            usage = run.setdefault('usage', {'version': 1, 'source': 'acp_sdk_decoded'})
            if 'cost_accounting' in usage:
                return
            history = [other for other in state['runs'].values() if other['id'] != run_id and
                       all(other.get(key) == run[key] for key in ('agent_sha256', 'workspace', 'session_id'))]
            previous = max(history, key=lambda other: other['sequence']) if history else None
            prior = previous.get('usage', {}).get('cost_accounting') if previous else None
            ledger = initial(prior, unknown_history=(loaded or bool(history)) and prior is None)
            if 'session' in usage:
                observe(ledger, usage['session']['data']['cost'])
            usage['cost_accounting'] = ledger
            self._event(state, 'usage_session_prepared', run_id, {})

    def usage_prompt_boundary(self, run_id: str, *, owner_id: str, binding: RoleBinding,
                              phase: str) -> dict | None:
        from .usage_budget import verdict
        if phase not in {'admit', 'begin', 'finish', 'observe'}:
            _error('invalid usage prompt phase')
        with self._transaction() as state:
            run = self._run(state, run_id)
            if (run['owner_id'] != owner_id or run['state'] in TERMINAL or
                    (phase != 'observe' and run['state'] not in {'running', 'waiting_input'})):
                _error('usage budget requires the active session supervisor', 3)
            if run['binding_sha256'] != binding.sha256:
                _error('usage budget binding changed', 3)
            ledger = run['usage']['cost_accounting']
            result = verdict(ledger, binding, phase='begin' if phase == 'admit' else phase)
            if phase == 'begin' and result is None:
                if ledger['pending']:
                    _error('usage prompt is already pending', 3)
                ledger['pending'], ledger['fresh'] = True, False
            elif phase == 'finish':
                if not ledger['pending']:
                    _error('usage prompt is not pending', 3)
                ledger['pending'] = False
                ledger['completed_prompts'] += 1
            if phase in {'begin', 'finish'}:
                self._event(state, 'usage_prompt_' + phase, run_id, {'held': result is not None})
            return result

    def configure_event_retention(self, max_events: int) -> None:
        if type(max_events) is not int or not 100 <= max_events <= 1000000:
            _error("max_runtime_events must be between 100 and 1000000")
        with self._transaction() as state:
            policy = state.setdefault('event_retention', {})
            if policy.get('max_events') != max_events:
                policy['max_events'] = max_events
                self._event(state, 'event_retention_configured', None, {'max_events': max_events})

    def set_paused(self, paused: bool) -> None:
        if type(paused) is not bool:
            _error("paused must be a boolean")
        with self._transaction() as state:
            if state["paused"] != paused:
                state["paused"] = paused
                self._event(state, "dispatch_paused" if paused else "dispatch_resumed", None, {})

    def claim(self, *, task: TaskRevision, binding: RoleBinding,
              config: ExecutionConfig, schema: SchemaSnapshot,
              project: Path, owner_id: str, conversation_id: str | None = None,
              automatic: bool = False) -> Claim:
        safe_name(owner_id)
        if binding not in config.bindings or binding.role not in schema.document.get("roles", {}):
            _error("binding is not part of the effective execution contract", 3)
        if conversation_id is not None:
            from .interactions import ConversationStore
            conversation = ConversationStore(self.runtime, conversation_id).snapshot()
            if (task != TaskRevision.conversation(self.runtime, conversation_id)
                    or conversation['closed'] or conversation.get('close_requested')
                    or conversation['binding_sha256'] != binding.sha256
                    or conversation['config_sha256'] != config.sha256
                    or conversation['schema_sha256'] != schema.sha256
                    or conversation['workspace'] != str(binding.workspace_path(project))):
                _error('conversation does not match the execution contract', 3)
        if ((conversation_id is None or conversation.get('task') is not None)
                and task.path.split("/")[0] not in schema.document["roles"][binding.role].get("claims_from", [])):
            _error(f"role {binding.role} cannot claim the assigned queue", 3)
        workspace = str(binding.workspace_path(project))
        agent = config.agent(binding.agent)
        with task_lock(self.runtime, task.task_id), self._transaction() as state:
            self._check_revision(task)
            if any(item["task_id"] == task.task_id and item["status"] in {"prepared", "needs_recovery"}
                   for item in state.get("maintenance", {}).values()):
                _error("task has an unresolved maintenance operation")
            if any(receipt["envelope"]["task_id"] == task.task_id
                   and receipt["status"] in {"received", "applying", "needs_recovery"}
                   for receipt in state["results"].values()):
                _error("task has an unresolved domain result")
            if any(item["task_id"] == task.task_id and item["status"] in {"queued", "starting", "running", "needs_recovery"}
                   for item in state.get("commands", {}).values()):
                _error("task has an unresolved command")
            if state["paused"]:
                _error("dispatch is paused")
            active = [run for run in state["runs"].values() if run["state"] not in TERMINAL]
            if any(run['account'] == binding.account and run['state'] == 'waiting_auth' for run in active):
                _error('account requires authentication')
            if any(run["task_id"] == task.task_id for run in active):
                _error(f"task {task.task_id} already has an active run")
            if len(active) >= config.max_running:
                _error("project execution capacity reached")
            if sum(run["binding_id"] == binding.id for run in active) >= binding.max_running:
                _error(f"binding {binding.id} execution capacity reached")
            limit = dict(config.account_limits).get(binding.account, config.max_running)
            if sum(run["account"] == binding.account for run in active) >= limit:
                _error(f"account {binding.account} execution capacity reached")
            if automatic:
                from .retry_policy import retry_admission
                retry = retry_admission(state, binding, task, config, schema, self.clock())
                if retry['reason'] != 'ready':
                    _error(f"automatic dispatch held: {retry['reason']}")
                if retry.get('retry_of'):
                    self._event(state, 'startup_retry_dispatch', retry['retry_of'], retry)
                if retry.get('continue_after'):
                    self._event(state, 'no_progress_continuation', retry['continue_after'], retry)
            from .retry_policy import account_backoff
            if account_backoff(state, binding.account, config, self.clock())['reason'] != 'ready':
                _error('account_backoff: wait for the shared account retry time')
            self._persist_contract("schema", schema.sha256,
                                   {"text": schema.text, "version": schema.version})
            self._persist_contract("execution", config.sha256, asdict(config))
            token = secrets.token_urlsafe(32)
            run_id = uuid.uuid4().hex
            run = {
                "id": run_id, "project_id": state["project_id"], "owner_id": owner_id,
                "conversation_id": conversation_id,
                "conversation_task": bool(conversation_id is not None and conversation.get('task') is not None),
                "task_id": task.task_id, "task_path": task.path, "task_revision": task.sha256,
                "role": binding.role, "binding_id": binding.id, "binding_sha256": binding.sha256,
                "agent_id": agent.id, "agent_sha256": agent.sha256, "config_sha256": config.sha256,
                "schema_sha256": schema.sha256, "schema_version": schema.version,
                "workspace": workspace, "account": binding.account,
                "permission": binding.permission, "state": "claimed", "created_at": self.clock(),
                "sequence": next_sequence(state),
                "updated_at": self.clock(), "session_id": None, "event_receipts": {},
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
            state["runs"][run_id] = run
            self._event(state, "claimed", run_id, {"task_id": task.task_id})
            public = {key: copy.deepcopy(value) for key, value in run.items() if key != "token_sha256"}
        return Claim(public, token)

    def _persist_contract(self, kind: str, digest: str, document: dict) -> None:
        """Write immutable dependencies before publishing the run that uses them.

        A crash may leave an unreferenced contract, never a run referencing a
        contract that was not synced. No credential values enter this document.
        """
        path = self.directory / "contracts" / f"{kind}-{digest}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if fingerprint(existing) != fingerprint(document):
                    raise ValueError("stored contract differs from its identity")
            except (OSError, ValueError) as exc:
                _error(f"cannot read pinned contract {path}: {exc}", 4)
        else:
            atomic_json(path, document)

    def contracts(self, run_id: str) -> dict:
        run = self._run(self.snapshot(), run_id)
        result = {}
        for kind, key in (("schema", "schema_sha256"), ("execution", "config_sha256")):
            path = self.directory / "contracts" / f"{kind}-{run[key]}.json"
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                digest = (hashlib.sha256(document["text"].encode()).hexdigest()
                          if kind == "schema" else fingerprint(document))
                if digest != run[key]:
                    raise ValueError("contract content does not match pinned identity")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _error(f"cannot read pinned contract {path}: {exc}", 4)
            result[kind] = document
        return result

    def _check_revision(self, task: TaskRevision) -> None:
        if task.path == '' and task.task_id.startswith('chat-'):
            current = TaskRevision.conversation(self.runtime, task.task_id.removeprefix('chat-'))
            if current != task:
                _error('conversation identity changed')
            return
        try:
            current = TaskRevision.capture(self.runtime, self.runtime / task.path)
        except (FileNotFoundError, OSError):
            _error("stale task revision: assigned task is missing or moved")
        if current != task:
            _error("stale task revision: assigned task has changed")

    @staticmethod
    def _run(state: dict, run_id: str) -> dict:
        if run_id not in state["runs"]:
            _error(f"unknown run {run_id}")
        return state["runs"][run_id]

    def transition(self, run_id: str, *, owner_id: str, event_id: str,
                   target: str, reason: str = "", session_id: str | None = None,
                   details: dict | None = None) -> dict:
        """Apply one supervisor event; duplicate delivery cannot replay it."""
        safe_name(event_id)
        payload = {"target": target, "reason": reason, "session_id": session_id}
        if details is not None:
            payload["details"] = details
        digest = fingerprint(payload)
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id:
                _error("run belongs to a different supervisor", 3)
            previous = run["event_receipts"].get(event_id)
            if previous is not None:
                if previous != digest:
                    _error("event identity reused with different contents")
                return {k: copy.deepcopy(v) for k, v in run.items() if k != "token_sha256"}
            if target not in TRANSITIONS.get(run["state"], set()):
                _error(f"illegal run transition {run['state']} -> {target}")
            run.update(state=target, updated_at=self.clock(), reason=reason)
            if session_id is not None:
                run["session_id"] = session_id
                if target == "running" and session_id:
                    run.setdefault("startup_ready_at", run["updated_at"])
            if details is not None:
                run["outcome"] = copy.deepcopy(details)
            run["event_receipts"][event_id] = digest
            self._event(state, target, run_id, payload)
            return {k: copy.deepcopy(v) for k, v in run.items() if k != "token_sha256"}

    def record_timing(self, run_id: str, *, owner_id: str, stage: str, seconds: float) -> None:
        """Persist first observed run-stage offsets; absent observations stay unknown."""
        import math
        allowed = {"workspace_ready", "context_ready", "process_recorded", "protocol_ready",
                   "session_ready", "first_prompt_started", "first_protocol_activity",
                   "first_prompt_activity", "cleanup_complete"}
        if stage not in allowed or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            _error("invalid run timing observation")
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id or run["state"] in TERMINAL:
                _error("timing observation requires the active run's supervisor", 3)
            timings = run.setdefault("timings", {})
            if stage not in timings:
                timings[stage] = seconds
                self._event(state, "run_stage_observed", run_id, {"stage": stage, "offset_seconds": seconds})

    def reserve_prompt_input(self, run_id: str, *, owner_id: str, request_id: str,
                             size: int, limit: int) -> dict:
        """Reserve known input before sending; uncertain sends retain their debit."""
        from .input_budget import InputBudgetExceeded
        if type(size) is not int or size < 0 or type(limit) is not int or limit < 1:
            _error("invalid prompt input reservation")
        safe_name(request_id)
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id or run["state"] != "running" or not run.get("session_id"):
                _error("prompt input requires the running session's supervisor", 3)
            reservations = run.setdefault("input_reservations", {})
            previous = reservations.get(request_id)
            if previous is not None:
                if previous["requested_bytes"] != size or previous["limit_bytes"] != limit:
                    _error("prompt reservation identity reused with different contents")
                return copy.deepcopy(previous)
            history = [other for other in state["runs"].values()
                       if other["id"] != run_id and all(other.get(key) == run[key] for key in
                                                       ("agent_sha256", "workspace", "session_id"))]
            if any(not other.get("input_reservations") and
                   other.get("outcome", {}).get("prompt_started") is not False for other in history):
                raise InputBudgetExceeded("session_history_unknown", limit=limit, used=None, requested=size)
            used = sum(item["requested_bytes"]
                       for other in state["runs"].values()
                       if all(other.get(key) == run[key] for key in
                              ("agent_sha256", "workspace", "session_id"))
                       for item in other.get("input_reservations", {}).values())
            if used + size > limit:
                raise InputBudgetExceeded("session", limit=limit, used=used, requested=size)
            result = {"requested_bytes": size, "used_bytes": used + size, "limit_bytes": limit}
            reservations[request_id] = result
            self._event(state, "prompt_input_reserved", run_id, {"request_id": request_id, **result})
            return copy.deepcopy(result)

    def record_process(self, run_id: str, *, owner_id: str, identity: dict) -> None:
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id or run["state"] != "starting":
                _error("process can only be recorded by the starting run's supervisor", 3)
            if run.get("process") is not None and run["process"] != identity:
                _error("run already has a different process identity")
            if run.get("process") is None:
                run["process"] = copy.deepcopy(identity)
                self._event(state, "process_recorded", run_id, identity)

    def workspace_plan(self, run_id: str, *, owner_id: str, plan: dict) -> None:
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id or run["state"] != "starting":
                _error("workspace preparation requires the starting run's supervisor", 3)
            run["workspace_plan"] = copy.deepcopy(plan)
            self._event(state, "workspace_preparing", run_id, plan)

    def workspace_ready(self, run_id: str, *, owner_id: str, path: str, identity: dict) -> dict:
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != owner_id or run["state"] != "starting":
                _error("workspace preparation requires the starting run's supervisor", 3)
            run.update(workspace=path, workspace_identity=identity)
            self._event(state, "workspace_ready", run_id, {"path": path, "identity": identity})
            return {key: copy.deepcopy(value) for key, value in run.items() if key != "token_sha256"}

    def request_control(self, run_id: str, kind: str) -> dict:
        if kind not in {"cancel", "retry"}:
            _error("unknown operator control")
        with self._transaction() as state:
            run = self._run(state, run_id)
            if kind == "retry" and run["state"] not in TERMINAL | {"waiting_auth", "waiting_input"}:
                _error("cancel the active run before requesting retry")
            if kind == "retry":
                if any(item["task_id"] == run["task_id"] and item["status"] in {"prepared", "needs_recovery"}
                       for item in state.get("maintenance", {}).values()):
                    _error("resolve the pending maintenance operation before retrying")
                if any(item["run_id"] == run_id and item["status"] in {"queued", "starting", "running", "needs_recovery"}
                       for item in state.get("commands", {}).values()):
                    _error("resolve the pending command before retrying")
                if any(other["task_id"] == run["task_id"] and other.get("sequence", 0) > run.get("sequence", 0)
                       for other in state["runs"].values()):
                    _error("a newer run exists for this task; inspect and retry the latest run")
                if any(receipt["envelope"]["run_id"] == run_id and receipt["status"] in {"received", "applying", "needs_recovery"}
                       for receipt in state["results"].values()):
                    _error("resolve the pending domain result before retrying")
            control = run.get("control")
            if control and control["status"] != "completed":
                if control["kind"] != kind:
                    _error("another control is pending for this run")
                return copy.deepcopy(control)
            control = {"id": uuid.uuid4().hex, "kind": kind, "status": "pending", "at": self.clock()}
            run["control"] = control
            self._event(state, "control_requested", run_id, control)
            return copy.deepcopy(control)

    def control_status(self, run_id: str, control_id: str, *, completed: bool) -> None:
        """Called by the exclusive supervisor after cancellation/cleanup."""
        with self._transaction() as state:
            run = self._run(state, run_id)
            control = run.get("control")
            if not control or control["id"] != control_id or control["status"] == "completed":
                return
            control["status"] = "completed" if completed else "processing"
            if completed:
                if run["state"] not in TERMINAL:
                    run.update(state="cancelled", reason="operator_cancelled", updated_at=self.clock())
                if control["kind"] == "retry":
                    run["retry_authorized"] = True
            self._event(state, "control_completed" if completed else "control_processing", run_id, control)

    def recover_run(self, run_id: str, *, previous_owner: str, owner_id: str) -> None:
        """Record recovery after the exclusive supervisor proves group cleanup.

        This domain-free operation does not retry commands or apply receipts.
        Authentication/input holds survive restart and still require action.
        """
        with self._transaction() as state:
            run = self._run(state, run_id)
            if run["owner_id"] != previous_owner or run["state"] in TERMINAL:
                return
            previous_state = run["state"]
            run.update(owner_id=owner_id, process=None, updated_at=self.clock())
            if previous_state not in {"waiting_auth", "waiting_input"}:
                run.update(state="interrupted", reason="supervisor_restart")
            self._event(state, "run_recovered", run_id,
                        {"previous_owner": previous_owner, "previous_state": previous_state,
                         "state": run["state"]})

    def submit_decision(self, document: dict, *, run_id: str, token: str) -> dict:
        """Fill mechanical envelope identity from the authenticated assignment.

        The default result identity is stable across delivery retries. Full
        envelopes remain accepted, but supplied identities cannot change the
        credential's assignment. Receipt authentication also permits exact
        duplicate delivery after the original result has already been applied.
        """
        if not isinstance(document, dict) or document.keys() - ResultEnvelope.__dataclass_fields__.keys():
            _error("decision must contain only supported result envelope fields")
        if "run_id" in document and document["run_id"] != run_id:
            _error("result run_id does not match assigned run", 3)
        run = self._run(self.snapshot(), run_id)
        fields = copy.deepcopy(document)
        for name, expected in (("run_id", run_id), ("task_id", run["task_id"]),
                               ("task_revision", run["task_revision"]), ("schema_sha256", run["schema_sha256"])):
            if name in fields and fields[name] != expected:
                _error(f"result {name} does not match assigned run", 3)
            fields[name] = expected
        fields.setdefault("result_id", run_id + "-result")
        try:
            envelope = ResultEnvelope(**fields)
        except TypeError as exc:
            _error(f"incomplete result decision: {exc}")
        return self.receive_result(envelope, token=token)

    def receive_result(self, envelope: ResultEnvelope, *, token: str) -> dict:
        """Authenticate and durably receive a decision for later domain validation.

        Re-delivery of the identical result remains valid after the task moves.
        It returns the existing receipt rather than repeating domain side effects.
        """
        document = envelope.document()
        digest = fingerprint(document)
        with task_lock(self.runtime, envelope.task_id), self._transaction() as state:
            run = self._run(state, envelope.run_id)
            if run.get('conversation_id') and not run.get('conversation_task'):
                _error('conversation has no assigned task for a domain result', 3)
            if not hmac.compare_digest(run["token_sha256"], hashlib.sha256(token.encode()).hexdigest()):
                _error("invalid run credential", 3)
            for name, expected in (("task_id", envelope.task_id),
                                   ("task_revision", envelope.task_revision),
                                   ("schema_sha256", envelope.schema_sha256)):
                if run[name] != expected:
                    _error(f"result {name} does not match assigned run", 3)
            existing = state["results"].get(envelope.result_id)
            if existing:
                if existing["sha256"] != digest:
                    _error("result identity reused with different contents")
                return copy.deepcopy(existing)
            if run["state"] not in {"running", "waiting_input"}:
                _error("run is not accepting new results")
            if any(item["run_id"] == envelope.run_id and item["status"] in {"queued", "starting", "running", "needs_recovery"}
                   for item in state.get("commands", {}).values()):
                _error("resolve the pending command before submitting a result")
            self._check_revision(TaskRevision(run["task_id"], run["task_path"], run["task_revision"]))
            if any(receipt["envelope"]["run_id"] == envelope.run_id for receipt in state["results"].values()):
                _error("run already submitted a result")
            receipt = {"status": "received", "sha256": digest, "envelope": document,
                       "role": run["role"], "at": self.clock()}
            state["results"][envelope.result_id] = receipt
            self._event(state, "result_received", run["id"], {"result_id": envelope.result_id})
            return copy.deepcopy(receipt)

    def result_status(self, result_id: str, status: str, *, details: dict | None = None) -> dict:
        """Domain-service acknowledgement; never used to bypass validators."""
        allowed = {"received": {"applying", "rejected"},
                   "applying": {"applied", "needs_recovery"}}
        with self._transaction() as state:
            receipt = state["results"].get(result_id)
            if receipt is None:
                _error("unknown result identity")
            if receipt["status"] == status:
                return copy.deepcopy(receipt)
            if status not in allowed.get(receipt["status"], set()):
                _error(f"illegal result transition {receipt['status']} -> {status}")
            receipt.update(status=status, details=details or {}, updated_at=self.clock())
            self._event(state, f"result_{status}", receipt["envelope"]["run_id"],
                        {"result_id": result_id, "details": details or {}})
            return copy.deepcopy(receipt)
