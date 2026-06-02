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


# ---------------------------------------------------------------------------
# task 0047: shipped codex profile copy to ~/.codex/<role>.config.toml.
# OBSOLETED by 0158: codex 0.130 stopped reading ~/.codex/<role>.config.toml;
# greatminds setup now installs per-role $CODEX_HOME dirs at
# <project>/coordination/.codex-home/<role>/ instead. The 0047 helper
# (``_copy_codex_profiles_if_missing``) was removed; 0158 coverage lives
# in tests/cli/test_codex_home_setup_0158.py.
# ---------------------------------------------------------------------------


def test_shipped_codex_profiles_have_no_internal_brand_leaks():
    """Regression for task 0047 part B: scrubbed profiles must not
    reference internal product names from prior codebases."""
    from greatminds.core.paths import find_canon_dir
    profiles_dir = find_canon_dir() / "codex" / "profiles"
    assert profiles_dir.is_dir()
    profiles = list(profiles_dir.glob("*.config.toml"))
    assert profiles, "shipped codex profiles missing from canon"
    forbidden = ("Guardora", "/opt/guardora", "lattice")
    for p in profiles:
        text = p.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, (
                f"{p.name} still contains internal-brand leak {needle!r}"
            )


def test_shipped_codex_profiles_cover_loop_mode_roles():
    """Regression for task 0047 part C: every codex-realistic loop-mode
    role MUST have a shipped profile so the agent gets role guidance
    when launched on codex via --profile <role-lower>."""
    from greatminds.core.paths import find_canon_dir
    profiles_dir = find_canon_dir() / "codex" / "profiles"
    shipped = {p.stem.replace(".config", "") for p in
               profiles_dir.glob("*.config.toml")}
    # The set of roles that PLANNER's 0047 revised scope flagged: any
    # loop-mode role where codex is a realistic launcher. Chat-mode
    # roles (planner, maintainer) and stand-keeper (chat) are excluded.
    expected = {
        "architect-reviewer", "developer", "ui-developer",
        "tester", "reader", "explorer", "technical-writer",
    }
    missing = expected - shipped
    assert not missing, f"shipped codex profiles missing: {missing}"


# ---------------------------------------------------------------------------
# task 0076: pre-trust config installer (opt-in via --pre-trust)
# ---------------------------------------------------------------------------


def test_install_claude_pretrust_writes_entry_in_empty_config(tmp_path, monkeypatch):
    """No prior ~/.claude.json → file created with our project marked
    hasTrustDialogAccepted: true."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    result = setup_mod._install_claude_pretrust(proj)
    assert result == "written"

    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    abs_proj = str(proj.resolve())
    assert data["projects"][abs_proj]["hasTrustDialogAccepted"] is True


def test_install_claude_pretrust_idempotent(tmp_path, monkeypatch):
    """Second call when already True → 'existing', file unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    setup_mod._install_claude_pretrust(proj)  # first call
    result = setup_mod._install_claude_pretrust(proj)  # second call
    assert result == "existing"


def test_install_claude_pretrust_preserves_other_projects(tmp_path, monkeypatch):
    """Other projects' entries MUST NOT be touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    # Pre-seed with another project's entry.
    pre = {
        "projects": {
            "/other/project": {
                "hasTrustDialogAccepted": True,
                "allowedTools": ["bash"],
            },
        },
        "numStartups": 42,
    }
    (home / ".claude.json").write_text(json.dumps(pre), encoding="utf-8")

    setup_mod._install_claude_pretrust(proj)
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"]["/other/project"]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/other/project"]["allowedTools"] == ["bash"]
    assert data["numStartups"] == 42
    abs_proj = str(proj.resolve())
    assert data["projects"][abs_proj]["hasTrustDialogAccepted"] is True


def test_install_codex_pretrust_writes_entry_in_empty_config(tmp_path, monkeypatch):
    """No prior ~/.codex/config.toml → file created with our project
    in a [projects."<abs>"] section trust_level = "trusted"."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    result = setup_mod._install_codex_pretrust(proj)
    assert result == "written"

    content = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    abs_proj = str(proj.resolve())
    assert f'[projects."{abs_proj}"]' in content
    assert 'trust_level = "trusted"' in content


