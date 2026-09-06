"""Independent wire-level ACP test peer. Never contacts a model service."""

import json
import subprocess
import sys
from pathlib import Path


scenario = sys.argv[1]
pending = None


def send(document):
    print(json.dumps({"jsonrpc": "2.0", **document}), flush=True)


def result(request_id, payload):
    send({"id": request_id, "result": payload})


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        result(request_id, {"protocolVersion": 999 if scenario == "bad-version" else 1,
                            "agentCapabilities": {"loadSession": False},
                            "authMethods": []})
    elif method == "session/new":
        result(request_id, {"sessionId": "test-session"})
    elif method == "session/prompt":
        pending = request_id
        if scenario == "disconnect":
            sys.exit(0)
        elif scenario == "hang":
            continue
        elif scenario == "permission":
            send({"id": "permission-one", "method": "session/request_permission",
                  "params": {"sessionId": "test-session",
                             "toolCall": {"toolCallId": "tool-one", "title": "Read a file", "kind": "read"},
                             "options": [{"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                                         {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
        else:
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
