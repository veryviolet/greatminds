"""Tests for task 0191: ``greatminds setup`` writes/extends
``<project>/.claude/settings.local.json`` with the schema-canonical
``permissions.allow`` rules.

Pre-0191 the file was written ONLY on greenfield projects with an
empty allow list — operators had to hand-edit it to unblock /loop
REVIEWER's git ops. Post-0191 schema.yaml's ``claude_settings:``
section is the source of truth; setup merges the canonical allow
rules into any existing file, preserving operator's hook + autoMode
entries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema carries the section ----------


def test_schema_has_claude_settings_section() -> None:
    """0191 schema pin: ``data/schema.yaml`` carries the
    ``claude_settings:`` section with the 8 git-op allow rules."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    cs = doc.get("claude_settings")
    assert cs is not None, "0191: schema.yaml missing 'claude_settings:'"
    allow = (cs.get("permissions") or {}).get("allow") or []
    for rule in (
        "Bash(git add:*)", "Bash(git commit:*)", "Bash(git tag:*)",
        "Bash(git push:*)", "Bash(git merge:*)",
        "Bash(git worktree:*)", "Bash(git checkout:*)",
        "Bash(git branch:*)",
    ):
        assert rule in allow, f"0191: missing canonical allow rule {rule!r}"


def test_load_claude_settings_allow_returns_list(tmp_path: Path) -> None:
    """Loader reads schema cleanly."""
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(yaml.safe_dump({
        "claude_settings": {
            "permissions": {"allow": ["Bash(foo:*)", "Bash(bar:*)"]}
        }
    }), encoding="utf-8")
    out = setup_mod._load_claude_settings_allow_from_canon(canon)
    assert out == ["Bash(foo:*)", "Bash(bar:*)"]


def test_load_claude_settings_allow_returns_empty_when_missing(
    tmp_path: Path,
) -> None:
    """Schema without the section → empty list (caller writes Stop
    hook only, no allow rules)."""
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text("version: 1\n", encoding="utf-8")
    assert setup_mod._load_claude_settings_allow_from_canon(canon) == []


# ---------- _ensure_claude_settings_local ----------


def _write_canon(tmp_path: Path, allow: list[str] | None = None) -> Path:
    """Write a minimal canon with claude_settings."""
    canon = tmp_path / "canon"
    canon.mkdir(parents=True, exist_ok=True)
    body = {"claude_settings": {"permissions": {"allow": allow or [
        "Bash(git commit:*)", "Bash(git push:*)",
    ]}}}
    (canon / "schema.yaml").write_text(
        yaml.safe_dump(body), encoding="utf-8",
    )
    return canon


def test_ensure_creates_file_with_schema_allow_when_missing(
    tmp_path: Path,
) -> None:
    """Greenfield: no settings.local.json → write template carrying
    schema's allow rules + Stop hook + autoMode."""
    canon = _write_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    status = setup_mod._ensure_claude_settings_local(project, canon)
    assert status == "written"
    doc = json.loads(
        (project / ".claude" / "settings.local.json").read_text(
            encoding="utf-8"
        )
    )
    assert doc["permissions"]["allow"] == [
        "Bash(git commit:*)", "Bash(git push:*)",
    ]
    assert doc["autoMode"]["allow"] == ["$defaults"]
    assert "Stop" in doc["hooks"]


def test_ensure_extends_existing_allow_via_union(tmp_path: Path) -> None:
    """File exists with operator's own allow rules → setup unions
    schema's allow into it, dedup, and PRESERVES operator's rules.

    This is the load-bearing 0191 contract: operators may add their
    own per-project rules and setup must never drop them."""
    canon = _write_canon(tmp_path, allow=[
        "Bash(git commit:*)", "Bash(git push:*)",
    ])
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    pre_existing = {
        "permissions": {"allow": [
            "Bash(npm install:*)",  # operator's own rule
            "Bash(git commit:*)",   # already present (will dedup)
        ]},
        "autoMode": {"allow": ["$defaults"]},
        "hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "echo operator-custom"}
        ]}]},
    }
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps(pre_existing, indent=2), encoding="utf-8",
    )

    status = setup_mod._ensure_claude_settings_local(project, canon)
    assert status == "extended"

    doc = json.loads(
        (project / ".claude" / "settings.local.json").read_text(
            encoding="utf-8"
        )
    )
    # Operator's rule preserved.
    assert "Bash(npm install:*)" in doc["permissions"]["allow"]
    # Schema's rule added.
    assert "Bash(git push:*)" in doc["permissions"]["allow"]
    # Dedup — git commit appears once.
    assert doc["permissions"]["allow"].count("Bash(git commit:*)") == 1
    # Operator's hook command preserved (NOT replaced).
    cmd = doc["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd == "echo operator-custom"


def test_ensure_idempotent_returns_unchanged_on_rerun(tmp_path: Path) -> None:
    """Two consecutive runs: first extends, second is a no-op."""
    canon = _write_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._ensure_claude_settings_local(project, canon)  # written
    status2 = setup_mod._ensure_claude_settings_local(project, canon)
    assert status2 == "unchanged"


def test_ensure_handles_corrupt_json_without_crash(tmp_path: Path) -> None:
    """settings.local.json contains invalid JSON → return
    ``unreadable``; don't overwrite (operator owns the file)."""
    canon = _write_canon(tmp_path)
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        "{ this is not json", encoding="utf-8",
    )
    status = setup_mod._ensure_claude_settings_local(project, canon)
    assert status == "unreadable"
    # File is left as-is.
    text = (project / ".claude" / "settings.local.json").read_text(
        encoding="utf-8"
    )
    assert "this is not json" in text


def test_ensure_handles_missing_allow_field_in_existing(tmp_path: Path) -> None:
    """Existing file has permissions.{} without an allow list →
    setup adds the schema's allow list."""
    canon = _write_canon(tmp_path, allow=["Bash(git push:*)"])
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {}, "hooks": {"Stop": []}}),
        encoding="utf-8",
    )
    status = setup_mod._ensure_claude_settings_local(project, canon)
    assert status == "extended"
    doc = json.loads(
        (project / ".claude" / "settings.local.json").read_text(
            encoding="utf-8"
        )
    )
    assert doc["permissions"]["allow"] == ["Bash(git push:*)"]
    # Existing hooks preserved as {}.
    assert "hooks" in doc


# ---------- regression pin: real canon writes the 8 git-op rules ----------


def test_real_canon_writes_eight_git_ops(tmp_path: Path) -> None:
    """End-to-end: using the real packaged canon (find_canon_dir())
    setup writes all 8 git-op allow rules into a greenfield project's
    settings.local.json. Pin against accidental schema deletion."""
    canon = find_canon_dir()
    project = tmp_path / "project"
    project.mkdir()
    status = setup_mod._ensure_claude_settings_local(project, canon)
    assert status == "written"
    doc = json.loads(
        (project / ".claude" / "settings.local.json").read_text(
            encoding="utf-8"
        )
    )
    allow = doc["permissions"]["allow"]
    for rule in (
        "Bash(git add:*)", "Bash(git commit:*)", "Bash(git tag:*)",
        "Bash(git push:*)", "Bash(git merge:*)",
        "Bash(git worktree:*)", "Bash(git checkout:*)",
        "Bash(git branch:*)",
    ):
        assert rule in allow
