"""Apply run-bound decisions through existing task gates with crash recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import atomic_bytes, atomic_json, durable_move, file_lock, safe_name, task_lock
from greatminds.core.util import now_iso
from greatminds.runtime.store import RunStore, TERMINAL, TaskRevision


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ResultService:
    def __init__(self, store: RunStore, *, checkpoint: Callable[[str], None] | None = None):
        self.store = store
        self.checkpoint = checkpoint or (lambda stage: None)

    def reconcile(self) -> None:
        snapshot = self.store.snapshot()
        for result_id, receipt in snapshot["results"].items():
            run = snapshot["runs"][receipt["envelope"]["run_id"]]
            if (receipt["status"] in {"received", "applying"}
                    and run["state"] in TERMINAL | {"waiting_input", "waiting_auth"}):
                self.apply(result_id)

    def _prepare(self, receipt: dict, run: dict, contract: dict) -> dict:
        from greatminds.cli import task as policy
        from greatminds.cli.worktree import load_worktree_policy

        envelope = receipt["envelope"]
        payload = envelope["payload"]
        decision = envelope["decision"]
        allowed = {
            "handoff": {"to_queue", "blocks", "artifacts", "reason"},
            "blocked": {"reason", "dependencies", "resume_to", "artifacts"},
            "needs_input": {"question", "artifacts"},
            "no_change": {"reason", "artifacts"},
        }
        if set(payload) - allowed[decision]:
            raise GreatMindsError("unknown fields in decision payload", exit_code=2)
        revision = TaskRevision(run["task_id"], run["task_path"], run["task_revision"])
        self.store._check_revision(revision)
        source = self.store.runtime / revision.path
        before = source.read_bytes()
        data = yaml.safe_load(before)
        if not isinstance(data, dict) or data.get("id") != run["task_id"]:
            raise GreatMindsError("task header identity does not match assigned task", exit_code=2)
        source_queue = revision.path.split("/")[0]
        target = payload.get("to_queue") if decision == "handoff" else source_queue
        if decision == "blocked":
            target = "feature_blocked"
        if not isinstance(target, str) or target not in contract["queues"]:
            raise GreatMindsError("decision has an unknown destination queue", exit_code=2)
        if decision == "handoff" and target == source_queue:
            raise GreatMindsError("handoff must change queues", exit_code=2)
        if decision == "needs_input" and not str(payload.get("question") or "").strip():
            raise GreatMindsError("needs_input requires a question", exit_code=2)
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
            raise GreatMindsError("artifacts must be an array of workspace paths", exit_code=2)
        workspace = Path(run["workspace"])
        artifact_records = []
        for item in artifacts:
            path = (workspace / item).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                raise GreatMindsError("artifact must be an existing file in the assigned workspace", exit_code=2)
            artifact_records.append({"path": str(path), "sha256": _digest(path.read_bytes())})
        at = now_iso()
        blocks = payload.get("blocks", [])
        if decision == "blocked":
            blocks = [{"kind": "blocked", "reason": payload.get("reason"),
                       "dependencies": payload.get("dependencies"), "resume_to": payload.get("resume_to")}]
        if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
            raise GreatMindsError("blocks must be an array of typed mappings", exit_code=2)
        with policy.domain_context(document=contract, runtime=self.store.runtime, workspace=workspace):
            for raw in blocks:
                fields = dict(raw)
                kind = fields.pop("kind", None)
                if not isinstance(kind, str):
                    raise GreatMindsError("each block requires a kind", exit_code=2)
                if "worktree_fingerprint" in fields:
                    raise GreatMindsError("worktree fingerprint is recorded by the daemon", exit_code=3)
                names = {"plan": ("written_by", "written_at"),
                         "implementation": ("closed_by", "closed_at"),
                         "tests": ("closed_by", "closed_at"),
                         "reader_review": ("reviewed_by", "reviewed_at"),
                         "review": ("reviewed_by", "reviewed_at"),
                         "blocked": ("blocked_by", "blocked_at")}.get(kind)
                if names:
                    if names[0] in fields and fields[names[0]] != run["role"]:
                        raise GreatMindsError("block cannot impersonate another role", exit_code=3)
                    fields[names[0]], fields[names[1]] = run["role"], at
                data = policy.prepare_block_candidate(data=data, queue=source_queue, coord=self.store.runtime,
                                                      task_id=run["task_id"], role=run["role"], kind=kind,
                                                      fields=fields, at=at)
                data["blocks"][-1]["provenance"] = {
                    "run_id": run["id"], "result_id": envelope["result_id"],
                    "task_revision": revision.sha256, "schema_sha256": run["schema_sha256"],
                    "applied_by": "SYSTEM"}
            policy.validate_task(data)
            if target != source_queue:
                error = policy.can_role_move(run["role"], source_queue, target, data)
                if error:
                    raise GreatMindsError(error, exit_code=3)
                policy.require_scope_match_on_routing(data, source_queue, target)
                policy.require_target_readiness(data, source_queue, target)
                policy.enforce_schema_requires(data, run["role"], source_queue, target)
        destination = f"{target}/{source.name}"
        if target != source_queue and (self.store.runtime / destination).exists():
            raise GreatMindsError("destination task already exists", exit_code=4)
        after = (yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode()
                 if blocks else before)
        worktree_policy = load_worktree_policy(self.store.runtime.parent, schema_document=contract)
        required = data.get("kind") in worktree_policy.required_for_task_kinds
        pre, post = [], []
        if target != source_queue:
            if required and target in {"feature_dev", "feature_ui_dev", "feature_docs"}:
                pre.append("create")
            if required and target == "verified" and worktree_policy.cleanup_on_verified:
                pre.append("merge")
                post.append("remove")
            if target == "archive" and worktree_policy.cleanup_on_archive and (
                    required or data.get("stream") == "review_session"):
                post.append("remove")
        return {"version": 1, "result_id": envelope["result_id"], "run_id": run["id"],
                "task_id": run["task_id"], "source": revision.path, "destination": destination,
                "before_sha256": _digest(before), "after_sha256": _digest(after),
                "after": after.decode(), "phase": "prepared", "pre": pre, "post": post,
                "action_started": None, "actions_done": [], "worktree_policy": asdict(worktree_policy),
                "at": at, "role": run["role"], "decision": decision, "artifacts": artifact_records,
                "question": payload.get("question"), "schema_sha256": run["schema_sha256"]}

    def _action(self, operation: dict, kind: str) -> None:
        from greatminds.cli.worktree import WorktreePolicy, worktree_create, worktree_merge, worktree_remove

        policy = WorktreePolicy(**operation["worktree_policy"])
        project, task_id = self.store.runtime.parent, operation["task_id"]
        if kind == "create":
            worktree_create(project, task_id, policy=policy)
        elif kind == "merge":
            result = worktree_merge(project, task_id, summary=f"review({task_id})", policy=policy)
            if not result.ok:
                raise GreatMindsError("reviewed worktree could not be merged", exit_code=4)
        elif kind == "remove":
            worktree_remove(project, task_id, force=False, policy=policy)
            if policy.worktree_path_for(project, task_id).exists():
                raise GreatMindsError("worktree cleanup did not remove the task workspace", exit_code=4)

    def _actions(self, path: Path, operation: dict, stage: str, *, recovered: bool) -> None:
        for index, kind in enumerate(operation[stage]):
            action_id = f"{stage}-{index}-{kind}"
            if action_id in operation["actions_done"]:
                continue
            if recovered and operation["action_started"] == action_id and kind == "merge":
                raise GreatMindsError("merge completion is uncertain; reconcile git state before continuing", exit_code=4)
            operation["action_started"] = action_id
            atomic_json(path, operation)
            self.checkpoint("action_started")
            self._action(operation, kind)
            operation["actions_done"].append(action_id)
            operation["action_started"] = None
            atomic_json(path, operation)

    def _journal(self, operation: dict) -> None:
        from greatminds.cli.task import journal_append

        journal_append(self.store.runtime, {"t": operation["at"], "actor": "SYSTEM",
            "actor_kind": "system", "decision_by": operation["role"], "task": operation["task_id"],
            "from": operation["source"].split("/")[0], "to": operation["destination"].split("/")[0],
            "reason": f"apply {operation['decision']} result", "intent_id": operation["result_id"],
            "operation_id": operation["result_id"], "run_id": operation["run_id"],
            "schema_sha256": operation["schema_sha256"]})

    def _recheck_gates(self, operation: dict, run: dict) -> None:
        from greatminds.cli import task as policy

        contract = yaml.safe_load(self.store.contracts(run["id"])["schema"]["text"])
        candidate = yaml.safe_load(operation["after"])
        source = operation["source"].split("/")[0]
        destination = operation["destination"].split("/")[0]
        with policy.domain_context(document=contract, runtime=self.store.runtime, workspace=Path(run["workspace"])):
            policy.validate_task(candidate)
            if source != destination:
                error = policy.can_role_move(run["role"], source, destination, candidate)
                if error:
                    raise GreatMindsError(error, exit_code=3)
                policy.require_scope_match_on_routing(candidate, source, destination)
                policy.require_target_readiness(candidate, source, destination)
                policy.enforce_schema_requires(candidate, run["role"], source, destination)

    def apply(self, result_id: str) -> dict:
        safe_name(result_id)
        initial = self.store.snapshot()["results"][result_id]
        with task_lock(self.store.runtime, initial["envelope"]["task_id"]):
            snapshot = self.store.snapshot()
            receipt = snapshot["results"][result_id]
            if receipt["status"] not in {"received", "applying"}:
                return receipt
            run = snapshot["runs"][receipt["envelope"]["run_id"]]
            if run["state"] not in TERMINAL | {"waiting_input", "waiting_auth"}:
                return receipt  # Do not mutate the assignment while its process is working.
            path = self.store.directory / "operations" / f"{result_id}.json"
            recovered = path.exists()
            if recovered:
                operation = json.loads(path.read_text())
            else:
                try:
                    contract = yaml.safe_load(self.store.contracts(run["id"])["schema"]["text"])
                    operation = self._prepare(receipt, run, contract)
                except (GreatMindsError, yaml.YAMLError) as exc:
                    status = "rejected" if receipt["status"] == "received" else "needs_recovery"
                    return self.store.result_status(result_id, status, details={"error": str(exc)})
                atomic_json(path, operation)
                self.checkpoint("prepared")
            if (operation.get("version") != 1 or operation.get("result_id") != result_id
                    or operation.get("run_id") != run["id"] or operation.get("task_id") != run["task_id"]
                    or operation.get("source") != run["task_path"]):
                raise GreatMindsError("operation identity does not match its result", exit_code=4)
            destination_parts = Path(operation["destination"]).parts
            if (len(destination_parts) != 2 or destination_parts[1] != f"{run['task_id']}.yaml"
                    or destination_parts[0].startswith(".")):
                raise GreatMindsError("invalid operation destination", exit_code=4)
            safe_name(destination_parts[0])
            if receipt["status"] == "received":
                self.store.result_status(result_id, "applying")
            source, destination = self.store.runtime / operation["source"], self.store.runtime / operation["destination"]
            try:
                if operation["phase"] != "committed":
                    if source.exists():
                        if _digest(source.read_bytes()) not in {operation["before_sha256"], operation["after_sha256"]}:
                            raise GreatMindsError("task changed before result recovery", exit_code=4)
                        if source != destination and destination.exists():
                            raise GreatMindsError("destination already contains a task", exit_code=4)
                        # Live dependencies/stand gates can change while a
                        # prepared operation is waiting for crash recovery.
                        self._recheck_gates(operation, run)
                    elif not destination.is_file() or _digest(destination.read_bytes()) != operation["after_sha256"]:
                        raise GreatMindsError("task missing before result recovery", exit_code=4)
                    for artifact in operation["artifacts"]:
                        if not Path(artifact["path"]).is_file() or _digest(Path(artifact["path"]).read_bytes()) != artifact["sha256"]:
                            raise GreatMindsError("artifact changed before result application", exit_code=4)
                    self._actions(path, operation, "pre", recovered=recovered)
                    after = operation["after"].encode()
                    if _digest(after) != operation["after_sha256"]:
                        raise GreatMindsError("operation content hash mismatch", exit_code=4)
                    if source.exists():
                        current = _digest(source.read_bytes())
                        if current == operation["before_sha256"]:
                            if current != operation["after_sha256"]:
                                atomic_bytes(source, after)
                            self.checkpoint("task_written")
                        elif current != operation["after_sha256"]:
                            raise GreatMindsError("task changed during result application", exit_code=4)
                        if source != destination:
                            durable_move(source, destination)
                        self.checkpoint("task_moved")
                    elif not destination.is_file() or _digest(destination.read_bytes()) != operation["after_sha256"]:
                        raise GreatMindsError("task missing after interrupted result application", exit_code=4)
                    self._journal(operation)
                    self.checkpoint("journaled")
                    self._actions(path, operation, "post", recovered=recovered)
                    operation["phase"] = "committed"
                    atomic_json(path, operation)
                    self.checkpoint("committed")
            except GreatMindsError as exc:
                return self.store.result_status(result_id, "needs_recovery", details={"error": str(exc)})
            return self.store.result_status(result_id, "applied", details={
                "destination": operation["destination"], "decision": operation["decision"],
                "artifacts": operation["artifacts"], "question": operation["question"]})
