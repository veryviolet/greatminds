"""Tests for task 0273: ``setup`` bake must list ssh/scp/rsync under
``autoMode.allow`` in ``.claude/settings.local.json``, parallel to
the entries 0267 added under ``permissions.allow``.

Pre-0273 the classifier's auto-mode ceiling silently re-challenged
TESTER's stand probe (``ssh avatar``), STAND-KEEPER's rsync, and
file-transfer scp from any ``--permission-mode auto`` pane —
despite ``permissions.allow`` already permitting these verbs in
default mode. The two gates are independent; 0273 mirrors the
trio into the auto_mode list.
"""
from __future__ import annotations

import json
from pathlib import Path

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema source-of-truth ----------


def test_schema_auto_mode_allow_lists_ssh_scp_rsync() -> None:
    """``schema.claude_settings.auto_mode.allow`` must contain all
    three ops verbs in addition to the 0267 push patterns."""
    allow = setup_mod._load_claude_settings_auto_mode_from_canon(
        find_canon_dir())
    for required in ("Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)"):
        assert required in allow, (
            f"0273: schema.claude_settings.auto_mode.allow missing "
            f"{required!r} — auto-mode panes will be challenged for "
            "session auth on these verbs"
        )
    # Regression net for the 0267 entries — adding 0273's three must
    # not displace them.
    assert "$defaults" in allow
    assert "Bash(git push origin main:*)" in allow


# ---------- _build_settings_local_json ----------


def test_fresh_setup_writes_ssh_scp_rsync_into_auto_mode(
    tmp_path: Path,
) -> None:
    """End-to-end: ``_build_settings_local_json`` populates
    ``autoMode.allow`` from schema; the file landing on a fresh fleet
    therefore lists the trio."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    allow = (data.get("autoMode") or {}).get("allow") or []
    for required in ("Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)"):
        assert required in allow


# ---------- _ensure_claude_settings_local additive merge ----------


def _write_existing(tmp_path: Path, payload: dict) -> Path:
    cclaude = tmp_path / ".claude"
    cclaude.mkdir()
    target = cclaude / "settings.local.json"
    target.write_text(json.dumps(payload, indent=2) + "\n",
                       encoding="utf-8")
    return target


def test_existing_file_extended_with_ssh_scp_rsync(tmp_path: Path) -> None:
    """A legacy file with only the 0267 push patterns under
    ``autoMode.allow`` must gain the three ops entries on next setup
    re-run (additive merge — the operator's customizations stay)."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults",
                               "Bash(git push origin main:*)",
                               "Bash(custom-op:*)"]},
    })
    status = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert status == "extended"
    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    auto = data["autoMode"]["allow"]
    for required in ("Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)"):
        assert required in auto
    # Operator's custom rule survived.
    assert "Bash(custom-op:*)" in auto
    # 0267 patterns also still present.
    assert "Bash(git push origin main:*)" in auto


def test_idempotent_after_ssh_extend(tmp_path: Path) -> None:
    """Re-running setup once the trio is present must report
    ``unchanged`` and not duplicate any rule."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
    })
    setup_mod._ensure_claude_settings_local(tmp_path, find_canon_dir())
    second = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert second == "unchanged"
    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    auto = data["autoMode"]["allow"]
    assert len(auto) == len(set(auto))  # no duplicates
