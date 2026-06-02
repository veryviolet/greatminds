"""Tests for task 0203: setup.py resolves claude binary across npm-
install locations + surfaces plugin-install failure reasons.

Pre-0203 ``greatminds setup`` on Lattice (ssh non-login, claude at
``~/.local/bin/claude``) reported «14 failed» silently because
subprocess.run(["claude", ...]) raised FileNotFoundError and the
helper swallowed it into a count. Operator had to grep PATH manually.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod


# ---------- _resolve_claude_binary ----------


def test_resolve_uses_shutil_which_when_on_path(monkeypatch) -> None:
    """Happy path: claude on PATH → shutil.which returns its path
    and we use that."""
    monkeypatch.setattr(setup_mod.shutil, "which",
                        lambda name: "/usr/bin/claude")
    assert setup_mod._resolve_claude_binary() == "/usr/bin/claude"


def test_resolve_falls_back_to_local_bin(monkeypatch, tmp_path: Path) -> None:
    """0203 Lattice case: claude NOT on PATH but at
    ``~/.local/bin/claude`` (npm-install destination on Ubuntu). ssh
    non-login shells don't source ~/.profile, so PATH lacks
    ~/.local/bin. The fallback candidate list catches this case."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    fake_home = tmp_path / "home"
    (fake_home / ".local" / "bin").mkdir(parents=True)
    claude = fake_home / ".local" / "bin" / "claude"
    claude.write_text("#!/bin/sh\necho stub", encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: fake_home))

    resolved = setup_mod._resolve_claude_binary()
    assert resolved == str(claude)


def test_resolve_falls_back_to_npm_global(monkeypatch, tmp_path: Path) -> None:
    """Other common npm-install destination: ``~/.npm-global/bin``."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    fake_home = tmp_path / "home"
    (fake_home / ".npm-global" / "bin").mkdir(parents=True)
    claude = fake_home / ".npm-global" / "bin" / "claude"
    claude.write_text("stub", encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setattr(setup_mod.Path, "home", classmethod(lambda cls: fake_home))

    resolved = setup_mod._resolve_claude_binary()
    assert resolved == str(claude)


def test_resolve_returns_none_when_nowhere(monkeypatch, tmp_path: Path) -> None:
    """No claude binary anywhere → None → caller logs a clear error
    instead of silently failing every plugin install."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_mod.Path, "home",
                        classmethod(lambda cls: tmp_path / "empty-home"))
    assert setup_mod._resolve_claude_binary() is None


# ---------- _install_claude_plugins_for_role uses resolved path ----------


def test_install_uses_resolved_absolute_path(monkeypatch) -> None:
    """0203: the resolved absolute path (NOT bare 'claude') is what
    subprocess.run sees. Pin against accidental regression to bare
    binary name (which would silently fail on ssh non-login)."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/custom/bin/claude")
    calls: list[list[str]] = []
    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    setup_mod._install_claude_plugins_for_role(
        "DEVELOPER", ["postman"],
    )
    # Every command starts with the resolved binary path.
    for cmd in calls:
        assert cmd[0] == "/custom/bin/claude", (
            f"0203: subprocess.run got {cmd[0]!r}; expected the "
            f"resolved absolute path"
        )


def test_install_returns_all_failed_when_no_claude(monkeypatch) -> None:
    """0203 contract: no claude binary anywhere → every curated
    plugin counts as failed AND is named in the returned list. No
    silent zero counts."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: None)
    plugins = ["postman", "sentry", "playwright"]
    (inst_fresh, pres_prior, pres_dedupe, failed,
     failed_names) = setup_mod._install_claude_plugins_for_role(
        "TESTER", plugins,
    )
    assert inst_fresh == 0
    assert pres_prior == 0
    assert pres_dedupe == 0
    assert failed == len(plugins)
    assert sorted(failed_names) == sorted(plugins), (
        "0203: failed_names must include every curated plugin so "
        "the setup summary can name them, not just count"
    )


# ---------- PLANNER §7 amendment: 3-category preserved split ----------


