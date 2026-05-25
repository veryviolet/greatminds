"""Tests for ``greatminds stop-decide`` — the claude/cursor stop-hook
helper that decides whether the agent should keep ticking.

Task 0143: the claude-host stop hook surfaced only ``.md`` inbox files
(daemon journal-notify wakes). ``greatminds inbox send`` writes
``.yaml`` for ask/info messages, which never reached the claude
PLANNER's stop hook — PLANNER stayed idle after a direct role-to-role
ask. Fix makes ``inbox_pending`` accept both ``.md`` and ``.yaml``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import stop_decide as stop_decide_mod


def _make_inbox(tmp_path: Path, role: str) -> Path:
    coord = tmp_path / "coordination"
    inbox = coord / "inbox" / role.lower()
    inbox.mkdir(parents=True)
    return inbox


def _invoke(role: str, project_dir: Path, host: str = "claude"):
    runner = CliRunner()
    return runner.invoke(
        stop_decide_mod.stop_decide,
        [role, "--host", host, "--project-dir", str(project_dir)],
        catch_exceptions=False,
    )


# ---------- inbox_pending: file-extension allowlist ----------


def test_inbox_pending_lists_md_wake(tmp_path: Path) -> None:
    """Daemon journal-notify wakes are .md — must still be surfaced
    (the iter-1 contract; iter-2 added .yaml support)."""
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / "wake-1234-0099-feature_dev.md").write_text("x", encoding="utf-8")
    coord = tmp_path / "coordination"
    pending = stop_decide_mod.inbox_pending(coord, "DEVELOPER")
    assert pending == ["wake-1234-0099-feature_dev.md"]


def test_inbox_pending_lists_yaml_ask(tmp_path: Path) -> None:
    """0143 fix: ``greatminds inbox send <role> --kind ask`` writes
    ``ask-<ts>-...yaml``. Pre-fix this never surfaced through
    stop-decide → claude PLANNER stayed idle after EXPLORER's send."""
    inbox = _make_inbox(tmp_path, "architect-planner")
    (inbox / "ask-1779694900-probe.yaml").write_text(
        "from_role: EXPLORER\nbody: probe\n", encoding="utf-8",
    )
    coord = tmp_path / "coordination"
    pending = stop_decide_mod.inbox_pending(coord, "ARCHITECT-PLANNER")
    assert pending == ["ask-1779694900-probe.yaml"]


def test_inbox_pending_lists_yaml_info(tmp_path: Path) -> None:
    """``--kind info`` also writes .yaml. Same surfacing path."""
    inbox = _make_inbox(tmp_path, "architect-planner")
    (inbox / "info-1779694900-status.yaml").write_text(
        "from_role: EXPLORER\nbody: status\n", encoding="utf-8",
    )
    coord = tmp_path / "coordination"
    pending = stop_decide_mod.inbox_pending(coord, "ARCHITECT-PLANNER")
    assert pending == ["info-1779694900-status.yaml"]


def test_inbox_pending_mixed_md_and_yaml(tmp_path: Path) -> None:
    """Both surface together. Sort is stable (alphabetical)."""
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / "ask-1779694901-q.yaml").write_text("x", encoding="utf-8")
    (inbox / "wake-1779694900-0001.md").write_text("x", encoding="utf-8")
    pending = stop_decide_mod.inbox_pending(tmp_path / "coordination",
                                            "DEVELOPER")
    assert sorted(pending) == [
        "ask-1779694901-q.yaml",
        "wake-1779694900-0001.md",
    ]


def test_inbox_pending_skips_other_extensions(tmp_path: Path) -> None:
    """``.txt``, ``.json``, ``.swp`` etc. must NOT surface as inbox
    messages even if they end up in the inbox dir."""
    inbox = _make_inbox(tmp_path, "developer")
    for noise in (
        "note.txt", "draft.json", ".wake.yaml.swp",
        "wake.bak", "log.gz",
    ):
        (inbox / noise).write_text("x", encoding="utf-8")
    pending = stop_decide_mod.inbox_pending(tmp_path / "coordination",
                                            "DEVELOPER")
    assert pending == []


def test_inbox_pending_skips_processed_prefix_for_both_extensions(tmp_path: Path) -> None:
    """Acked breadcrumbs (``processed-*``) must NOT count, whether
    they came from .md (wake) or .yaml (ask/info) acks."""
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / "processed-wake-1779-0001.md").write_text("x", encoding="utf-8")
    (inbox / "processed-ask-1779-q.yaml").write_text("x", encoding="utf-8")
    pending = stop_decide_mod.inbox_pending(tmp_path / "coordination",
                                            "DEVELOPER")
    assert pending == []


def test_inbox_pending_skips_gitkeep(tmp_path: Path) -> None:
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / ".gitkeep").write_text("", encoding="utf-8")
    pending = stop_decide_mod.inbox_pending(tmp_path / "coordination",
                                            "DEVELOPER")
    assert pending == []


def test_inbox_pending_empty_when_no_inbox_dir(tmp_path: Path) -> None:
    """No inbox dir for the role → empty list, no crash."""
    (tmp_path / "coordination").mkdir()
    pending = stop_decide_mod.inbox_pending(tmp_path / "coordination",
                                            "NONEXISTENT-ROLE")
    assert pending == []


# ---------- end-to-end via the click command ----------


def test_stop_decide_blocks_on_yaml_ask_claude_host(tmp_path: Path) -> None:
    """0143 acceptance: claude-host stop hook must now block on a
    .yaml ask. Pre-fix this returned ``{}`` (allow stop); PLANNER
    therefore stayed idle after EXPLORER's send."""
    inbox = _make_inbox(tmp_path, "architect-planner")
    (inbox / "ask-1779694900-probe.yaml").write_text("x", encoding="utf-8")
    result = _invoke("ARCHITECT-PLANNER", tmp_path)
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["decision"] == "block"
    assert "1 pending inbox message" in payload["reason"]
    assert "inbox/architect-planner/" in payload["reason"]


def test_stop_decide_allows_when_inbox_empty(tmp_path: Path) -> None:
    _make_inbox(tmp_path, "developer")
    result = _invoke("DEVELOPER", tmp_path)
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_stop_decide_message_no_longer_says_wake_only(tmp_path: Path) -> None:
    """0143 wording fix: 'new wake message(s)' was misleading when an
    ask/info counted. The new phrasing 'pending inbox message(s)'
    correctly covers all surfaced kinds."""
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / "ask-1779694900-q.yaml").write_text("x", encoding="utf-8")
    result = _invoke("DEVELOPER", tmp_path)
    payload = json.loads(result.output)
    assert "pending inbox message(s)" in payload["reason"]
    # Negative pin: the misleading old phrasing must be gone.
    assert "new wake message(s)" not in payload["reason"]


def test_stop_decide_cursor_host_emits_followup_message(tmp_path: Path) -> None:
    """The cursor branch uses ``followup_message``, not the
    claude-style ``decision/reason/systemMessage`` triple. 0143 must
    not regress the cursor envelope."""
    inbox = _make_inbox(tmp_path, "developer")
    (inbox / "ask-1779694900-q.yaml").write_text("x", encoding="utf-8")
    result = _invoke("DEVELOPER", tmp_path, host="cursor")
    payload = json.loads(result.output)
    assert "followup_message" in payload
    assert "decision" not in payload
    assert "1 pending inbox message" in payload["followup_message"]
