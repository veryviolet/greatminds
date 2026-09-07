"""Independent wire-level ACP test peer. Never contacts a model service."""

import json
import os
import subprocess
import sys
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path


scenario = sys.argv[1]
pending = None
authenticated = False
starts_path = (Path(os.environ["GREATMINDS_PROJECT_DIR"]) / ".greatminds" / "agent-starts.log"
               if scenario == "pipeline" else Path("agent-starts.log"))
with starts_path.open("a") as starts:
    starts.write(scenario + "\n")


def send(document):
    print(json.dumps({"jsonrpc": "2.0", **document}), flush=True)


def result(request_id, payload):
    send({"id": request_id, "result": payload})


def model_options(value):
    return [{"id": "model", "name": "Model", "category": "model", "type": "select",
             "currentValue": value, "options": [{"value": "default", "name": "Default"},
                                                 {"value": "chosen", "name": "Chosen"}]}]


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        capabilities = {"loadSession": scenario in {"resume", "usage-resume"}}
        if scenario.startswith("protocol-evidence"):
            capabilities.update(promptCapabilities={"image": True, "_meta": {"secret": "SECRET_META"}},
                                _meta={"credential": os.environ.get("GREATMINDS_RUN_TOKEN", "SECRET_TOKEN")})
        result(request_id, {"protocolVersion": 999 if scenario == "bad-version" else 1,
                            "agentCapabilities": capabilities,
                            "authMethods": ([{"id": "fixture-key", "name": "Fixture key"}] if scenario == "auth" else [])})
    elif method == "authenticate":
        if message["params"]["methodId"] == "fixture-key":
            authenticated = True
            result(request_id, {})
        else:
            send({"id": request_id, "error": {"code": -32000, "message": "Authentication required"}})
    elif method == "session/new":
        if scenario == "auth" and not authenticated:
            send({"id": request_id, "error": {"code": -32000, "message": "Authentication required"}})
            continue
        payload = {"sessionId": "test-session"}
        if scenario.startswith("model"):
            payload["configOptions"] = model_options("default")
        result(request_id, payload)
    elif method == "session/set_config_option":
        value = "default" if scenario == "model-ignore" else message["params"]["value"]
        result(request_id, {"configOptions": model_options(value)})
    elif method == "session/load":
        with Path("loads.log").open("a") as loads:
            loads.write(message["params"]["sessionId"] + "\n")
        send({"method": "session/update", "params": {
            "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "historical reply"}}}})
        result(request_id, {})
    elif method == "session/prompt":
        pending = request_id
        if scenario == "disconnect":
            sys.exit(0)
        elif scenario in {"hang", "orphan", "cancel", "cancel-output"}:
            if scenario == "cancel-output":
                send({"method": "session/update", "params": {
                    "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "started"}}}})
            continue
        elif scenario in {"usage-observations", "usage-budget-hang", "usage-resume",
                          "usage-regress", "usage-currency", "usage-invalid"}:
            previous_cost = float(Path('reported-cost').read_text()) if Path('reported-cost').exists() else 0
            costs = [(80, .2), (20, .3)]
            if scenario == 'usage-regress':
                costs[-1] = (20, .1)
            if scenario == 'usage-invalid':
                costs[-1] = (20, -1)
            if scenario == 'usage-resume':
                costs = [(20, round(previous_cost + .2, 2))]
                Path('reported-cost').write_text(str(costs[-1][1]))
            for used, amount in costs:
                send({"method": "session/update", "params": {
                    "sessionId": "test-session", "update": {
                        "sessionUpdate": "usage_update", "used": used, "size": 100,
                        "cost": {"amount": amount, "currency": 'EUR' if scenario == 'usage-currency' and used == 20 else 'USD',
                                 "_meta": {"secret": "SECRET_COST"}},
                        "_meta": {"secret": "SECRET_CONTEXT"}}}})
            if scenario == 'usage-budget-hang':
                continue
            result(pending, {"stopReason": "end_turn", "usage": {
                "totalTokens": 150, "inputTokens": 100, "outputTokens": 50,
                "cachedReadTokens": 70, "_meta": {"secret": "SECRET_TOKENS"}}})
        elif scenario.startswith("protocol-evidence"):
            for index in range(40):
                identifier = f"SECRET_TOOL_ID-{index}"
                send({"method": "session/update", "params": {"sessionId": "test-session", "update": {
                    "sessionUpdate": "tool_call", "toolCallId": identifier, "title": "SECRET_TITLE",
                    "kind": "edit", "status": "in_progress", "rawInput": {"secret": os.environ.get("GREATMINDS_RUN_TOKEN")},
                    "rawOutput": "SECRET_OUTPUT", "locations": [{"path": "/SECRET_PATH"}],
                    "content": [{"type": "content", "content": {"type": "text", "text": "SECRET_CONTENT"}}],
                    "_meta": {"secret": "SECRET_META"}}}})
                send({"method": "session/update", "params": {"sessionId": "test-session", "update": {
                    "sessionUpdate": "tool_call_update", "toolCallId": identifier, "status": "completed"}}})
            send({"method": "session/update", "params": {"sessionId": "test-session", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "SECRET_ASSISTANT"}}}})
            if scenario.endswith("-error"):
                send({"id": pending, "error": {"code": -32603, "message": "SECRET_ERROR",
                                               "data": {"secret": "SECRET_DETAIL"}}})
            else:
                result(pending, {"stopReason": "end_turn"})
        elif scenario.startswith("permission"):
            tool = {"toolCallId": "tool-one", "title": "Read a file", "kind": "read"}
            if scenario != "permission":
                location = Path.cwd() / "file.txt" if scenario != "permission-outside" else Path.cwd().parent / "outside.txt"
                tool["locations"] = [{"path": str(location)}]
            if scenario == "permission-execute":
                tool["kind"] = "execute"
            send({"id": "permission-one", "method": "session/request_permission",
                  "params": {"sessionId": "test-session",
                             "toolCall": tool,
                             "options": [{"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                                         {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
        else:
            if scenario == 'task-chat':
                context = json.loads(subprocess.check_output(
                    [sys.executable, '-m', 'greatminds.cli.main', 'run', 'contract'], text=True))
                Path('chat-task-context.json').write_text(json.dumps(context))
                subprocess.run([*context['cli_argv'], 'run', 'submit', '--json',
                                json.dumps({'decision': 'no_change', 'payload': {}})], check=True,
                               capture_output=True, text=True)
            if scenario == "pipeline":
                context = json.loads(message["params"]["prompt"][0]["text"].split("\n\n", 1)[1])
                role = context["role"]
                if role == "DEVELOPER":
                    Path("clamp.py").write_text("def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('invalid bounds')\n    return min(max(value, lower), upper)\n")
                checked = subprocess.run([*context["cli_argv"], "run", "command", "unit-tests", "--wait", "30"],
                                         capture_output=True, text=True, check=True)
                evidence = json.loads(checked.stdout)
                assert evidence["status"] == "succeeded", evidence
                assert "Ran 6 tests" in evidence["output_preview"]["stderr"]["text"]
                commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
                if role == "DEVELOPER":
                    target, block = "feature_test", {"kind": "implementation", "base_commit": commit,
                                                      "files": ["clamp.py"], "ready_for_test": True}
                elif role == "TESTER":
                    target, block = "feature_review", {"kind": "tests", "base_commit": commit,
                        "test_files": ["test_clamp.py"], "command_request_id": evidence["id"],
                        "stand_evidence": {}, "gate_check_result": "n/a", "gate_check_commit": commit,
                        "gate_check_at": datetime.now(timezone.utc).isoformat(), "ready_for_review": True}
                else:
                    target, block = "verified", {"kind": "review", "outcome": "approved", "commit": commit}
                decision = {"decision": "handoff", "payload": {"to_queue": target,
                    "blocks": [block], "command_evidence": [evidence["id"]], "artifacts": ["clamp.py"]}}
                subprocess.run([*context["cli_argv"], "run", "submit", "--json", json.dumps(decision)],
                               capture_output=True, text=True, check=True)
            if scenario in {"submit", "handoff", "command"}:
                context = json.loads(message["params"]["prompt"][0]["text"].split("\n\n", 1)[1])
                envelope = context["result_format"]
                envelope.update(result_id="fixture-result", decision="no_change", payload={})
                if scenario == "command":
                    checked = subprocess.run([sys.executable, "-m", "greatminds.cli.main", "run", "command",
                                              "check", "--request-id", "fixture-command", "--wait", "30"],
                                             capture_output=True, text=True)
                    if checked.returncode or json.loads(checked.stdout)["status"] != "succeeded":
                        send({"id": pending, "error": {"code": -32603, "message": "configured check failed"}})
                        continue
                    envelope["payload"] = {"command_evidence": ["fixture-command"]}
                if scenario == "handoff":
                    Path("implementation.txt").write_text("fixture implementation")
                    envelope.update(decision="handoff", payload={"to_queue": "feature_test", "blocks": [
                        {"kind": "implementation", "base_commit": "fixture-commit", "files": ["implementation.txt"],
                         "ready_for_test": True}], "artifacts": ["implementation.txt"]})
                # A result envelope is runtime metadata, not a new source file
                # which should invalidate a just-completed check.
                result_path = Path("result.json").resolve()
                if scenario == "command":
                    with tempfile.NamedTemporaryFile(prefix="greatminds-command-result-", suffix=".json", delete=False) as handle:
                        result_path = Path(handle.name)
                result_path.write_text(json.dumps(envelope))
                submitted = subprocess.run([sys.executable, "-m", "greatminds.cli.main", "run", "submit",
                                            "--file", str(result_path)],
                                           capture_output=True, text=True)
                if scenario == "command":
                    result_path.unlink()
                if submitted.returncode:
                    send({"id": pending, "error": {"code": -32603, "message": submitted.stderr}})
                    continue
            if scenario == "child":
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                Path("child.pid").write_text(str(child.pid))
            send({"method": "session/update", "params": {
                "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"}}}})
            result(pending, {"stopReason": "end_turn"})
    elif method is None and request_id == "permission-one":
        outcome = message.get("result", {}).get("outcome", {})
        if scenario == "permission-marker" and outcome.get("optionId") == "yes":
            Path("acp-permission-check.txt").write_text("permission-ok")
        send({"method": "session/update", "params": {
            "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps(outcome)}}}})
        result(pending, {"stopReason": "end_turn"})
    elif method == "session/cancel" and scenario != "hang":
        if scenario == 'usage-budget-hang':
            Path('usage-cancelled').write_text('cancelled')
        result(pending, {"stopReason": "cancelled"})

if scenario == "orphan":
    time.sleep(60)  # Survive EOF after supervisor SIGKILL for recovery tests.
