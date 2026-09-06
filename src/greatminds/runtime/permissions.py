"""Durable, single-request operator decisions for live ACP callbacks."""

import copy
import json
import re
import uuid

from greatminds.core.errors import GreatMindsError
from .processes import process_identity
from .store import TaskRevision


UNRESOLVED = {"pending", "answered"}


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: "[redacted]" if re.search(r"password|secret|token|authorization|api.?key", key, re.I)
                else redact(item, secrets) for key, item in value.items() if key not in {"_meta", "field_meta"}}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", value)
    return value


class PermissionService:
    def __init__(self, store):
        self.store = store

    def _live(self, state, run_id, owner_id=None):
        run = self.store._run(state, run_id)
        if (run["state"] not in {"running", "waiting_input"}
                or (owner_id is not None and run["owner_id"] != owner_id)
                or (run.get("control") and run["control"]["status"] != "completed")):
            raise GreatMindsError("permission requires the current active run", exit_code=3)
        identity = run.get("process")
        if not identity or process_identity(identity["pid"]) != identity:
            raise GreatMindsError("permission process is no longer live", exit_code=3)
        self.store._check_revision(TaskRevision(run["task_id"], run["task_path"], run["task_revision"]))
        return run

    def create(self, run_id, *, owner_id, session_id, tool, options, secrets=(), timeout=300):
        if len(json.dumps({"tool": tool, "options": options}).encode()) > 262144:
            raise GreatMindsError("permission request exceeds 256 KiB", exit_code=2)
        ids = [option["optionId"] for option in options]
        if not ids or len(set(ids)) != len(ids):
            raise GreatMindsError("permission options must have unique identities", exit_code=2)
        with self.store._transaction() as state:
            run = self._live(state, run_id, owner_id)
            if run.get("session_id") != session_id:
                raise GreatMindsError("permission session does not match the run", exit_code=3)
            request = {"id": uuid.uuid4().hex, "run_id": run_id, "owner_id": owner_id,
                       "session_id": session_id, "task_revision": run["task_revision"],
                       "status": "pending", "created_at": self.store.clock(),
                       "expires_at": self.store.clock() + timeout,
                       "tool": redact(tool, secrets), "options": redact(options, secrets)}
            # Option identities are protocol data, never rewritten by redaction.
            for saved, original in zip(request["options"], options):
                saved["optionId"] = original["optionId"]
                saved["kind"] = original["kind"]
            state.setdefault("permissions", {})[request["id"]] = request
            run.update(state="waiting_input", reason="permission_required", updated_at=self.store.clock())
            self.store._event(state, "permission_requested", run_id, {"request_id": request["id"]})
            return copy.deepcopy(request)

    def get(self, request_id):
        request = self.store.snapshot().get("permissions", {}).get(request_id)
        if request is None:
            raise GreatMindsError("unknown permission request", exit_code=2)
        return request

    def answer(self, request_id, option_id):
        with self.store._transaction() as state:
            request = state.get("permissions", {}).get(request_id)
            if request is None:
                raise GreatMindsError("unknown permission request", exit_code=2)
            run = self._live(state, request["run_id"], request["owner_id"])
            if run.get("session_id") != request["session_id"] or self.store.clock() >= request["expires_at"]:
                raise GreatMindsError("permission session changed or request expired", exit_code=3)
            option = next((item for item in request["options"] if item["optionId"] == option_id), None)
            if option is None:
                raise GreatMindsError("permission option was not offered", exit_code=2)
            if option["kind"] not in {"allow_once", "reject_once"}:
                raise GreatMindsError("persistent permission grants are not supported; choose a one-time option", exit_code=2)
            if request["status"] == "answered" and request["option_id"] == option_id:
                return copy.deepcopy(request)
            if request["status"] != "pending":
                raise GreatMindsError("permission request is no longer pending", exit_code=3)
            request.update(status="answered", option_id=option_id, answered_at=self.store.clock())
            self.store._event(state, "permission_answered", request["run_id"],
                              {"request_id": request_id, "option_id": option_id})
            return copy.deepcopy(request)

    def _release_wait(self, state, run_id):
        run = self.store._run(state, run_id)
        if run["state"] == "waiting_input" and not any(
                item["run_id"] == run_id and item["status"] in UNRESOLVED
                for item in state.get("permissions", {}).values()):
            run.update(state="running", reason="permission_callback_resolved", updated_at=self.store.clock())

    def consume(self, request_id, *, owner_id):
        with self.store._transaction() as state:
            request = state["permissions"][request_id]
            run = self._live(state, request["run_id"], owner_id)
            if run.get("session_id") != request["session_id"] or self.store.clock() >= request["expires_at"]:
                raise GreatMindsError("permission session changed or request expired", exit_code=3)
            if request["owner_id"] != owner_id:
                raise GreatMindsError("permission belongs to another supervisor", exit_code=3)
            if request["status"] == "pending":
                return None
            if request["status"] != "answered":
                raise GreatMindsError("permission response has already been consumed or cancelled", exit_code=3)
            request.update(status="consumed", consumed_at=self.store.clock())
            self._release_wait(state, request["run_id"])
            self.store._event(state, "permission_consumed", request["run_id"], {"request_id": request_id})
            return request["option_id"]

    def cancel(self, request_id, *, owner_id, reason, resume_run=True):
        with self.store._transaction() as state:
            request = state.get("permissions", {}).get(request_id)
            if request is None or request["status"] not in UNRESOLVED:
                return
            if request["owner_id"] != owner_id:
                raise GreatMindsError("permission belongs to another supervisor", exit_code=3)
            request.update(status="cancelled", reason=reason, closed_at=self.store.clock())
            if resume_run:
                self._release_wait(state, request["run_id"])
            self.store._event(state, "permission_cancelled", request["run_id"],
                              {"request_id": request_id, "reason": reason})

    def close_run(self, run_id, *, owner_id, reason, resume_run=False):
        for item in self.store.snapshot().get("permissions", {}).values():
            if item["run_id"] == run_id and item["owner_id"] == owner_id:
                self.cancel(item["id"], owner_id=owner_id, reason=reason, resume_run=resume_run)
