"""Regression tests for ``start-agent codex`` argv composition (task 0043).

EXPLORER avatar dogfood on codex 0.133.0 caught two problems:

  1. The stale ``codex resume --last`` fallback no longer worked: codex
     0.133.0 silently rejected ``--last``, then the next positional (the
     bootstrap prompt text) was interpreted as the missing session-id,
     producing ``ERROR: No saved session found with ID continue your tick
     as DEVELOPER — you already know the contract``.
  2. The branch logic confused claude's ``session_new`` (the agent-side
     session-id concept used by claude) with codex's rollout-UUID (a
     separate concept stored in ``~/.codex/sessions/``). The two are
     independent.

The fix:
  - Drop the ``--last`` fallback entirely.
  - Branch only on ``codex_sid`` presence:
      - sid found → ``codex resume <sid> EXTRA PROMPT``
      - sid empty → ``codex EXTRA PROMPT`` (fresh session, bootstrap)
  - The ``session_new`` parameter is now ignored inside the builder
    (kept on the signature for caller compatibility).

These tests pin the argv shape for both branches.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import start_agent as sa_mod


@pytest.fixture(autouse=True)
def _disable_yolo(monkeypatch):
    """Skip yolo-arg injection (it's orthogonal to the bug under test)."""
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "1")


def test_codex_argv_starts_fresh_when_no_codex_sid_found(tmp_path, monkeypatch):
    """No prior rollout → fresh session; --last must NOT appear."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = tmp_path / "registry"
    registry.mkdir()

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=registry,
        session_new=False,  # claude-side session flag; must NOT trigger --last
        extra=[],
        prompt="continue your tick as DEVELOPER — you already know the contract",
    )

    assert argv[0] == "codex"
    assert "--last" not in argv, (
        "regression: --last was the broken fallback in 1.2.1; "
        f"argv={argv}"
    )
    assert "resume" not in argv, (
        "no codex_sid → no resume verb; argv must start a fresh session. "
        f"argv={argv}"
    )
    # Bootstrap prompt is the last positional.
    assert argv[-1] == "continue your tick as DEVELOPER — you already know the contract"


def test_codex_argv_resumes_when_codex_sid_present(tmp_path, monkeypatch):
    """Existing codex rollout → resume with that sid; prompt as positional."""
    sid_file = tmp_path / "developer.codex-session-id"
    sid_file.write_text("aaaa-bbbb-cccc-dddd\n", encoding="utf-8")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=tmp_path,
        session_new=False,
        extra=[],
        prompt="continue your tick as DEVELOPER",
    )

    assert argv[:3] == ["codex", "resume", "aaaa-bbbb-cccc-dddd"]
    assert argv[-1] == "continue your tick as DEVELOPER"


def test_codex_argv_session_new_flag_is_irrelevant_to_resume_decision(
    tmp_path, monkeypatch
):
    """The decision is codex_sid-driven, NOT session_new-driven.

    Two callers with the same codex_sid but different session_new values
    must produce the same resume shape.
    """
    sid_file = tmp_path / "developer.codex-session-id"
    sid_file.write_text("session-xyz\n", encoding="utf-8")

    argv_resume = sa_mod.build_codex_argv(
        role="DEVELOPER", registry_dir=tmp_path,
        session_new=False, extra=[], prompt="hi",
    )
    argv_new = sa_mod.build_codex_argv(
        role="DEVELOPER", registry_dir=tmp_path,
        session_new=True, extra=[], prompt="hi",
    )
    assert argv_resume == argv_new, (
        f"session_new must not influence codex argv. "
        f"resume={argv_resume} new={argv_new}"
    )


def test_codex_argv_discovers_rollout_and_persists_sid(tmp_path, monkeypatch):
    """When no sid file exists but discover_codex_session finds a rollout,
    the discovered sid is persisted to the registry for next time."""
    monkeypatch.setattr(sa_mod, "discover_codex_session",
                        lambda role, **_kw: "discovered-sid-1234")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER", registry_dir=tmp_path,
        session_new=False, extra=[], prompt="hello",
    )
    assert argv[:3] == ["codex", "resume", "discovered-sid-1234"]

    persisted = tmp_path / "developer.codex-session-id"
    assert persisted.is_file()
    assert persisted.read_text(encoding="utf-8").strip() == "discovered-sid-1234"


def test_codex_argv_extra_args_pass_through_before_prompt(tmp_path, monkeypatch):
    """User-supplied EXTRA args (e.g. --model X) sit between codex/yolo
    args and the trailing prompt."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    argv = sa_mod.build_codex_argv(
        role="DEVELOPER", registry_dir=tmp_path,
        session_new=True, extra=["--model", "gpt-5"], prompt="prompt-text",
    )
    # extras come right before prompt
    assert argv[-3:] == ["--model", "gpt-5", "prompt-text"]


def test_codex_argv_explorer_repro_no_last_branch(tmp_path, monkeypatch):
    """Exact EXPLORER repro shape: second launch after codex self-update,
    sid file empty (cleared), session_new=False. Must NOT emit --last."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    # No sid file in registry.
    argv = sa_mod.build_codex_argv(
        role="DEVELOPER", registry_dir=tmp_path,
        session_new=False, extra=[],
        prompt="continue your tick as DEVELOPER — you already know the contract",
    )
    assert "--last" not in argv
    assert "resume" not in argv
    # New session: bare codex + prompt (no resume verb at all).
    assert argv[0] == "codex"
    assert argv[-1] == "continue your tick as DEVELOPER — you already know the contract"
