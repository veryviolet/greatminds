"""Tests for `greatminds setup` hook-path generation.

Bug context: `setup` previously wrote `.claude/settings.local.json`
with hook commands containing a bare ``greatminds`` token. Claude
sessions opened in the project without the greatminds venv on PATH
crashed every hook with ``/bin/sh: 1: greatminds: not found``.

Contract under test: every hook command line in the generated
``settings.local.json`` begins with an absolute reference to
greatminds (``shutil.which`` result, or a ``<python> -m
greatminds.cli.main`` fallback).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod


def _collect_command_hooks(settings: dict) -> list[str]:
    """Walk every event in hooks.* and return all command strings."""
    out: list[str] = []
    for _event, entries in (settings.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for h in entry.get("hooks") or []:
                if isinstance(h, dict) and h.get("type") == "command":
                    cmd = h.get("command")
                    if isinstance(cmd, str):
                        out.append(cmd)
    return out


def test_settings_local_json_uses_absolute_greatminds_path(tmp_path, monkeypatch):
    """which() resolves → that absolute path goes verbatim into the command."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda name: "/abs/bin/greatminds" if name == "greatminds" else None,
    )

    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)
    cmds = _collect_command_hooks(data)

    assert cmds, "no command hooks emitted"
    for cmd in cmds:
        assert cmd.startswith("/abs/bin/greatminds "), \
            f"command must start with the resolved absolute path: {cmd!r}"


def test_settings_local_json_normalizes_relative_which_result(tmp_path, monkeypatch):
    """`shutil.which` may return a relative path if PATH has a relative
    entry (e.g. ``.venv/bin``). The command embedded in the JSON must
    still be an absolute path — we normalize via ``Path.resolve()``.
    """
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: ".venv/bin/greatminds")

    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)
    cmds = _collect_command_hooks(data)

    expected_prefix = str(Path(".venv/bin/greatminds").resolve()) + " "
    assert cmds
    for cmd in cmds:
        assert cmd.startswith("/"), \
            f"hook command must be absolute even when which() returned relative: {cmd!r}"
        assert cmd.startswith(expected_prefix), \
            f"command must start with resolved absolute path {expected_prefix!r}: {cmd!r}"


def test_settings_local_json_falls_back_to_python_module(tmp_path, monkeypatch):
    """which() returns None → fall back to `<sys.executable> -m greatminds.cli.main`."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)

    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)
    cmds = _collect_command_hooks(data)

    assert cmds
    prefix = f"{sys.executable} -m greatminds.cli.main "
    for cmd in cmds:
        assert cmd.startswith(prefix), \
            f"fallback must start with `{prefix}`: {cmd!r}"


def test_no_hook_command_starts_with_bare_greatminds(tmp_path, monkeypatch):
    """The bug invariant: NO command line may start with bare 'greatminds '."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda name: "/abs/bin/greatminds",
    )
    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)
    cmds = _collect_command_hooks(data)

    assert cmds
    for cmd in cmds:
        # Must start with absolute path (/) or sys.executable's absolute path.
        assert cmd.startswith("/"), \
            f"hook command must start with absolute path, not bare token: {cmd!r}"
        assert not cmd.startswith("greatminds "), \
            f"hook command starts with the bare 'greatminds' token: {cmd!r}"


def test_stop_hook_command_carries_role_and_project_dir(tmp_path, monkeypatch):
    """The Stop command keeps its argv: stop-decide ROLE --host claude --project-dir DIR."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda name: "/abs/bin/greatminds",
    )
    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)

    stop_entries = data["hooks"]["Stop"]
    assert len(stop_entries) == 1
    inner = stop_entries[0]["hooks"]
    assert len(inner) == 1
    cmd = inner[0]["command"]

    assert " stop-decide " in cmd
    assert '"${GREATMINDS_ROLE:-UNKNOWN}"' in cmd
    assert "--host claude" in cmd
    assert f"--project-dir {tmp_path}" in cmd


def test_idempotent_re_resolution_picks_up_new_venv_path(tmp_path, monkeypatch):
    """Each call re-runs shutil.which → new venv location is reflected."""
    monkeypatch.setattr(
        setup_mod.shutil, "which", lambda name: "/old/bin/greatminds",
    )
    a = setup_mod._build_settings_local_json(tmp_path)
    assert "/old/bin/greatminds" in a

    monkeypatch.setattr(
        setup_mod.shutil, "which", lambda name: "/new/bin/greatminds",
    )
    b = setup_mod._build_settings_local_json(tmp_path)
    assert "/new/bin/greatminds" in b
    assert "/old/bin/greatminds" not in b


def test_generated_json_is_valid(tmp_path, monkeypatch):
    """Sanity: the file we write parses as JSON and has expected shape."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda name: "/abs/bin/greatminds",
    )
    content = setup_mod._build_settings_local_json(tmp_path)
    data = json.loads(content)  # raises on invalid JSON

    assert data["permissions"]["allow"] == []
    assert data["autoMode"]["allow"] == ["$defaults"]
    assert "Stop" in data["hooks"]