def test_per_role_classifies_pre_campaign_preserved(monkeypatch) -> None:
    """0203 iter-2 (PLANNER §7): a plugin already in
    ``pre_campaign_installed`` snapshot classifies as
    preserved-prior, NOT preserved-dedupe."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/bin/claude")
    pre_campaign = {"postman"}     # already in claude home
    this_run: set[str] = set()      # nothing installed yet this run
    inst_fresh, pres_prior, pres_dedupe, failed, _ = (
        setup_mod._install_claude_plugins_for_role(
            "DEVELOPER", ["postman"],
            claude_bin="/bin/claude",
            pre_campaign_installed=pre_campaign,
            installed_this_run=this_run,
        )
    )
    assert pres_prior == 1
    assert pres_dedupe == 0
    assert inst_fresh == 0
    assert failed == 0


def test_per_role_classifies_dedupe_within_run(monkeypatch) -> None:
    """A plugin NOT in pre-campaign but already installed this run
    (by an earlier role) classifies as preserved-dedupe, NOT
    preserved-prior. This closes the «6 installed, 8 preserved»
    ambiguity Lattice flagged."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/bin/claude")
    pre_campaign: set[str] = set()           # nothing in claude home
    this_run = {"postman"}                    # already installed this run
    inst_fresh, pres_prior, pres_dedupe, _, _ = (
        setup_mod._install_claude_plugins_for_role(
            "TESTER", ["postman"],
            claude_bin="/bin/claude",
            pre_campaign_installed=pre_campaign,
            installed_this_run=this_run,
        )
    )
    assert pres_dedupe == 1
    assert pres_prior == 0
    assert inst_fresh == 0


def test_fresh_install_updates_this_run_set(monkeypatch) -> None:
    """A successful fresh install adds the name to ``installed_this_run``
    so the next role that needs it picks it up as dedupe."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/bin/claude")
    def fake_run(cmd, *a, **kw):
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    pre_campaign: set[str] = set()
    this_run: set[str] = set()
    setup_mod._install_claude_plugins_for_role(
        "DEVELOPER", ["postman"],
        claude_bin="/bin/claude",
        pre_campaign_installed=pre_campaign,
        installed_this_run=this_run,
    )
    assert "postman" in this_run, (
        "0203 iter-2: fresh install must register in installed_this_run "
        "so cross-role dedupe is observable"
    )


def test_aggregate_caller_threads_shared_state(monkeypatch,
                                                  tmp_path) -> None:
    """0203 iter-2 contract: ``_install_role_plugins_per_host``
    snapshots pre_campaign once and threads it (plus a mutable
    this-run set) through every per-role call. Pin against accidental
    regression to per-role snapshotting (which would conflate prior
    and dedupe again)."""
    import yaml as _yaml
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text(_yaml.safe_dump({
        "plugins": {
            "claude_marketplace": {
                "DEVELOPER": ["postman"],
                "TESTER":    ["postman"],  # same plugin in 2nd role
            },
        },
    }), encoding="utf-8")

    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/bin/claude")
    monkeypatch.setattr(setup_mod, "_claude_plugin_list_names",
                        lambda *_a, **_kw: set())  # nothing pre-existing

    install_calls: list[str] = []
    def fake_run(cmd, *a, **kw):
        if cmd[1:3] == ["plugin", "install"]:
            install_calls.append(cmd[3])
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    monkeypatch.delenv("GREATMINDS_SKIP_PLUGIN_INSTALL", raising=False)

    (inst_fresh, pres_prior, pres_dedupe, failed,
     failed_names) = setup_mod._install_role_plugins_per_host(
        canon, verbose=False,
    )
    # DEVELOPER installs postman fresh; TESTER sees it dedupe.
    assert inst_fresh == 1
    assert pres_prior == 0
    assert pres_dedupe == 1
    assert failed == 0
    # And only ONE actual claude plugin install command was issued.
    assert len(install_calls) == 1


def test_install_surfaces_stderr_for_per_plugin_failure(
    monkeypatch, capsys,
) -> None:
    """0203 contract: per-plugin install failure logs a one-liner
    with the stderr excerpt — regardless of --verbose. Pre-0203 the
    error was silenced behind a counter; Lattice debugged blind."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: "/bin/claude")
    def fake_run(cmd, *a, **kw):
        if cmd[1:3] == ["plugin", "list"]:
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        return subprocess.CompletedProcess(
            list(cmd), 1, "", "marketplace not registered\nsecondline",
        )
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    setup_mod._install_claude_plugins_for_role(
        "DEVELOPER", ["postman"], verbose=False,
    )
    cap = capsys.readouterr()
    # The warning lands on stderr (warn() helper). Check both
    # stderr and stdout (capsys reads from each separately, and
    # click.secho with err=True goes to stderr).
    combined = cap.err + cap.out
    assert "postman" in combined
    assert "marketplace not registered" in combined
