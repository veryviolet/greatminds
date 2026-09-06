"""Journaled system operations for mechanically decidable task maintenance."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import durable_move, safe_name, task_lock
from greatminds.core.util import now_iso
from greatminds.runtime.config import fingerprint
from greatminds.runtime.store import TERMINAL, TaskRevision
from .dependencies import inspect_dependencies, latest_blocked


PENDING = frozenset({"prepared", "needs_recovery"})


class MaintenanceService:
    def __init__(self, store, schema, *, environment=None, checkpoint=None):
        self.store, self.schema, self.environment = store, schema, environment
        self.checkpoint = checkpoint or (lambda stage: None)

    def _busy(self, snapshot, task_id, *, operation_id=None):
        if any(run["task_id"] == task_id and run["state"] not in TERMINAL for run in snapshot["runs"].values()):
            return "active_run"
        if any(receipt["envelope"]["task_id"] == task_id and receipt["status"] in {"received", "applying", "needs_recovery"}
               for receipt in snapshot["results"].values()):
            return "domain_result_unresolved"
        if any(item["task_id"] == task_id and item["status"] in {"queued", "starting", "running", "needs_recovery"}
               for item in snapshot.get("commands", {}).values()):
            return "command_unresolved"
        if any(item["task_id"] == task_id and item["id"] != operation_id and item["status"] in PENDING
               for item in snapshot.get("maintenance", {}).values()):
            return "maintenance_unresolved"
        return None

    def _gate(self, data, destination, document):
        from greatminds.cli import task as policy
        from greatminds.cli.worktree import load_worktree_policy

        rule = document.get("system_transitions", {}).get("resume_dependencies")
        if rule != {"from": "feature_blocked", "to": "declared_resume_to",
                    "requires": ["terminal_dependencies", "declared_resume", "task_readiness"]}:
            raise GreatMindsError("effective schema does not authorize this system resume policy")
        block = latest_blocked(data)
        if not block or block.get("resume_to") != destination:
            raise GreatMindsError("system resume must use the latest declared resume_to")
        if policy._blocked_withdrawn_reason_error(data) is None:
            raise GreatMindsError("withdrawn work requires an operator decision")
        worktrees = load_worktree_policy(self.store.runtime.parent, schema_document=document)
        workspace = worktrees.worktree_path_for(self.store.runtime.parent, data["id"])
        if not workspace.is_dir():
            workspace = self.store.runtime.parent
        with policy.domain_context(document=document, runtime=self.store.runtime, workspace=workspace.resolve(),
                                   environment=self.environment):
            policy.validate_task(data)
            if destination not in document["queues"] or policy._is_terminal_queue(destination) or destination == "feature_blocked":
                raise GreatMindsError("system resume requires a different active destination queue")
            policy.require_scope_match_on_routing(data, "feature_blocked", destination)
            policy.require_target_readiness(data, "feature_blocked", destination)

    def inspect(self):
        report = inspect_dependencies(self.store.runtime, self.schema.document, environment=self.environment)
        snapshot = self.store.snapshot()
        for item in report["tasks"].values():
            busy = self._busy(snapshot, item["task_id"])
            if busy:
                item.update(status=busy, reasons=[{"code": busy}])
            elif item["status"] == "ready":
                try:
                    previous = snapshot.get("maintenance", {}).get(self._operation_id(item))
                    if previous:
                        raise GreatMindsError("this exact system resume was already applied or abandoned; inspect the task history")
                    data = yaml.safe_load((self.store.runtime / item["path"]).read_text())
                    self._gate(data, item["resume_to"], self.schema.document)
                except (GreatMindsError, OSError, TypeError, AttributeError, yaml.YAMLError) as exc:
                    item.update(status="gate_failed", reasons=[{"code": "task_readiness", "message": str(exc)}])
        report["schema_sha256"] = self.schema.sha256
        return report

    def _operation_id(self, finding):
        return "resume-" + fingerprint({"task_revision": finding["task_revision"],
                                       "dependencies": finding["dependencies"], "schema": self.schema.sha256})

    def _save_findings(self, report):
        with self.store._transaction() as state:
            if state.get("maintenance_findings") != report:
                state["maintenance_findings"] = report
                self.store._event(state, "maintenance_findings_changed", None,
                                  {"sha256": fingerprint(report), "tasks": len(report["tasks"])})

    def reconcile(self):
        # Finish recorded operations before inspecting newly ready work.
        for operation in self.store.snapshot().get("maintenance", {}).values():
            if operation["status"] == "prepared":
                self.apply(operation["id"])
        report = self.inspect()
        for item in report["tasks"].values():
            if item["status"] == "ready":
                self.resume(item)
        self._save_findings(self.inspect())

    def resume(self, finding):
        task_id = safe_name(finding["task_id"])
        ids = sorted({task_id, *(dep["task_id"] for dep in finding["dependencies"])})
        with ExitStack() as locks:
            for locked_id in ids:
                locks.enter_context(task_lock(self.store.runtime, locked_id))
            report = inspect_dependencies(self.store.runtime, self.schema.document, environment=self.environment)
            current = report["tasks"].get(task_id)
            if not current or current["status"] != "ready" or current.get("task_revision") != finding.get("task_revision"):
                return None
            if sorted({task_id, *(dep["task_id"] for dep in current["dependencies"])}) != ids:
                return None
            path = self.store.runtime / current["path"]
            data = yaml.safe_load(path.read_text())
            self._gate(data, current["resume_to"], self.schema.document)
            destination = f"{current['resume_to']}/{path.name}"
            operation_id = self._operation_id(current)
            with self.store._transaction() as state:
                if self._busy(state, task_id):
                    return None
                if operation_id in state.get("maintenance", {}):
                    return None
                if (self.store.runtime / destination).exists():
                    raise GreatMindsError("system resume destination already exists", exit_code=4)
                self.store._persist_contract("schema", self.schema.sha256,
                                             {"text": self.schema.text, "version": self.schema.version})
                operation = {"id": operation_id, "kind": "resume_dependencies", "status": "prepared",
                             "task_id": task_id, "task_revision": current["task_revision"],
                             "source": current["path"], "destination": destination,
                             "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "schema_sha256": self.schema.sha256, "dependencies": current["dependencies"],
                             "blocked_by": current["blocked_by"], "at": now_iso()}
                state.setdefault("maintenance", {})[operation_id] = operation
                self.store._event(state, "system_operation_prepared", None, {"operation_id": operation_id, "task_id": task_id})
            self.checkpoint("prepared")
        return self.apply(operation_id)

    def _contract(self, operation):
        path = self.store.directory / "contracts" / f"schema-{operation['schema_sha256']}.json"
        document = json.loads(path.read_text())
        if hashlib.sha256(document["text"].encode()).hexdigest() != operation["schema_sha256"]:
            raise GreatMindsError("system operation schema identity mismatch", exit_code=4)
        return yaml.safe_load(document["text"])

    def apply(self, operation_id):
        from greatminds.cli.task import journal_append

        safe_name(operation_id)
        operation = self.store.snapshot().get("maintenance", {}).get(operation_id)
        if not operation or operation["status"] != "prepared":
            return operation
        ids = sorted({operation["task_id"], *(dep["task_id"] for dep in operation["dependencies"])})
        with ExitStack() as locks:
            for task_id in ids:
                locks.enter_context(task_lock(self.store.runtime, task_id))
            snapshot = self.store.snapshot()
            operation = snapshot["maintenance"][operation_id]
            if operation["status"] != "prepared":
                return operation
            if self._busy(snapshot, operation["task_id"], operation_id=operation_id):
                return operation
            source = self.store.runtime / operation["source"]
            destination = self.store.runtime / operation["destination"]
            try:
                document = self._contract(operation)
                if (operation.get("kind") != "resume_dependencies"
                        or operation["source"] != f"feature_blocked/{operation['task_id']}.yaml"
                        or len(Path(operation["destination"]).parts) != 2
                        or destination.name != f"{operation['task_id']}.yaml"
                        or destination.parent.name not in document["queues"]
                        or destination.absolute() != destination.resolve()):
                    raise GreatMindsError("invalid system operation identity or destination")
                if source.exists():
                    self.store._check_revision(TaskRevision(operation["task_id"], operation["source"], operation["task_revision"]))
                    if destination.exists():
                        raise GreatMindsError("system resume destination already contains a task")
                    report = inspect_dependencies(self.store.runtime, document, environment=self.environment)
                    current = report["tasks"].get(operation["task_id"])
                    if not current or current["status"] != "ready" or current["dependencies"] != operation["dependencies"]:
                        raise GreatMindsError("dependency evidence changed before system resume")
                    data = yaml.safe_load(source.read_text())
                    self._gate(data, destination.parent.name, document)
                    durable_move(source, destination)
                    self.checkpoint("task_moved")
                elif (not destination.is_file() or destination.is_symlink()
                      or hashlib.sha256(destination.read_bytes()).hexdigest() != operation["content_sha256"]):
                    raise GreatMindsError("task changed during system operation recovery")
                journal_append(self.store.runtime, {"t": operation["at"], "actor": "SYSTEM", "actor_kind": "system",
                    "task": operation["task_id"], "from": "feature_blocked", "to": destination.parent.name,
                    "reason": "declared terminal dependencies satisfied", "operation_id": operation_id,
                    "schema_sha256": operation["schema_sha256"], "blocked_by": operation["blocked_by"],
                    "dependency_evidence": operation["dependencies"]})
                self.checkpoint("journaled")
                status, details = "applied", {}
            except (GreatMindsError, OSError, ValueError, yaml.YAMLError) as exc:
                status, details = "needs_recovery", {"error": str(exc)}
            with self.store._transaction() as state:
                record = state["maintenance"][operation_id]
                record.update(status=status, details=details, updated_at=self.store.clock())
                self.store._event(state, "system_operation_" + status, None, {"operation_id": operation_id})
                return copy.deepcopy(record)

    def request_repair(self, operation_id):
        """Retry reconciliation only; validation and source hashes remain mandatory."""
        with self.store._transaction() as state:
            operation = state.get("maintenance", {}).get(operation_id)
            if not operation or operation["status"] != "needs_recovery":
                raise GreatMindsError("operation is not awaiting recovery")
            operation.update(status="prepared", repair_requested_at=self.store.clock())
            self.store._event(state, "system_repair_requested", None, {"operation_id": operation_id})
            return copy.deepcopy(operation)

    def abandon(self, operation_id, *, reason):
        """Cancel an uncommitted intent without discarding either task file."""
        if not isinstance(reason, str) or not reason.strip():
            raise GreatMindsError("abandoning a system intent requires an operator explanation")
        operation = self.store.snapshot().get("maintenance", {}).get(operation_id)
        if not operation:
            raise GreatMindsError("unknown system operation")
        with task_lock(self.store.runtime, operation["task_id"]), self.store._transaction() as state:
            current = state["maintenance"][operation_id]
            if current["status"] != "needs_recovery":
                raise GreatMindsError("only an unresolved system intent can be abandoned")
            source, destination = self.store.runtime / current["source"], self.store.runtime / current["destination"]
            if not source.is_file() or source.is_symlink() or destination.exists():
                raise GreatMindsError("cannot abandon an intent whose task may already have moved; reconcile it")
            current.update(status="abandoned", resolution=reason, updated_at=self.store.clock())
            self.store._event(state, "system_operation_abandoned", None, {"operation_id": operation_id, "reason": reason})
            return copy.deepcopy(current)
