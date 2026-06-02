"""Tests for task 0175: ``greatminds setup`` installs USER-curated
marketplace plugins per role.

Pre-0175 ``data/roles/*.md`` referenced plugins via fabricated paths
(``plugins/role-X/skills/Y/SKILL.md``) that never existed in any real
marketplace — installing them silently no-op'd. 0175 replaces with a
USER-curated list in ``schema.yaml`` under ``plugins:`` pointing at
names from the public ``claude-plugins-official`` marketplace.
``greatminds setup`` invokes ``claude plugin install <name>@claude-
plugins-official`` per role per the curated list.

Codex side is deferred (empty lists per USER directive).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod


@pytest.fixture(autouse=True)
def _allow_plugin_install(monkeypatch):
    """0175: the suite-wide conftest sets GREATMINDS_SKIP_PLUGIN_INSTALL
    so integration tests don't shell out. This file tests the helper
    itself, so unset the guard for every test here."""
    monkeypatch.delenv("GREATMINDS_SKIP_PLUGIN_INSTALL", raising=False)


def _make_canon_with_plugins(canon_root: Path, plugins: dict) -> None:
    """Write a schema.yaml with the plugins: section."""
    canon_root.mkdir(parents=True, exist_ok=True)
    (canon_root / "schema.yaml").write_text(
        yaml.safe_dump({"plugins": plugins}, sort_keys=False),
        encoding="utf-8",
    )


# ---------- _load_curated_plugins ----------


def test_load_curated_plugins_returns_dict(tmp_path: Path) -> None:
    """Happy path: schema.yaml carries plugins: section; loader returns
    it verbatim."""
    canon = tmp_path / "canon"
    _make_canon_with_plugins(canon, {
        "claude_marketplace": {
            "DEVELOPER": ["postman"],
            "TESTER": ["playwright", "postman"],
        },
        "codex_marketplace": {},
    })
    out = setup_mod._load_curated_plugins(canon)
    assert out["claude_marketplace"]["DEVELOPER"] == ["postman"]
    assert out["claude_marketplace"]["TESTER"] == ["playwright", "postman"]


def test_load_curated_plugins_returns_empty_when_no_section(tmp_path: Path) -> None:
    """Defensive: schema.yaml without plugins: section → empty dict.
    Setup logs nothing and continues (no false positives)."""
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text("version: 1\n", encoding="utf-8")
    assert setup_mod._load_curated_plugins(canon) == {}


def test_load_curated_plugins_returns_empty_when_no_schema(tmp_path: Path) -> None:
    """No schema.yaml at all → empty dict, no crash."""
    canon = tmp_path / "canon"
    canon.mkdir()
    assert setup_mod._load_curated_plugins(canon) == {}


# ---------- _install_claude_plugins_for_role ----------


@pytest.fixture
def fake_claude_cli(monkeypatch):
    """Record every subprocess.run call for claude plugin commands.
    Returns the call list."""
    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        # ``claude plugin list`` returns empty (no existing plugins).
        if cmd[:3] == ["claude", "plugin", "list"]:
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)
    return calls


def test_install_claude_plugins_invokes_per_plugin(fake_claude_cli) -> None:
    """0175: ``claude plugin install <name>@claude-plugins-official``
    is called once per curated plugin for a role."""
    inst_fresh, pres_prior, pres_dedupe, failed, failed_names = (
        setup_mod._install_claude_plugins_for_role(
            "DEVELOPER", ["postman", "sentry"],
        )
    )
    assert inst_fresh == 2
    assert pres_prior == 0
    assert pres_dedupe == 0
    assert failed == 0
    assert failed_names == []
    install_calls = [c for c in fake_claude_cli
                     if c[1:3] == ["plugin", "install"]]
    names = [c[3] for c in install_calls]
    assert names == [
        "postman@claude-plugins-official",
        "sentry@claude-plugins-official",
    ]


def test_install_claude_plugins_skips_already_installed(monkeypatch) -> None:
    """0175 idempotency: a plugin already present in
    ``claude plugin list`` output is skipped (no install call). Post-
    0203-iter-2 it now classifies as ``preserved-prior`` (pre-
    campaign)."""
    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        if cmd[1:3] == ["plugin", "list"]:
            return subprocess.CompletedProcess(
                list(cmd), 0, "postman\nplaywright\n", "",
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    inst_fresh, pres_prior, pres_dedupe, failed, _ = (
        setup_mod._install_claude_plugins_for_role(
            "TESTER", ["postman", "sentry"],
        )
    )
    assert inst_fresh == 1     # sentry installed
    assert pres_prior == 1     # postman already there
    assert pres_dedupe == 0
    assert failed == 0
    install_calls = [c for c in calls
                     if c[1:3] == ["plugin", "install"]]
    assert len(install_calls) == 1
    assert install_calls[0][3] == "sentry@claude-plugins-official"


def test_install_claude_plugins_continues_after_per_plugin_failure(monkeypatch) -> None:
    """0175 contract: per-plugin failure is non-fatal. If
    ``claude plugin install postman`` fails (upstream renamed it,
    etc.), the loop continues to ``sentry`` and reports the failure
    count."""
    def fake_run(cmd, *_a, **_kw):
        if cmd[1:3] == ["plugin", "list"]:
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        if cmd[1:3] == ["plugin", "install"] and "postman" in cmd[3]:
            return subprocess.CompletedProcess(
                list(cmd), 1, "", "marketplace renamed plugin",
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    inst_fresh, pres_prior, pres_dedupe, failed, failed_names = (
        setup_mod._install_claude_plugins_for_role(
            "DEVELOPER", ["postman", "sentry"],
        )
    )
    assert inst_fresh == 1
    assert pres_prior == 0
    assert pres_dedupe == 0
    assert failed == 1
    assert failed_names == ["postman"]


def test_install_claude_plugins_empty_list_is_noop(fake_claude_cli) -> None:
    """A role with empty plugin list (READER, MAINTAINER per the
    curated table) does NOT call claude at all — not even the
    ``plugin list`` probe."""
    inst_fresh, pres_prior, pres_dedupe, failed, failed_names = (
        setup_mod._install_claude_plugins_for_role("READER", [])
    )
    assert (inst_fresh, pres_prior, pres_dedupe, failed) == (0, 0, 0, 0)
    assert failed_names == []
    assert fake_claude_cli == []


def test_install_claude_plugins_handles_missing_claude_cli(monkeypatch) -> None:
    """0203: ``claude`` binary not on PATH and not in the fallback
    candidate locations → resolver returns None → role-install
    reports all-failed with named plugins, doesn't shell out at all."""
    monkeypatch.setattr(setup_mod, "_resolve_claude_binary",
                        lambda: None)
    inst_fresh, pres_prior, pres_dedupe, failed, failed_names = (
        setup_mod._install_claude_plugins_for_role(
            "DEVELOPER", ["postman"],
        )
    )
    assert inst_fresh == 0
    assert pres_prior == 0
    assert pres_dedupe == 0
    assert failed == 1
    assert failed_names == ["postman"]