def test_install_codex_pretrust_idempotent(tmp_path, monkeypatch):
    """Second call → 'existing', file content unchanged."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    setup_mod._install_codex_pretrust(proj)
    before = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    result = setup_mod._install_codex_pretrust(proj)
    assert result == "existing"
    after = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert before == after


def test_install_codex_pretrust_append_only_preserves_existing(tmp_path, monkeypatch):
    """Existing config.toml with model, other projects, etc. — pre-trust
    appends ONLY a new section for our project. Existing content
    (including other projects' trust_level) untouched."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    proj = tmp_path / "toy"
    proj.mkdir()

    pre = (
        'model = "gpt-5.5"\n'
        '\n'
        '[projects."/other/proj"]\n'
        'trust_level = "untrusted"\n'  # opposite of trusted; must be preserved
    )
    (home / ".codex" / "config.toml").write_text(pre, encoding="utf-8")

    setup_mod._install_codex_pretrust(proj)
    after = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    # Preserved:
    assert 'model = "gpt-5.5"' in after
    assert '[projects."/other/proj"]' in after
    assert 'trust_level = "untrusted"' in after
    # Added:
    abs_proj = str(proj.resolve())
    assert f'[projects."{abs_proj}"]' in after


def test_install_claude_pretrust_handles_malformed_json(tmp_path, monkeypatch):
    """Malformed ~/.claude.json doesn't crash setup — returns
    'skipped: ...' diagnostic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: home))
    (home / ".claude.json").write_text("{this is not json", encoding="utf-8")
    proj = tmp_path / "toy"
    proj.mkdir()

    result = setup_mod._install_claude_pretrust(proj)
    assert result.startswith("skipped:")
    assert "unreadable" in result


def test_driven_codex_profiles_carry_heartbeat_directive():
    """Every DRIVEN codex role's profile MUST tell the agent to call a
    read-only greatminds CLI at the FIRST step of every tick. That
    call's side effect is the heartbeat write that coordd's in-flight-
    turn hang detector reads while the role's run-lock is held — without
    it a long driven turn would look hung.

    Interactive / self-loop roles (the PLANNER chat seat, the MAINTAINER
    watchdog) run no coordd-driven turns, so the per-tick heartbeat
    directive does not apply to them and their profiles are exempt."""
    import yaml
    from greatminds.core.paths import find_canon_dir
    canon = find_canon_dir()
    roles = (yaml.safe_load(
        (canon / "schema.yaml").read_text(encoding="utf-8")) or {}
    ).get("roles") or {}
    profiles_dir = canon / "codex" / "profiles"
    profiles = list(profiles_dir.glob("*.config.toml"))
    assert profiles, "no shipped codex profiles found"
    checked = 0
    for p in profiles:
        role_lower = p.stem.replace(".config", "")
        if (roles.get(role_lower.upper()) or {}).get("lifecycle") != "driven":
            continue
        checked += 1
        text = p.read_text(encoding="utf-8")
        assert "## Mandatory tick heartbeat" in text, (
            f"{p.name} missing heartbeat header"
        )
        # The directive must instruct a read-only greatminds CLI call as
        # the heartbeat-writing first step. The exact command is
        # role-specific (e.g. ``greatminds task list <queue>`` for a
        # queue-claiming worker, ``greatminds stand status`` for SK).
        assert "greatminds " in text, (
            f"{p.name} missing a read-only greatminds CLI directive"
        )
        assert f"heartbeat.{role_lower}" in text, (
            f"{p.name} missing 'heartbeat.{role_lower}' reference"
        )
        assert "FIRST step of every tick" in text, (
            f"{p.name} missing first-step-every-tick emphasis"
        )
    assert checked, "expected at least one driven codex profile to check"
