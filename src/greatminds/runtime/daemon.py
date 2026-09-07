"""Deterministic ACP dispatch for projects with an execution contract."""

from __future__ import annotations

import asyncio
import hashlib
import signal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.domain.results import ResultService
from greatminds.domain.maintenance import MaintenanceService
from greatminds.domain.stand_deployments import DeploymentLedger
from greatminds.domain.stand_leases import StandLeaseService
from .config import load_execution_config
from .stand_scheduler import StandScheduler
from .store import RunStore, TERMINAL, TaskRevision
from .supervisor import Supervisor
from .processes import terminate_group
from .background import presence, publish_presence
from .retry_policy import retry_admission, account_backoff


async def _domain_reconcile(loop, pool, results):
    future = loop.run_in_executor(pool, results.reconcile)
    # A finite wait also services shutdown/control signals on hosts where a
    # cross-thread event-loop wakeup is delayed. Healthy wakeups return early.
    while not future.done():
        await asyncio.wait({future}, timeout=0.1)
    return future.result()


def assignments(store, config, schema, *, snapshot=None):
    """Explain each concrete assignment without creating processes or claims."""
    snapshot = store.snapshot() if snapshot is None else snapshot
    runs = list(snapshot["runs"].values())
    active = [run for run in runs if run["state"] not in TERMINAL]
    observed_at = store.clock()
    account_delays = {account: account_backoff(snapshot, account, config, observed_at)
                      for account in {binding.account for binding in config.bindings}}
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
                elif any(receipt["envelope"]["task_id"] == task.task_id
                         and receipt["status"] in {"received", "applying", "needs_recovery"}
                         for receipt in snapshot["results"].values()):
                    reason = "domain_result_unresolved"
                elif any(item["task_id"] == task.task_id and item["status"] in {"queued", "starting", "running", "needs_recovery"}
                         for item in snapshot.get("commands", {}).values()):
                    reason = "command_unresolved"
                elif any(item["task_id"] == task.task_id and item["status"] in {"prepared", "needs_recovery"}
                         for item in snapshot.get("maintenance", {}).values()):
                    reason = "maintenance_unresolved"
                elif any(run["account"] == binding.account and run["state"] == "waiting_auth" for run in active):
                    reason = "account_authentication_required"
                elif previous and not latest.get("retry_authorized"):
                    # An unchanged revision does not create an infinite LLM
                    # loop, including after restart. Explicit retry is added
                    # through operator controls, not inferred from prose.
                    receipts = [receipt for receipt in snapshot["results"].values()
                                if receipt["envelope"]["run_id"] == latest["id"]]
                    if receipts and receipts[-1]["status"] == "rejected":
                        reason = "result_rejected"
                    elif receipts and receipts[-1]["envelope"]["decision"] == "needs_input":
                        reason = "human_input_required"
                    else:
                        reason = retry_admission(snapshot, binding, task, config, schema, store.clock())["reason"]
                if reason == "ready":
                    reason = retry_admission(snapshot, binding, task, config, schema, observed_at)["reason"]
                if reason == "ready":
                    reason = account_delays[binding.account]["reason"]
                if reason == "ready" and len(active) >= config.max_running:
                    reason = "project_capacity"
                elif reason == "ready" and sum(run["binding_id"] == binding.id for run in active) >= binding.max_running:
                    reason = "binding_capacity"
                elif reason == "ready" and sum(run["account"] == binding.account for run in active) >= dict(config.account_limits).get(binding.account, config.max_running):
                    reason = "account_capacity"
                yield binding, task, reason


