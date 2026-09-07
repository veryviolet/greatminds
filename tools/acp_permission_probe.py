"""Live supervisor/CLI permission test in a temporary project (uses model usage).

The probe never approves a request. Inspect/answer the printed project's request
with `greatminds run permission ID --option ID` from an operator terminal.
With --cancel-while-pending it cancels the supervised run after observing an
unanswered permission, and verifies cleanup without executing the command.
"""

import argparse
import asyncio
import json
from pathlib import Path
import tempfile

from greatminds.core.paths import project_runtime_dir
from greatminds.core.schema import load_schema_snapshot
from greatminds.runtime.config import parse_execution_config
from greatminds.runtime.store import RunStore, TaskRevision
from greatminds.runtime.supervisor import Supervisor
from greatminds.runtime.processes import group_members, process_identity


PROMPT = ('Use your shell tool to execute exactly: python3 -c "from pathlib import Path; '
          "Path('acp-permission-check.txt').write_text('permission-ok')\". "
          'Then reply with exactly DONE. This is a synthetic test inside this temporary workspace.')


async def probe(args, argv):
    project = Path(tempfile.mkdtemp(prefix="greatminds-permission-live-"))
    runtime = project_runtime_dir(project)
    path = runtime / "feature_dev" / "0001-permission.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("title: Synthetic permission integration\n")
    schema = load_schema_snapshot()
    binding = {"role": "DEVELOPER", "agent": "probe", "permission": "ask", "timeout_seconds": args.timeout}
    if args.mode:
        binding["mode"] = args.mode
    config = parse_execution_config({"version": 1, "agents": {"probe": {
        "transport": "acp", "argv": argv, "adapter_version": args.adapter_version,
        "harness_version": args.harness_version}}, "bindings": {"worker": binding}},
        roles=set(schema.document["roles"]))
    store = RunStore(runtime)
    print(json.dumps({"project": str(project), "runtime": str(runtime)}), flush=True)
    async with Supervisor(project=project, store=store, config=config, schema=schema) as service:
        claim = service.claim(TaskRevision.capture(runtime, path), config.bindings[0])
        marker = project / "acp-permission-check.txt"
        work = asyncio.create_task(service.execute(claim, binding=config.bindings[0], prompt=args.prompt_prefix + PROMPT))
        observed_before_marker = False
        try:
            while not work.done():
                pending = [item for item in store.snapshot().get("permissions", {}).values()
                           if item["status"] == "pending"]
                if pending:
                    observed_before_marker = not marker.exists()
                    if args.cancel_while_pending:
                        work.cancel()
                    break
                await asyncio.sleep(.05)
            result = await work
        finally:
            if not work.done():
                work.cancel()
                await work
        requests = list(store.snapshot().get("permissions", {}).values())
        report = {"run_id": result["id"], "state": result["state"], "reason": result["reason"],
                  "cancel_while_pending": args.cancel_while_pending,
                  "permission_observed_before_marker": observed_before_marker,
                  "marker": marker.read_text() if marker.exists() else None,
                  "permissions": [{key: item.get(key) for key in
                                   ("id", "status", "option_id", "created_at", "answered_at", "consumed_at")}
                                  for item in requests]}
        identity = result.get('process')
        report['process_group_exited'] = not identity or (
            process_identity(identity['pid']) != identity and not group_members(identity))
        report["passed"] = (result["state"] == "completed" and report["marker"] == "permission-ok"
                            and observed_before_marker and any(item["status"] == "consumed" and any(
                                option["optionId"] == item["option_id"] and option["kind"] == "allow_once"
                                for option in item["options"]) for item in requests))
        if args.cancel_while_pending:
            report['passed'] = (result['state'] == 'cancelled' and observed_before_marker
                and not marker.exists() and bool(requests)
                and all(item['status'] == 'cancelled' for item in requests)
                and report['process_group_exited'])
        print(json.dumps(report), flush=True)
        return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-version", required=True)
    parser.add_argument("--harness-version", required=True)
    parser.add_argument("--mode")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--prompt-prefix", default="")
    parser.add_argument("--cancel-while-pending", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv or args.timeout <= 0:
        parser.error("provide an executable and a positive timeout")
    return asyncio.run(probe(args, argv))


if __name__ == "__main__":
    raise SystemExit(main())
