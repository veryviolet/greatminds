"""Task 0378: driven Codex auth detector must ignore command-output
signature strings.

Regression for the false positive where ``_CodexStdioSession.consume_turn``
scanned ``json.dumps(msg)`` of EVERY app-server message for auth
signatures — so an ``item/commandexecution/outputdelta`` (or any
assistant message) that merely *mentions* ``refresh_token_reused`` /
``token_expired`` / ``401`` / ``auth_failure`` (e.g. ARCHITECT-REVIEWER
reviewing or testing the 0374/0375/0376 auth code) tripped
``_CodexAuthError`` even though the transport/auth layer was healthy.

The fix scopes auth-signature matching to error-bearing text only
(``_codex_auth_scan_text``): JSON-RPC ``error`` responses and the
explicitly error-bearing fields of notification events — NEVER ``item/*``
assistant/tool OUTPUT. True auth errors must still fail fast.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from greatminds.cli import coordd as cd


def _session_with(messages: list[dict]) -> cd._CodexStdioSession:
    r, w = os.pipe()
    for m in messages:
        os.write(w, (json.dumps(m) + "\n").encode("utf-8"))
    os.close(w)

    class _FakeStdout:
        def fileno(self) -> int:
            return r

    class _FakeProc:
        stdout = _FakeStdout()

    return cd._CodexStdioSession(_FakeProc())


# ---------- false-positive must NOT raise ----------


@pytest.mark.parametrize(
    "signature",
    ["refresh_token_reused", "token_expired", "401 unauthorized",
     "auth_failure", "no codex credentials", "please run codex login"],
)
def test_command_output_delta_with_auth_string_does_not_raise(signature):
    """The exact DEV-fleet false positive: a command-output delta whose
    payload contains an auth signature must complete the turn, not raise
    ``_CodexAuthError``."""
    sess = _session_with([
        {"method": "item/started", "params": {}},
        {"method": "item/commandexecution/outputdelta",
         "params": {"delta": f"grep matched: {signature} in coordd.py"}},
        {"method": "item/completed", "params": {}},
        {"method": "turn/completed", "params": {"threadId": "th"}},
    ])
    work, transcript = sess.consume_turn("th", time.monotonic() + 5)
    # output-delta + the two surrounding items all count as real work.
    assert work == 3
    assert "item/commandexecution/outputdelta" in transcript


def test_assistant_message_text_mentioning_auth_does_not_raise():
    """An assistant item whose text body reviews auth code is OUTPUT,
    not an auth failure."""
    sess = _session_with([
        {"method": "item/agentmessage/delta",
         "params": {"text": "The detector raises on refresh_token_reused "
                            "and 401 unauthorized; tests cover token_expired."}},
        {"method": "turn/completed", "params": {"threadId": "th"}},
    ])
    work, _ = sess.consume_turn("th", time.monotonic() + 5)
    assert work == 1


# ---------- true auth failures must STILL fail fast ----------


def test_jsonrpc_error_response_with_auth_signature_raises():
    sess = _session_with([
        {"id": 3, "error": {"code": -32000,
                            "message": "401 unauthorized: token_expired"}},
    ])
    with pytest.raises(cd._CodexAuthError):
        sess.consume_turn("th", time.monotonic() + 5, turn_req_id=3)


def test_codex_event_error_field_with_auth_signature_raises():
    sess = _session_with([
        {"method": "codex/event",
         "params": {"error": "refresh_token_reused: please run codex login"}},
    ])
    with pytest.raises(cd._CodexAuthError):
        sess.consume_turn("th", time.monotonic() + 5)


def test_turn_failed_with_auth_message_raises():
    sess = _session_with([
        {"method": "turn/failed",
         "params": {"message": "no codex credentials"}},
    ])
    with pytest.raises(cd._CodexAuthError):
        sess.consume_turn("th", time.monotonic() + 5)


# ---------- _codex_auth_scan_text unit coverage ----------


def test_scan_text_excludes_item_payloads():
    msg = {"method": "item/commandexecution/outputdelta",
           "params": {"delta": "token_expired 401 unauthorized"}}
    assert cd._codex_auth_scan_text(msg) == ""


def test_scan_text_includes_jsonrpc_error_whole():
    msg = {"id": 3, "error": {"message": "401 unauthorized"}}
    assert "401 unauthorized" in cd._codex_auth_scan_text(msg)


def test_scan_text_includes_event_error_fields_only():
    msg = {"method": "codex/event",
           "params": {"msg": "token_expired", "irrelevant": "x"}}
    assert "token_expired" in cd._codex_auth_scan_text(msg)
