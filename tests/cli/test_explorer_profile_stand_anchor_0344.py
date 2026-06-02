"""Tests for task 0344: the shipped EXPLORER codex profile must carry the
0336 stand-anchor, NOT the stale web-only no-SSH/host-probe boundary.

0336 de-web-ified schema.roles.EXPLORER + roles/EXPLORER.md, but missed
src/greatminds/data/codex/profiles/explorer.config.toml — its
``developer_instructions`` still injected "NEVER ssh into stand hosts …
REST API + DB … ONLY", which OVERRODE the corrected contract and blocked
on-stand validation. 0344 replaces that line with the stand-anchor
wording (work ON the stand as a real user whatever the product's shape;
ssh/host as needed) while keeping the coordination-access CLI-only rule.
"""
from __future__ import annotations

import tomllib

from greatminds.core.paths import find_canon_dir


def _explorer_profile_text() -> str:
    return (find_canon_dir() / "codex" / "profiles"
            / "explorer.config.toml").read_text(encoding="utf-8")


def _developer_instructions() -> str:
    doc = tomllib.loads(_explorer_profile_text())
    return doc.get("developer_instructions") or ""


# ---------- the stale web-only boundary is gone ----------


def test_profile_has_no_no_ssh_boundary() -> None:
    instr = _developer_instructions().lower()
    assert "never ssh" not in instr, (
        "0344: explorer profile still forbids ssh (stale web-only boundary)")
    assert "no host-probe" not in instr
    assert "ls/cat host filesystem" not in instr


def test_profile_has_no_rest_db_only_method() -> None:
    instr = _developer_instructions().lower()
    # the "REST API + DB read queries ... ONLY" web-only method must be gone
    assert "rest api + db" not in instr
    assert not ("rest" in instr and "only" in instr
                and "db read" in instr), (
        "0344: explorer profile still pins REST+DB as the ONLY method")


# ---------- the 0336 stand-anchor is present ----------


def test_profile_states_stand_anchor() -> None:
    instr = _developer_instructions().lower()
    assert "stand-anchor" in instr or "on the stand as a real user" in instr
    # access method follows the product shape — ssh/host named alongside http
    assert "ssh" in instr, (
        "0344: profile must name ssh/host access for host-shaped products")
    assert "off-stand" in instr or "local substitutes" in instr, (
        "0344: profile must still forbid off-stand / local validation")


def test_profile_keeps_coordination_access_cli_only() -> None:
    """The CLI-only coordination-access rule is correct and unrelated to
    stand probing — it must NOT be removed alongside the boundary."""
    instr = _developer_instructions().lower()
    assert "greatminds cli" in instr or "cli-only" in instr or (
        "coordination/" in instr and "greatminds" in instr)


# ---------- profile still parses + keeps its other invariants ----------


def test_profile_still_valid_toml_with_explorer_table() -> None:
    doc = tomllib.loads(_explorer_profile_text())
    assert "developer_instructions" in doc
    prof = (doc.get("profiles") or {}).get("explorer") or {}
    assert prof.get("approval_policy") == "never"
    # heartbeat + owned-queue guidance preserved
    instr = doc["developer_instructions"]
    assert "review_sessions" in instr
    assert "heartbeat" in instr.lower()
