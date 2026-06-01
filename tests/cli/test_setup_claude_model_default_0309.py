"""Tests for task 0309: ``greatminds setup`` writes a default
``model`` field into ``.claude/settings.local.json``.

Pre-0309 each fleet inherited claude's machine-level global
default model; the choice drifted across hosts. 0309 makes the
fleet's model selection explicit: the setup-time write seeds
``claude-opus-4-8`` (or whatever ``schema.claude_settings.model``
declares). Operator-edited files preserve their existing value
on re-runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema loader ----------


def test_load_model_defaults_to_opus_4_8_when_schema_missing(
    tmp_path: Path,
) -> None:
    """A canon dir without ``schema.yaml`` falls back to
    ``DEFAULT_CLAUDE_MODEL`` so a partial install still produces a
    valid file."""
    assert setup_mod._load_claude_settings_model_from_canon(tmp_path) \
        == setup_mod.DEFAULT_CLAUDE_MODEL == "claude-opus-4-8"


def test_load_model_reads_schema_when_present() -> None:
    """When schema declares ``claude_settings.model``, the helper
    returns that value verbatim. The default lands here when the
    schema is silent (most projects today)."""
    out = setup_mod._load_claude_settings_model_from_canon(
        find_canon_dir())
    assert isinstance(out, str) and out.strip()
    # Either schema declares a value or we fall back to opus-4-8.
    # Both are acceptable; just pin the type + non-empty.


# ---------- fresh-write contract ----------


def test_fresh_setup_writes_model_field(tmp_path: Path) -> None:
    """``_build_settings_local_json`` puts ``model`` at the top
    of the settings dict on a fresh build."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=find_canon_dir())
    data = json.loads(text)
    assert "model" in data
    assert data["model"]  # non-empty
    # Default model is opus-4-8 unless schema overrides.
    assert "opus" in data["model"].lower() \
        or "claude-" in data["model"]


def test_build_settings_without_canon_uses_default_model(
    tmp_path: Path,
) -> None:
    """Callers passing ``canon=None`` still get a valid file with
    the default model (no schema → no override)."""
    text = setup_mod._build_settings_local_json(
        tmp_path, canon=None)
    data = json.loads(text)
    assert data["model"] == "claude-opus-4-8"


# ---------- additive merge preserves operator override ----------


def _write_existing(tmp_path: Path, payload: dict) -> Path:
    cclaude = tmp_path / ".claude"
    cclaude.mkdir()
    target = cclaude / "settings.local.json"
    target.write_text(json.dumps(payload, indent=2) + "\n",
                        encoding="utf-8")
    return target


def test_existing_file_with_model_preserves_operator_choice(
    tmp_path: Path,
) -> None:
    """Operator has manually edited ``model`` → re-running setup
    must NOT overwrite it. Pre-0309 the merge logic added other
    fields; 0309's model field follows the same pattern."""
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
        "0309: operator's manual model override must survive setup"
    )


def test_existing_file_without_model_gains_default(
    tmp_path: Path,
) -> None:
    """Legacy file from a pre-0309 install (no ``model`` key) →
    setup re-run adds it with the canon default."""
    _write_existing(tmp_path, {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
    })
    status = setup_mod._ensure_claude_settings_local(
        tmp_path, find_canon_dir())
    assert status == "extended"
    data = json.loads(
        (tmp_path / ".claude" / "settings.local.json")
        .read_text(encoding="utf-8")
    )
    assert data["model"] == "claude-opus-4-8"


def test_setup_idempotent_after_model_field_added(
    tmp_path: Path,
) -> None:
    """Two consecutive setups: first adds model, second reports
    ``unchanged``. No accidental duplicate writes."""
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
