"""Tests for task 0349 (SAFETY): EXPLORER host-destructive actions must
target the AVATAR stand (ssh), never the local fleet.

During 0329 EXPLORER ran kill -TERM against THIS local dev fleet's coordd
(greatminds-daemon@greatminds-dev) instead of the avatar stand. The fix
makes the EXPLORER codex profile unambiguous: every host-destructive /
lifecycle / recovery command runs on the avatar via ``ssh <STAND_HOST>``,
and kill/pkill/systemctl/logout/reboot against localhost / greatminds-dev
is strictly forbidden. (A runtime guard was considered — option b — but
a codex agent runs a free shell with no chokepoint to intercept, so the
enforceable surface is the rendered contract; option a/c.)

0344's stand-anchor (no blanket no-ssh boundary) must REMAIN — this only
adds the avatar-targeting safety clause on top.
"""
from __future__ import annotations

import tomllib

from greatminds.core.paths import find_canon_dir


def _instr() -> str:
    doc = tomllib.loads(
        (find_canon_dir() / "codex" / "profiles"
         / "explorer.config.toml").read_text(encoding="utf-8"))
    return (doc.get("developer_instructions") or "").lower()


# ---------- the avatar-only safety clause is present ----------


def test_destructive_actions_require_ssh_to_avatar() -> None:
    instr = _instr()
    assert "ssh <stand_host>" in instr or "ssh stand_host" in instr, (
        "0349: profile must require ssh <STAND_HOST> for destructive actions")
    assert "avatar" in instr


def test_local_destructive_is_forbidden() -> None:
    instr = _instr()
    assert "greatminds-dev" in instr, (
        "0349: profile must name the local greatminds-dev fleet as off-limits")
    assert "localhost" in instr
    # the forbidden verbs are named
    for verb in ("kill", "systemctl", "logout"):
        assert verb in instr, f"0349: destructive verb {verb!r} must be named"
    assert "forbidden" in instr


def test_names_coordd_local_kill_as_the_hazard() -> None:
    instr = _instr()
    assert "coordd" in instr
    # "the local host is NOT the stand"
    assert "not the stand" in instr


# ---------- 0344 stand-anchor must NOT be re-broken ----------


def test_stand_anchor_and_ssh_operation_preserved() -> None:
    instr = _instr()
    # no blanket no-ssh boundary re-added (that was the wrong 0331 fix)
    assert "never ssh" not in instr
    assert "no host-probe" not in instr
    # on-stand ssh operation still allowed
    assert "ssh" in instr
    assert "stand-anchor" in instr or "on the stand as a real user" in instr


def test_profile_still_valid_toml() -> None:
    doc = tomllib.loads(
        (find_canon_dir() / "codex" / "profiles"
         / "explorer.config.toml").read_text(encoding="utf-8"))
    assert "developer_instructions" in doc
    assert (doc.get("profiles") or {}).get("explorer", {}).get(
        "approval_policy") == "never"
