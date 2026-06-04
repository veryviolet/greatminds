"""Tests for task 0164: discover_codex_session walks the per-role
``<CODEX_HOME>/sessions/`` after 0158 instead of the legacy
``~/.codex/sessions/``.

Pre-0164 (and post-0158) the discovery looked at the WRONG directory:
``~/.codex/sessions/`` carried pre-0158 rollouts, but codex 0.130+
launches with ``CODEX_HOME=<project>/coordination/.codex-home/<role>/``
and writes new rollouts under that per-role tree. Discovery found
stale legacy SIDs, cached them, and the next launch issued
``codex resume <sid>`` against the new home where the SID didn't
exist. The wrapper-loop respawned forever.

0164 fix: walk ``<project>/coordination/.codex-home/<role>/sessions/``
first; fall back to ``~/.codex/sessions/`` only when the per-role
home doesn't exist (legacy projects not yet re-run through 0158
setup). Stop at the FIRST root that yields a hit — pre-0158
rollouts are stale by definition once the post-0158 home is
populated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import start_agent as sa_mod


def _seed_rollout(root: Path, role: str, sid: str, mtime: float | None = None) -> Path:
    """Write a minimal rollout-*.jsonl whose head contains
    ``"You are <ROLE> agent"`` so the discovery's content filter
    accepts it.

    The existing discovery regex (rollout-[0-9T-]+-([0-9a-f-]+).jsonl)
    is greedy: it consumes as much of the date-prefix as possible
    and captures only the trailing hex segment. Tests pass ``sid``
    as the segment that should be returned (typically the last
    hex group after the final ``-``).
    """
    root.mkdir(parents=True, exist_ok=True)
    # Discovery's name regex requires ``rollout-<iso>-<uuid>.jsonl``.
    fname = f"rollout-2026-05-25T00-00-00-{sid}.jsonl"
    fp = root / fname
    fp.write_text(
        f'{{"role":"{role}","intro":"You are {role} agent"}}\n',
        encoding="utf-8",
    )
    if mtime is not None:
        import os
        os.utime(fp, (mtime, mtime))
    return fp


# ---------- per-role home wins over legacy ----------


def test_discovery_prefers_per_role_home_over_legacy(tmp_path: Path,
                                                      monkeypatch) -> None:
    """0164 contract: when ``<project>/coordination/.codex-home/<role>/
    sessions/`` exists, discovery walks IT, not the legacy
    ``~/.codex/sessions/``. A SID found in the per-role tree wins
    even if the legacy tree carries a newer rollout for the same
    role."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    per_role = (project / "coordination" / ".codex-home" / "developer"
                / "sessions")
    legacy = home / ".codex" / "sessions"
    # Each SID is the trailing segment the discovery regex captures.
    fresh_sid = "f5e5f5e5f5e5"
    stale_sid = "deaddeaddead"
    # The PER-ROLE rollout (older mtime) must still win over the
    # LEGACY one (newer mtime). The legacy tree should not be consulted
    # once the per-role tree yields a hit.
    _seed_rollout(per_role, "DEVELOPER", fresh_sid, mtime=1000)
    _seed_rollout(legacy, "DEVELOPER", stale_sid, mtime=9999)

    found = sa_mod.discover_codex_session("DEVELOPER", project_dir=project)
    assert found == fresh_sid, (
        f"0164: per-role home must win over legacy; got {found!r}, "
        f"expected {fresh_sid!r}"
    )


def test_discovery_falls_back_to_legacy_when_no_per_role_home(tmp_path: Path,
                                                                monkeypatch) -> None:
    """Legacy fallback: a project that hasn't been re-run through 0158
    setup yet has no per-role home. Discovery falls back to
    ``~/.codex/sessions/`` so legacy installs still resume — they were
    not affected by the 0158 CODEX_HOME change."""
    project = tmp_path / "project"
    project.mkdir()
    # No per-role home created.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    legacy = home / ".codex" / "sessions"
    sid = "deadbeefdead"
    _seed_rollout(legacy, "DEVELOPER", sid)

    found = sa_mod.discover_codex_session("DEVELOPER", project_dir=project)
    assert found == sid


