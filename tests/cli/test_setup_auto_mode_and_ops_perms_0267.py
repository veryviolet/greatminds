"""Tests for task 0267: ``greatminds setup`` bake of
``.claude/settings.local.json`` must include autoMode push patterns
+ ops permissions (ssh/scp/rsync/git revert).

Pre-0267 fresh setups landed with ``autoMode.allow: ["$defaults"]``
and ``permissions.allow`` carrying only the git-mainline ops. The
classifier's auto-mode ceiling silently blocked ``git push origin
main`` even when ``Bash(git push:*)`` was present; TESTER's stand
probe (``ssh avatar``) and STAND-KEEPER's rsync also got blocked
with no allow entry. Operators worked around it via the ``!`` prefix
or by hand-editing the settings file — both lost on re-setup.

0267 ships schema-driven canonical lists so fresh deploys get the
right set out of the box.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema source-of-truth ----------


def test_schema_claude_settings_auto_mode_includes_push_origin_main() -> None:
    """The canonical autoMode list MUST carry the push-origin-main
    patterns. Without these the classifier blocks REVIEWER's cut and
    push pipeline on every release."""
    auto = setup_mod._load_claude_settings_auto_mode_from_canon(
        find_canon_dir())
    assert "$defaults" in auto
    assert "Bash(git push origin main:*)" in auto
    assert "Bash(git push origin main --follow-tags:*)" in auto
    assert "Bash(git push --follow-tags:*)" in auto


def test_schema_claude_settings_permissions_allow_includes_ops() -> None:
    """``permissions.allow`` must include the ops triad TESTER (ssh,
    scp) and STAND-KEEPER (rsync) depend on, plus ``git revert`` for
    REVIEWER's rollback path."""
    allow = setup_mod._load_claude_settings_allow_from_canon(
        find_canon_dir())
    for required in ("Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)",
                     "Bash(git revert:*)"):
        assert required in allow, (
            f"0267: schema.claude_settings.permissions.allow missing "
            f"{required!r} — TESTER/STAND-KEEPER/REVIEWER lose this verb"
        )


def test_load_auto_mode_defaults_when_schema_missing(tmp_path: Path) -> None:
    """Defensive: a canon dir without schema.yaml returns ``["$defaults"]``
    so a partial install still produces a valid settings file."""
    assert setup_mod._load_claude_settings_auto_mode_from_canon(tmp_path) \
        == ["$defaults"]


# ---------- _build_settings_local_json ----------


def test_build_settings_writes_autoMode_from_schema(tmp_path: Path) -> None:
    """Fresh-write contract: ``_build_settings_local_json`` puts the
    schema-driven list under ``autoMode.allow`` — NOT the pre-0267
    hardcoded ``["$defaults"]`` placeholder."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    allow = (data.get("autoMode") or {}).get("allow") or []
    assert "$defaults" in allow
    assert "Bash(git push origin main:*)" in allow
    assert "Bash(git push --follow-tags:*)" in allow


def test_build_settings_writes_ops_perms_from_schema(tmp_path: Path) -> None:
    """``permissions.allow`` from a fresh build includes the ops triad."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    allow = (data.get("permissions") or {}).get("allow") or []
    for required in ("Bash(ssh:*)", "Bash(scp:*)", "Bash(rsync:*)",
                     "Bash(git revert:*)"):
        assert required in allow


def test_build_settings_without_canon_keeps_defaults_only(tmp_path: Path) -> None:
    """Backwards-compat: callers that pass ``canon=None`` still get a
    valid file with autoMode.allow=['$defaults'] (no schema =
    no extra rules)."""
    text = setup_mod._build_settings_local_json(tmp_path, canon=None)
    data = json.loads(text)
    assert data["autoMode"]["allow"] == ["$defaults"]
    # permissions stays empty without canon — pre-0267 contract preserved.
    assert data["permissions"]["allow"] == []


# ---------- _ensure_claude_settings_local (additive merge) ----------


def _write_existing(tmp_path: Path, payload: dict) -> Path:
    cclaude = tmp_path / ".claude"
    cclaude.mkdir()
    target = cclaude / "settings.local.json"
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def test_ensure_extends_autoMode_into_legacy_file(tmp_path: Path) -> None:
    """A pre-0267 file with ``autoMode.allow: ["$defaults"]`` should
    gain the schema's push-pattern entries without losing the
    operator's own customizations elsewhere in the file."""
    target = _write_existing(tmp_path, {
        "permissions": {"allow": ["Bash(git add:*)"]},
        "autoMode": {"allow": ["$defaults"]},
        "hooks": {"Stop": [{"matcher": "",
                            "hooks": [{"type": "command",
                                       "command": "/operator/hook"}]}]},
    })

    status = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert status == "extended"

    data = json.loads(target.read_text(encoding="utf-8"))
    auto = (data.get("autoMode") or {}).get("allow") or []
    assert "$defaults" in auto
    assert "Bash(git push origin main:*)" in auto
    # Operator's hook untouched.
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "/operator/hook"
    # Permissions also extended with the new ops verbs.
    perms = (data.get("permissions") or {}).get("allow") or []
    assert "Bash(ssh:*)" in perms
    assert "Bash(rsync:*)" in perms


def test_ensure_is_idempotent_after_extend(tmp_path: Path) -> None:
    """Re-running setup must NOT duplicate any rule and must report
    ``unchanged`` on the second pass."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
    })

    first = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    second = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert first == "extended"
    assert second == "unchanged"

    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    # Each rule appears at most once.
    auto = data["autoMode"]["allow"]
    assert len(auto) == len(set(auto)), \
        f"0267: duplicates in autoMode.allow after re-run: {auto}"
    perms = data["permissions"]["allow"]
    assert len(perms) == len(set(perms))


def test_ensure_preserves_operator_extra_auto_mode_entries(
    tmp_path: Path,
) -> None:
    """An operator who added their own ``autoMode.allow`` entry must
    keep it after setup: the merge is union-by-set, not replace."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults",
                               "Bash(custom-op:*)"]},
    })
    setup_mod._ensure_claude_settings_local(tmp_path, find_canon_dir())

    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    auto = data["autoMode"]["allow"]
    assert "Bash(custom-op:*)" in auto, (
        "0267: operator's own autoMode rule must survive setup re-runs"
    )
    assert "Bash(git push origin main:*)" in auto


def test_ensure_creates_autoMode_when_missing_in_existing_file(
    tmp_path: Path,
) -> None:
    """A legacy file without an ``autoMode`` top-level key at all
    must gain one with the canonical list."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
    })
    setup_mod._ensure_claude_settings_local(tmp_path, find_canon_dir())

    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    assert "autoMode" in data
    assert "$defaults" in data["autoMode"]["allow"]
    assert "Bash(git push origin main:*)" in data["autoMode"]["allow"]
