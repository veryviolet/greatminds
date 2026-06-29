"""Tests for task 0313 (0311 Phase 1c): MAINTAINER recovery
commands in ``claude_settings.auto_mode.allow``.

MAINTAINER is self-loop (USER-absent) and runs recovery — agent
restart, daemon control, dead-pid SIGTERM. The classifier's
auto-mode ceiling blocked these without explicit allow entries,
so MAINTAINER's autonomous recovery stalled waiting for a USER
who isn't there. 0313 adds the recovery patterns to the
schema-driven auto_mode.allow list that setup bakes into
``.claude/settings.local.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


RECOVERY_PATTERNS = (
    "Bash(greatminds restart:*)",
    "Bash(greatminds daemon:*)",
    "Bash(greatminds start-agent:*)",
    "Bash(kill:*)",
    "Bash(systemctl --user:*)",
)

REVIEWER_FSM_PATTERNS = (
    "Bash(greatminds task mv:*)",
    "Bash(greatminds worktree:*)",
    "Bash(git revert:*)",
)


def _schema_auto_mode() -> list[str]:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return (((doc.get("claude_settings") or {})
             .get("auto_mode") or {})
            .get("allow") or [])


# ---------- schema source-of-truth ----------


def test_schema_auto_mode_has_recovery_patterns() -> None:
    """0313: every recovery pattern MAINTAINER needs must be in
    schema.claude_settings.auto_mode.allow."""
    allow = _schema_auto_mode()
    for pat in RECOVERY_PATTERNS:
        assert pat in allow, (
            f"0313: auto_mode.allow missing recovery pattern "
            f"{pat!r}; MAINTAINER's autonomous recovery will be "
            "blocked by the classifier"
        )


def test_schema_auto_mode_keeps_existing_entries() -> None:
    """Regression net: the 0267/0273 push + ops entries must
    survive the 0313 addition."""
    allow = _schema_auto_mode()
    for legacy in ("$defaults", "Bash(git push origin main:*)",
                    "Bash(ssh:*)", "Bash(rsync:*)"):
        assert legacy in allow


def test_schema_auto_mode_has_reviewer_fsm_patterns() -> None:
    """Driven REVIEWER must be able to execute the FSM's own terminal
    transition and rollback commands under Claude auto-mode."""
    allow = _schema_auto_mode()
    for pat in REVIEWER_FSM_PATTERNS:
        assert pat in allow, (
            f"auto_mode.allow missing REVIEWER FSM pattern {pat!r}"
        )


def test_helper_returns_recovery_patterns() -> None:
    """``_load_claude_settings_auto_mode_from_canon`` surfaces the
    recovery patterns so setup bakes them into the file."""
    allow = setup_mod._load_claude_settings_auto_mode_from_canon(
        find_canon_dir())
    for pat in RECOVERY_PATTERNS + REVIEWER_FSM_PATTERNS:
        assert pat in allow


# ---------- setup bakes them into settings.local.json ----------


def test_fresh_setup_writes_recovery_patterns(tmp_path: Path) -> None:
    """Fresh ``_build_settings_local_json`` puts the recovery
    patterns under autoMode.allow."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    allow = (data.get("autoMode") or {}).get("allow") or []
    for pat in RECOVERY_PATTERNS + REVIEWER_FSM_PATTERNS:
        assert pat in allow, (
            f"0313: settings.local.json autoMode.allow missing "
            f"{pat!r}"
        )


def test_setup_merge_adds_recovery_to_legacy_file(
    tmp_path: Path,
) -> None:
    """A legacy settings file (only the 0267 push patterns) gains
    the recovery patterns on setup re-run, preserving operator
    extras."""
    cclaude = tmp_path / ".claude"
    cclaude.mkdir()
    (cclaude / "settings.local.json").write_text(
        json.dumps({
            "permissions": {"allow": []},
            "autoMode": {"allow": ["$defaults",
                                    "Bash(custom-op:*)"]},
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    status = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert status == "extended"
    data = json.loads(
        (cclaude / "settings.local.json").read_text(encoding="utf-8")
    )
    allow = data["autoMode"]["allow"]
    for pat in RECOVERY_PATTERNS + REVIEWER_FSM_PATTERNS:
        assert pat in allow
    # Operator's custom entry survives.
    assert "Bash(custom-op:*)" in allow
