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
