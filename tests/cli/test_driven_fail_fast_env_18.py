"""Tests for 0369 (GitHub #18): the driven ``claude -p`` subprocess must
fail fast under API rate-limit / overload instead of retrying in-process for
many minutes while holding the run-lock.

Root cause: claude's own defaults — ``API_TIMEOUT_MS`` (10 min/attempt) and
``CLAUDE_CODE_MAX_RETRIES`` (10) — let a rate-limited turn churn silently,
starving the role queue. The fix pins both low in the driven subprocess env so
the turn returns quickly; coordd's existing outcome classifier + retry
scheduler then own the (visible) backoff.
"""
from __future__ import annotations

from greatminds.cli import coordd as cd


# claude's stock defaults the fail-fast knobs must undercut. If these drift in
# the SDK the intent still holds: driven turns must be meaningfully shorter.
_STOCK_API_TIMEOUT_MS = 600_000
_STOCK_MAX_RETRIES = 10


def test_env_carries_fail_fast_knobs() -> None:
    """The driven subprocess env pins API_TIMEOUT_MS + CLAUDE_CODE_MAX_RETRIES
    to coordd's fail-fast values (alongside the role export)."""
    env = cd._driven_subprocess_env("developer")
    assert env["GREATMINDS_ROLE"] == "DEVELOPER"
    assert env["API_TIMEOUT_MS"] == cd.DRIVEN_API_TIMEOUT_MS
    assert env["CLAUDE_CODE_MAX_RETRIES"] == cd.DRIVEN_MAX_RETRIES


def test_fail_fast_defaults_undercut_stock() -> None:
    """The defaults must be materially smaller than claude's stock retry
    behaviour — otherwise a stalled turn still holds the lock for many
    minutes (the #18 starvation)."""
    assert int(cd.DRIVEN_API_TIMEOUT_MS) < _STOCK_API_TIMEOUT_MS
    assert int(cd.DRIVEN_MAX_RETRIES) < _STOCK_MAX_RETRIES
    # And the worst-case in-process stall (timeout × retries) stays well
    # under the 30-min DRIVEN_TURN_TIMEOUT_SEC backstop, so the env fix —
    # not the backstop — is what frees the lock.
    worst_case_sec = (int(cd.DRIVEN_API_TIMEOUT_MS) / 1000.0) * (
        int(cd.DRIVEN_MAX_RETRIES) + 1)
    assert worst_case_sec < cd.DRIVEN_TURN_TIMEOUT_SEC


def test_fail_fast_overrides_inherited_env(monkeypatch) -> None:
    """An inherited (operator/global) API_TIMEOUT_MS does NOT leak into a
    driven turn — driven turns must fail fast regardless, so coordd's value
    wins over os.environ."""
    monkeypatch.setenv("API_TIMEOUT_MS", "9999999")
    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "99")
    env = cd._driven_subprocess_env("tester")
    assert env["API_TIMEOUT_MS"] == cd.DRIVEN_API_TIMEOUT_MS
    assert env["CLAUDE_CODE_MAX_RETRIES"] == cd.DRIVEN_MAX_RETRIES


def test_env_preserves_claude_auth_and_sets_home_path(monkeypatch) -> None:
    """Driven Claude needs explicit HOME/PATH, while operator-supplied
    Claude auth env remains valid and must not be stripped."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "old-oauth")
    monkeypatch.setenv("CLAUDE_BRIDGE_OAUTH_TOKEN", "old-bridge")
    monkeypatch.setenv("CLAUDE_CODE_HOST_AUTH_ENV_VAR", "HOST_AUTH")
    monkeypatch.setenv("HOST_AUTH", "old-host-auth")

    env = cd._driven_subprocess_env("developer")

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "old-oauth"
    assert env["CLAUDE_BRIDGE_OAUTH_TOKEN"] == "old-bridge"
    assert env["CLAUDE_CODE_HOST_AUTH_ENV_VAR"] == "HOST_AUTH"
    assert env["HOST_AUTH"] == "old-host-auth"
    assert env["HOME"]
    assert ".local/bin" in env["PATH"]
    assert env["GREATMINDS_ROLE"] == "DEVELOPER"
    assert env["CLAUDE_CODE_MAX_RETRIES"] == cd.DRIVEN_MAX_RETRIES
