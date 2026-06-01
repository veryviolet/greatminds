"""Tests for task 0310: revert 0309 — ``greatminds setup`` must
NOT write a ``model`` field into ``.claude/settings.local.json``.

USER directive 2026-06-01: model selection belongs to the
machine-level ``~/.claude/settings.json`` or the interactive
``/model`` command. Pinning it in project settings created
unwanted coupling and blocked per-machine choice. 0310 removes
the 0309 model-write block + its helper/constant.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- fresh write: no model key ----------


def test_fresh_settings_has_no_model_key(tmp_path: Path) -> None:
    """0310: a fresh ``_build_settings_local_json`` must NOT emit a
    ``model`` field."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    assert "model" not in data, (
        f"0310: settings.local.json must not carry a model key "
        f"(got {data.get('model')!r})"
    )


def test_fresh_settings_without_canon_has_no_model(
    tmp_path: Path,
) -> None:
    """Same with ``canon=None`` — no model field on any path."""
    text = setup_mod._build_settings_local_json(tmp_path, canon=None)
    data = json.loads(text)
    assert "model" not in data


def test_fresh_settings_still_has_permissions_and_hooks(
    tmp_path: Path,
) -> None:
    """Regression net: removing the model field must not disturb
    the rest of the settings dict."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    assert "permissions" in data
    assert "autoMode" in data
    assert "hooks" in data


# ---------- 0309 symbols removed ----------


def test_default_claude_model_constant_removed() -> None:
    """0310: ``DEFAULT_CLAUDE_MODEL`` from 0309 must be deleted."""
    assert not hasattr(setup_mod, "DEFAULT_CLAUDE_MODEL"), (
        "0310: DEFAULT_CLAUDE_MODEL constant must be removed"
    )


def test_load_model_helper_removed() -> None:
    """0310: ``_load_claude_settings_model_from_canon`` from 0309
    must be deleted."""
    assert not hasattr(
        setup_mod, "_load_claude_settings_model_from_canon"
    ), (
        "0310: _load_claude_settings_model_from_canon helper must "
        "be removed"
    )


# ---------- merge path leaves operator model untouched ----------


def _write_existing(tmp_path: Path, payload: dict) -> Path:
    cclaude = tmp_path / ".claude"
    cclaude.mkdir()
    target = cclaude / "settings.local.json"
    target.write_text(json.dumps(payload, indent=2) + "\n",
                        encoding="utf-8")
    return target


def test_merge_does_not_add_model_to_legacy_file(
    tmp_path: Path,
) -> None:
    """A legacy file WITHOUT a model field must STAY without one
    after setup — 0310 reverts the 0309 auto-add."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
    })
    setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    assert "model" not in data, (
        "0310: setup must NOT inject a model field on merge"
    )


def test_merge_preserves_operator_model_if_present(
    tmp_path: Path,
) -> None:
    """If the operator manually added a ``model`` field, setup
    must leave it untouched (setup doesn't touch the key at all
    post-0310)."""
    _write_existing(tmp_path, {
        "model": "claude-sonnet-4-6",
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
    })
    setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    assert data["model"] == "claude-sonnet-4-6", (
        "0310: operator's manual model value must survive (setup "
        "doesn't touch the key)"
    )