def test_discovery_returns_empty_when_no_rollouts_anywhere(tmp_path: Path,
                                                            monkeypatch) -> None:
    """Fresh install (no rollouts in either home) → empty string.
    Caller (build_codex_argv) then takes the fresh-launch branch."""
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    found = sa_mod.discover_codex_session("DEVELOPER", project_dir=project)
    assert found == ""


def test_discovery_returns_empty_when_per_role_home_empty(tmp_path: Path,
                                                          monkeypatch) -> None:
    """Per-role home EXISTS but has no rollouts → discovery must NOT
    leak into the legacy tree (which is where the 0164 bug came from).
    The post-0158 home shape signals 'this is a 0158-era install; use
    only its rollouts'."""
    project = tmp_path / "project"
    per_role = (project / "coordination" / ".codex-home" / "developer"
                / "sessions")
    per_role.mkdir(parents=True)  # exists but empty
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    legacy = home / ".codex" / "sessions"
    _seed_rollout(legacy, "DEVELOPER", "0099aaaabbbb")

    found = sa_mod.discover_codex_session("DEVELOPER", project_dir=project)
    assert found == "", (
        "0164: empty per-role home must NOT fall through to legacy "
        "(legacy SIDs are stale once the post-0158 home is the source "
        f"of truth). Got: {found!r}"
    )


def test_discovery_no_legacy_fallback_when_codex_home_root_exists_without_sessions(
    tmp_path: Path, monkeypatch,
) -> None:
    """0164 iter-2 (REVIEWER + TESTER ask): the legacy gate is the
    codex_home ROOT, not the sessions subdir. A fresh ``greatminds
    setup`` creates ``coordination/.codex-home/<role>/`` (containing
    config.toml) before codex has written any rollouts; ``sessions/``
    doesn't exist yet. Iter-1 fell through to legacy in this case;
    iter-2 must NOT. The codex_home root is the 0158-era marker."""
    project = tmp_path / "project"
    codex_home = (project / "coordination" / ".codex-home" / "developer")
    codex_home.mkdir(parents=True)
    # NOTE: no sessions/ subdir — codex hasn't run yet.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    legacy = home / ".codex" / "sessions"
    _seed_rollout(legacy, "DEVELOPER", "0099aaaabbbb")

    found = sa_mod.discover_codex_session("DEVELOPER", project_dir=project)
    assert found == "", (
        "0164 iter-2: codex_home root presence (without sessions/) "
        "is the 0158-era marker — legacy fallback must be GATED on the "
        f"root, not the subdir. Got: {found!r}"
    )


# ---------- backward compat (no project_dir arg) ----------


def test_discovery_signature_accepts_legacy_call(tmp_path: Path,
                                                  monkeypatch) -> None:
    """Pre-0164 callers passed only ``role``. The new project_dir
    parameter must default to None and the call must still succeed,
    walking the legacy tree only."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    legacy = home / ".codex" / "sessions"
    sid = "ff00ff00ff00"
    _seed_rollout(legacy, "DEVELOPER", sid)

    # No project_dir kwarg — must still work.
    found = sa_mod.discover_codex_session("DEVELOPER")
    assert found == sid


# ---------- role-name filter still applies ----------


def test_discovery_finds_session_with_generic_1_5_0_bootstrap_head(
        tmp_path: Path, monkeypatch) -> None:
    """REGRESSION: the 1.5.0+ codex bootstrap head is generic ('You are a
    greatminds coordination agent'), NOT 'You are <ROLE> agent'. The old
    per-role needle therefore rejected the role's OWN rollouts → discovery
    returned "" → codex relaunched FRESH = silent session reset on every
    restart / update. Per-role codex homes are already role-isolated, so
    discovery now takes the newest rollout regardless of head — the
    session resumes instead of resetting."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    tester_home = (project / "coordination" / ".codex-home" / "tester"
                   / "sessions")
    # Head is the generic 1.5.0 bootstrap, NOT "You are TESTER agent".
    _seed_rollout(tester_home, "a greatminds coordination", "aa11aa11aa11")

    found = sa_mod.discover_codex_session("TESTER", project_dir=project)
    assert found == "aa11aa11aa11"  # resumed, NOT reset to fresh
