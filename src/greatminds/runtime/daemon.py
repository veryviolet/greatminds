"""Deterministic ACP dispatch for projects with an execution contract."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from .config import load_execution_config
from .context import compile_context
from .store import RunStore, TERMINAL, TaskRevision
from .supervisor import Supervisor
from .processes import terminate_group


def assignments(store, config, schema):
    """Explain each concrete assignment without creating processes or claims."""
    snapshot = store.snapshot()
    runs = list(snapshot["runs"].values())
    active = [run for run in runs if run["state"] not in TERMINAL]
    for binding in config.bindings:
        if binding.scheduling != "queue":
            continue
        role = schema.document["roles"][binding.role]
        for queue in role.get("claims_from", []):
            # Mechanical dependency release belongs to the domain controller.
            if queue == "feature_blocked":
                continue
            for path in sorted((store.runtime / queue).glob("*.yaml")):
                try:
                    task = TaskRevision.capture(store.runtime, path)
                except (OSError, GreatMindsError):
                    continue
                previous = [run for run in runs if run["task_id"] == task.task_id
                            and run["task_revision"] == task.sha256]
                latest = max(previous, key=lambda run: (run["created_at"], run.get("sequence", 0))) if previous else None
                reason = "ready"
                if snapshot["paused"]:
                    reason = "dispatch_paused"
                elif any(run["task_id"] == task.task_id for run in active):
                    reason = "active_run"
                elif any(run["account"] == binding.account and run["state"] == "waiting_auth" for run in active):
                    reason = "account_authentication_required"
                elif previous and not latest.get("retry_authorized"):
                    # An unchanged revision does not create an infinite LLM
                    # loop, including after restart. Explicit retry is added
                    # through operator controls, not inferred from prose.
                    reason = "result_pending" if any(
                        receipt["envelope"]["run_id"] in {run["id"] for run in previous}
                        for receipt in snapshot["results"].values()) else "revision_already_attempted"
                elif len(active) >= config.max_running:
                    reason = "project_capacity"
                elif sum(run["binding_id"] == binding.id for run in active) >= binding.max_running:
                    reason = "binding_capacity"
                elif sum(run["account"] == binding.account for run in active) >= dict(config.account_limits).get(binding.account, config.max_running):
                    reason = "account_capacity"
                yield binding, task, reason


async def serve(project: Path, *, interval: float = 1, once: bool = False,
                environment: dict | None = None):
    schema = load_schema_snapshot()
    config = load_execution_config(project / "coordination" / "execution.yaml",
                                   roles=set(schema.document["roles"]))
    store = RunStore(project_runtime_dir(project))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    try:
        if not once:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
        async with Supervisor(project=project, store=store, schema=schema,
                              config=config, environment=environment) as supervisor:
            active: dict[str, asyncio.Task] = {}
            try:
                while not stop.is_set():
                    for run_id, future in list(active.items()):
                        if future.done():
                            future.result()  # Surface persistence failures; do not silently retry.
                            del active[run_id]
                    for run in store.snapshot()["runs"].values():
                        control = run.get("control")
                        if not control or control["status"] == "completed":
                            continue
                        future = active.get(run["id"])
                        if future is not None:
                            if control["status"] == "pending":
                                store.control_status(run["id"], control["id"], completed=False)
                                future.cancel()
                        else:
                            if run.get("process"):
                                await terminate_group(run["process"])
                            store.control_status(run["id"], control["id"], completed=True)
                    for binding, task, reason in assignments(store, config, schema):
                        if reason != "ready":
                            continue
                        try:
                            claim = supervisor.claim(task, binding)
                        except GreatMindsError as exc:
                            if exc.exit_code == 4:
                                raise
                            continue  # Claims recheck capacity and revision under locks.
                        try:
                            prompt = compile_context(store, claim, schema)
                        except (OSError, GreatMindsError):
                            supervisor._transition(claim.run["id"], "interrupted", reason="context_invalid")
                            continue
                        active[claim.run["id"]] = asyncio.create_task(
                            supervisor.execute(claim, binding=binding, prompt=prompt))
                    if once:
                        if active:
                            await asyncio.gather(*active.values())
                        break
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=max(0.2, interval))
                    except TimeoutError:
                        pass
            finally:
                for future in active.values():
                    if not future.done() and not future.cancelling():
                        future.cancel()
                if active:
                    await asyncio.gather(*active.values(), return_exceptions=True)
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
    return store.snapshot()