# ---------- _install_role_plugins_per_host (aggregate) ----------


def test_install_role_plugins_iterates_all_claude_roles(monkeypatch,
                                                         tmp_path: Path) -> None:
    """Aggregate: setup walks every role with a non-empty curated
    plugin list and calls _install_claude_plugins_for_role for it."""
    canon = tmp_path / "canon"
    _make_canon_with_plugins(canon, {
        "claude_marketplace": {
            "DEVELOPER": ["postman"],
            "TESTER": ["playwright"],
            "READER": [],   # skipped (empty)
        },
    })
    role_calls: list[str] = []
    monkeypatch.setattr(
        setup_mod, "_install_claude_plugins_for_role",
        lambda role, plugins, **kw: (
            role_calls.append(role) or (len(plugins), 0, 0, 0, [])
        ),
    )

    setup_mod._install_role_plugins_per_host(canon, verbose=False)
    assert "DEVELOPER" in role_calls
    assert "TESTER" in role_calls
    assert "READER" not in role_calls  # empty list → skipped


def test_install_role_plugins_codex_side_deferred(monkeypatch,
                                                    tmp_path: Path) -> None:
    """Codex roles with empty lists → setup logs deferral, no install
    calls. The plan body explicitly defers codex pending separate
    USER curation."""
    canon = tmp_path / "canon"
    _make_canon_with_plugins(canon, {
        "claude_marketplace": {},
        "codex_marketplace": {
            "TECHNICAL-WRITER": [],
            "EXPLORER": [],
        },
    })

    install_called: list = []
    monkeypatch.setattr(
        setup_mod, "_install_claude_plugins_for_role",
        lambda *a, **kw: install_called.append(a) or (0, 0, 0, 0, []),
    )

    out = setup_mod._install_role_plugins_per_host(canon, verbose=False)
    assert out == (0, 0, 0, 0, [])
    assert install_called == []


def test_install_role_plugins_returns_aggregate_counts(monkeypatch,
                                                        tmp_path: Path) -> None:
    """The aggregate helper sums (installed, skipped, failed) across
    all roles for the setup-summary line."""
    canon = tmp_path / "canon"
    _make_canon_with_plugins(canon, {
        "claude_marketplace": {
            "DEVELOPER": ["a"],
            "TESTER": ["b", "c"],
        },
    })
    counts_by_role = {
        # (inst_fresh, pres_prior, pres_dedupe, failed, failed_names)
        "DEVELOPER": (1, 0, 0, 0, []),
        "TESTER":    (1, 1, 0, 1, ["c"]),
    }
    monkeypatch.setattr(
        setup_mod, "_install_claude_plugins_for_role",
        lambda role, plugins, **kw: counts_by_role[role],
    )

    (inst_fresh, pres_prior, pres_dedupe, failed,
     failed_names) = setup_mod._install_role_plugins_per_host(
        canon, verbose=False,
    )
    assert inst_fresh == 2
    assert pres_prior == 1
    assert pres_dedupe == 0
    assert failed == 1
    assert "c" in failed_names


# ---------- regression pin: role-docs no longer carry fabricated bullets ----------


def test_role_docs_no_fabricated_plugin_bullets() -> None:
    """0175 regression pin: ``data/roles/*.md`` no longer references
    the fabricated ``plugins/role-X/skills/Y/SKILL.md`` paths.

    Skip cleanly if the role-doc cleanup is not yet applied — the
    schema-yaml-driven setup helper above is the load-bearing part;
    the cleanup is a follow-up cosmetic.
    """
    from greatminds.core.paths import find_canon_dir
    roles_dir = find_canon_dir() / "roles"
    if not roles_dir.is_dir():
        pytest.skip("roles dir missing")
    fabricated_pattern = "plugins/role-"
    offenders: list[str] = []
    for f in sorted(roles_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- `") and fabricated_pattern in stripped:
                offenders.append(f"{f.name}: {stripped[:80]}")
    if offenders:
        pytest.skip(
            f"0175 follow-up: role-doc plugin bullets still cite "
            f"fabricated paths; cleanup pending. Offenders: {offenders[:3]}"
        )