async def serve(project: Path, *, interval: float = 1, once: bool = False,
                environment: dict | None = None):
    try:
        revision = hashlib.sha256((project / 'coordination/execution.yaml').read_bytes()).hexdigest()
    except OSError as exc:
        raise GreatMindsError('cannot load execution config', exit_code=2) from exc
    schema = load_schema_snapshot()
    config = load_execution_config(project / "coordination" / "execution.yaml",
                                   roles=set(schema.document["roles"]))
    store = RunStore(project_runtime_dir(project))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    domain_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="greatminds-domain")
    try:
        if not once:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
                installed_signals.append(signum)
        async with Supervisor(project=project, store=store, schema=schema,
                              config=config, environment=environment,
                              on_acquired=lambda: publish_presence(project, revision, 'starting')) as supervisor, presence(project, revision=revision):
            active: dict[str, asyncio.Task] = {}
            results = ResultService(store, environment=supervisor.environment)
            maintenance = MaintenanceService(store, schema, environment=supervisor.environment)
            deployments = DeploymentLedger(store.runtime)
            stand_leases = StandLeaseService(store)
            stand_scheduler = StandScheduler(store, config.stand)
            dispatched_once = False
            try:
                while not stop.is_set():
                    for run_id, future in list(active.items()):
                        if future.done():
                            if not future.cancelled():
                                future.result()  # Surface persistence failures; do not silently retry.
                            else:
                                await supervisor.commands.finish_run(run_id)
                            del active[run_id]
                    await _domain_reconcile(loop, domain_pool, results)
                    await _domain_reconcile(loop, domain_pool, maintenance)
                    await _domain_reconcile(loop, domain_pool, deployments)
                    await _domain_reconcile(loop, domain_pool, stand_leases)
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
                    await supervisor.commands.poll(supervisor.id)
                    stand_scheduler.poll()
                    from .interactions import ConversationStore
                    closing = False
                    # One consistent observation for this pass. Claims still recheck
                    # capacity and conversation identity under the store lock.
                    conversation_paths = sorted((store.directory / 'conversations').glob('*/state.json'))
                    live_conversations = {}
                    for run in (store.snapshot()['runs'].values() if conversation_paths else ()):
                        if run.get('conversation_id') and run['state'] not in TERMINAL:
                            live_conversations.setdefault(run['conversation_id'], []).append(run)
                    for path in conversation_paths:
                        conversation = ConversationStore(store.runtime, path.parent.name)
                        document = conversation.snapshot()
                        if document.get('close_requested') and not document['closed']:
                            live = live_conversations.get(conversation.id, [])
                            if live:
                                closing = True
                                for run in live:
                                    if not run.get('control') or run['control']['status'] == 'completed':
                                        store.request_control(run['id'], 'cancel')
                            else:
                                conversation.finish_close(supervisor.id)
                            continue
                        if document['closed'] or conversation.id in live_conversations:
                            continue
                        binding = next((b for b in config.bindings if b.id == document['binding_id']), None)
                        if binding is None:
                            conversation.dispatch_status('blocked', 'binding is no longer configured')
                            continue
                        try:
                            conversation.acquire(supervisor.id, config_sha256=config.sha256,
                                                 schema_sha256=schema.sha256)
                            if not any(t['status'] == 'queued' for t in conversation.snapshot()['turns'].values()):
                                continue
                            claim = supervisor.claim(TaskRevision.conversation(store.runtime, conversation.id),
                                                     binding, conversation_id=conversation.id)
                        except GreatMindsError as exc:
                            conversation.dispatch_status('blocked', str(exc)[:200])
                            continue
                        conversation.dispatch_status('running')
                        active[claim.run['id']] = asyncio.create_task(supervisor.execute(
                            claim, binding=binding, conversation=conversation, keep_open=not once))
                    assignment_snapshot = store.snapshot()
                    candidates = list(assignments(store, config, schema, snapshot=assignment_snapshot))
                    revisions = {task.path: task.sha256 for _, task, _ in candidates}
                    observed = {path: entry['task_revision'] for path, entry in
                                assignment_snapshot.get('queue_observations', {}).items()}
                    if revisions != observed:
                        store.observe_assignments([task for _, task, _ in candidates])
                    for binding, task, reason in candidates:
                        if reason != "ready" or (once and dispatched_once):
                            continue
                        try:
                            claim = supervisor.claim(task, binding, automatic=True)
                        except GreatMindsError as exc:
                            if exc.exit_code == 4:
                                raise
                            continue  # Claims recheck capacity and revision under locks.
                        active[claim.run["id"]] = asyncio.create_task(
                            supervisor.execute(claim, binding=binding))
                    dispatched_once = True
                    if once and not active and not stand_scheduler.busy and not closing:
                        await _domain_reconcile(loop, domain_pool, results)
                        await _domain_reconcile(loop, domain_pool, maintenance)
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
                await stand_scheduler.close()
                # Finish any in-progress domain transaction before releasing
                # the project supervisor lease, including on cancellation.
                domain_pool.shutdown(wait=True, cancel_futures=True)
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        domain_pool.shutdown(wait=True, cancel_futures=True)
    return store.snapshot()
