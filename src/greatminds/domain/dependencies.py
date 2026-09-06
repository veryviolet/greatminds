"""Read-only dependency graph and ACP live-role gates shared by daemon and CLI."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.runtime.config import load_execution_config
from greatminds.runtime.processes import process_identity
from greatminds.runtime.store import RunStore, TaskRevision


DEPENDENCY = re.compile(r"^(?P<queue>[a-z_]+)/(?P<id>[0-9]{1,4}-[a-z0-9-]+)\.(?:yaml|md)$")


def latest_blocked(data):
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        return None
    return next((block for block in reversed(blocks)
                 if isinstance(block, dict) and block.get("kind") == "blocked"), None)


def cycle_components(graph):
    """Iterative strongly connected components; safe for deep dependency chains."""
    visited, order = set(), []
    for start in sorted(graph):
        if start in visited:
            continue
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                order.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend((neighbor, False) for neighbor in reversed(sorted(graph.get(node, [])))
                             if neighbor not in visited)
    reverse = {node: [] for node in visited}
    for node, neighbors in graph.items():
        for neighbor in neighbors:
            reverse[neighbor].append(node)
    visited, cycles = set(), []
    for start in reversed(order):
        if start in visited:
            continue
        members, stack = [], [start]
        visited.add(start)
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in reverse[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        if len(members) > 1 or start in graph.get(start, []):
            cycles.append(sorted(members))
    return sorted(cycles)


def live_role_holds(runtime, data, block, schema, *, environment=None):
    required = block.get("requires_live_roles", data.get("requires_live_roles", []))
    if not required:
        return []
    if not isinstance(required, list) or any(not isinstance(role, str) for role in required):
        return [{"code": "invalid_live_roles", "message": "requires_live_roles must be an array of role names"}]
    context = block.get("requires_live_roles_context", data.get("requires_live_roles_context"))
    if context is not None and (not isinstance(context, str) or not context.strip()):
        return [{"code": "invalid_live_context", "message": "live-role context must be a project/runtime path"}]
    from greatminds.core.paths import live_roles_runtime
    try:
        target = live_roles_runtime(runtime, context)
    except GreatMindsError:
        return [{"code": "live_context_unavailable", "context": context}]
    config_path = target.parent / "coordination" / "execution.yaml"
    try:
        config = load_execution_config(config_path, roles=set(schema.get("roles", {})))
        snapshot = RunStore(target).snapshot()
    except GreatMindsError:
        return [{"code": "live_context_unavailable", "context": str(target)}]
    source = os.environ if environment is None else environment
    holds = []
    for role in required:
        bindings = [binding for binding in config.bindings if binding.role == role]
        if not bindings:
            holds.append({"code": "required_role_unconfigured", "role": role, "context": str(target)})
            continue
        candidates = [run for run in snapshot["runs"].values() if run["role"] == role]
        running = [run for run in candidates if run["state"] == "running" and run.get("process")]
        try:
            live = any(process_identity(run["process"]["pid"]) == run["process"] for run in running)
        except (OSError, ValueError):
            live = False
        if live:
            continue
        if any(run["state"] == "waiting_auth" for run in candidates) or (
                target == runtime and all(any(not source.get(name) for name in config.agent(binding.agent).required_env)
                                          for binding in bindings)):
            code = "required_role_authentication"
        elif any(run["state"] == "waiting_input" for run in candidates):
            code = "required_role_input"
        else:
            code = "required_role_not_running"
        holds.append({"code": code, "role": role, "context": str(target)})
    return holds


def inspect_dependencies(runtime: Path, schema: dict, *, environment=None) -> dict:
    runtime = runtime.resolve()
    queues = schema.get("queues", {})
    terminal = {name for name, meta in queues.items() if isinstance(meta, dict) and meta.get("kind") == "terminal"}
    locations = {}
    for queue in sorted(queues):
        for path in sorted((runtime / queue).glob("*")):
            if path.suffix in {".yaml", ".md"} and not path.name.startswith("_"):
                locations.setdefault(path.stem, []).append(path)
    findings, graph = {}, {}
    for path in sorted((runtime / "feature_blocked").glob("*")):
        if path.suffix not in {".yaml", ".md"} or path.name.startswith("_"):
            continue
        task_id = path.stem
        item = {"task_id": task_id, "path": str(path.relative_to(runtime)), "status": "ready",
                "resume_to": None, "dependencies": [], "reasons": []}
        findings[task_id] = item
        graph[task_id] = []
        if path.suffix != ".yaml":
            item.update(status="migration_required", reasons=[{"code": "legacy_task_format"}])
            continue
        try:
            revision = TaskRevision.capture(runtime, path)
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict) or data.get("id") != task_id:
                raise ValueError("task identity does not match its filename")
            item["task_revision"] = revision.sha256
        except (GreatMindsError, OSError, ValueError, yaml.YAMLError):
            item.update(status="malformed", reasons=[{"code": "invalid_task"}])
            continue
        if len(locations.get(task_id, [])) != 1:
            item.update(status="malformed", reasons=[{"code": "duplicate_task_identity"}])
            continue
        block = latest_blocked(data)
        if block is None:
            item.update(status="malformed", reasons=[{"code": "missing_blocked_block"}])
            continue
        item["resume_to"] = block.get("resume_to") if isinstance(block.get("resume_to"), str) else None
        author = block.get("by", block.get("blocked_by"))
        item["blocked_by"] = author if isinstance(author, str) else None
        deps = block.get("dependencies")
        if (not isinstance(item["resume_to"], str) or item["resume_to"] not in queues
                or item["resume_to"] in terminal | {"feature_blocked"}
                or not isinstance(deps, list) or not deps):
            item.update(status="malformed", reasons=[{"code": "invalid_resume_or_dependencies"}])
            continue
        for dep in deps:
            match = DEPENDENCY.fullmatch(dep) if isinstance(dep, str) else None
            record = {"declared": dep if isinstance(dep, str) else repr(dep), "status": "malformed"}
            item["dependencies"].append(record)
            if not match or match["queue"] not in queues:
                continue
            dep_id, expected = match["id"], match["queue"]
            found = locations.get(dep_id, [])
            record.update(task_id=dep_id, expected_queue=expected, actual_queues=[p.parent.name for p in found])
            if any(p.parent.name == "feature_blocked" for p in found):
                graph[task_id].append(dep_id)
            if len(found) > 1:
                record["status"] = "ambiguous"
            elif not found:
                record["status"] = "missing"
            elif found[0].parent.name != expected:
                record["status"] = "wrong_terminal" if found[0].parent.name in terminal else "waiting"
            elif expected not in terminal:
                record["status"] = "active_dependency"
            elif found[0].suffix != ".yaml":
                record["status"] = "migration_required"
            else:
                try:
                    dep_revision = TaskRevision.capture(runtime, found[0])
                    target_data = yaml.safe_load(found[0].read_text())
                    if not isinstance(target_data, dict) or target_data.get("id") != dep_id:
                        raise ValueError("dependency identity mismatch")
                    record.update(status="satisfied", path=dep_revision.path, task_revision=dep_revision.sha256)
                except (GreatMindsError, OSError, ValueError, yaml.YAMLError):
                    record["status"] = "malformed"
        failures = [dep for dep in item["dependencies"] if dep["status"] != "satisfied"]
        if failures:
            states = {dep["status"] for dep in failures}
            item["status"] = ("malformed" if states & {"malformed", "ambiguous"} else
                              "wrong_terminal" if "wrong_terminal" in states else "waiting")
            item["reasons"] = [{"code": dep["status"], "dependency": dep["declared"],
                                "expected_queue": dep.get("expected_queue"),
                                "actual_queues": dep.get("actual_queues", [])} for dep in failures]
        else:
            holds = live_role_holds(runtime, data, block, schema, environment=environment)
            if holds:
                item.update(status="live_role_hold", reasons=holds)
        if any(word in str(block.get("reason", "")).lower() for word in ("withdrawn", "abandoned", "obsoleted")):
            item.update(status="withdrawn", reasons=[{"code": "operator_withdrawal"}])
    cycles = cycle_components(graph)
    for component in cycles:
        for task_id in component:
            if task_id in findings:
                findings[task_id].update(status="cycle", reasons=[{"code": "dependency_cycle", "tasks": component}])
    return {"version": 1, "tasks": findings, "cycles": cycles}
