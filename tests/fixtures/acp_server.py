"""Independent wire-level ACP test peer. Never contacts a model service."""

import json
import subprocess
import sys
import time
from pathlib import Path


scenario = sys.argv[1]
pending = None
with Path("agent-starts.log").open("a") as starts:
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
        result(request_id, {"protocolVersion": 999 if scenario == "bad-version" else 1,
                            "agentCapabilities": {"loadSession": scenario == "resume"},
                            "authMethods": []})
    elif method == "session/new":
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
        result(request_id, {})
    elif method == "session/prompt":
        pending = request_id
        if scenario == "disconnect":
            sys.exit(0)
        elif scenario in {"hang", "orphan"}:
            continue
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
            if scenario == "submit":
                context = json.loads(message["params"]["prompt"][0]["text"].split("\n\n", 1)[1])
                envelope = context["result_format"]
                envelope.update(result_id="fixture-result", decision="no_change", payload={})
                Path("result.json").write_text(json.dumps(envelope))
                submitted = subprocess.run([sys.executable, "-m", "greatminds.cli.main", "run", "submit",
                                            "--file", str(Path("result.json").resolve())],
                                           capture_output=True, text=True)
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
        send({"method": "session/update", "params": {
            "sessionId": "test-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps(outcome)}}}})
        result(pending, {"stopReason": "end_turn"})
    elif method == "session/cancel" and scenario != "hang":
        result(pending, {"stopReason": "cancelled"})

if scenario == "orphan":
    time.sleep(60)  # Survive EOF after supervisor SIGKILL for recovery tests.
