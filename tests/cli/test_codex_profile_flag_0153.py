"""Regression tests for task 0153, SUPERSEDED by task 0390.

0153 pinned that ``build_codex_argv`` emits ``--profile <role>`` (codex-cli
0.130 dropped the older ``--profile-v2``). 0390 removes ``--profile`` from
the paned/interactive Codex launch entirely: ``--profile`` only selects a
``[profiles.<role>]`` section INSIDE a per-role ``CODEX_HOME``, which is
exactly the per-role-home-as-auth path that wedged the pane at the Codex
sign-in UI (the host login lives in the SINGLE machine ``CODEX_HOME``).
Mirroring the 0375 driven model, the role model now rides a
``-c model="..."`` override read from the per-role config SOURCE, and
``CODEX_HOME`` points at the machine home.

These tests therefore pin the POST-0390 argv shape: ``--profile`` (and the
long-dead ``--profile-v2``) must NEVER appear; the role model rides
``-c model=`` when the per-role coordination config declares one.

REVIEWER iter-2 ask (still honoured): monkeypatch ``Path.home`` so any
legacy ``~/.codex`` lookups land in ``tmp_path``, never the operator's
real home dir.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import start_agent as sa_mod


@pytest.fixture(autouse=True)
def _disable_yolo(monkeypatch):
    """Skip yolo-arg injection (orthogonal to the flag under test)."""
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "1")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` to a per-test tmp dir so no test touches
    the operator's real ``~/.codex/``."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _registry_under_project(tmp_path: Path) -> Path:
    """A ``.agent_registry`` dir whose ``.parent.parent`` is the project
    root — ``build_codex_argv`` derives ``project_dir`` from
    ``registry_dir.parent.parent`` to find the per-role config SOURCE."""
    registry = tmp_path / "project" / "coordination" / ".agent_registry"
    registry.mkdir(parents=True)
    return registry


def _seed_role_model(registry: Path, role_lower: str, model: str) -> Path:
    """Per-role config SOURCE under ``coordination/.codex-home/<role>`` —
    declares ``model`` (no auth.json). ``build_codex_argv`` reads this to
    compose the ``-c model="..."`` override (post-0390)."""
    project = registry.parent.parent
    home = project / "coordination" / ".codex-home" / role_lower
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'developer_instructions = "ok"\nmodel = "{model}"\n'
        f'\n[profiles.{role_lower}]\nmodel = "{model}"\n',
        encoding="utf-8",
    )
    return home


def test_codex_argv_emits_c_model_not_profile(tmp_path, monkeypatch, fake_home):
    """Post-0390 contract: the paned argv carries ``-c model="..."`` and
    NEVER ``--profile`` / ``--profile-v2`` (both select config inside a
    per-role CODEX_HOME — the per-role-auth path 0390 removes)."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = _registry_under_project(tmp_path)
    _seed_role_model(registry, "developer", "gpt-5-codex")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap prompt",
    )

    assert "--profile" not in argv, f"0390 removed --profile; argv={argv}"
    assert "--profile-v2" not in argv, f"argv={argv}"
    assert "-c" in argv, f"argv must carry the -c model override; argv={argv}"
    assert 'model="gpt-5-codex"' in argv, f"argv={argv}"


def test_codex_argv_c_model_value_is_role_model(tmp_path, monkeypatch, fake_home):
    """The ``-c`` override value is the role's configured model, read from
    the per-role config SOURCE (lowercased role dir)."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = _registry_under_project(tmp_path)
    _seed_role_model(registry, "architect-reviewer", "gpt-5")

    argv = sa_mod.build_codex_argv(
        role="ARCHITECT-REVIEWER",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap",
    )

    i = argv.index("-c")
    assert argv[i + 1] == 'model="gpt-5"'
    assert "--profile" not in argv


def test_codex_argv_no_model_override_when_no_config(tmp_path, monkeypatch, fake_home):
    """Negative pin: with no per-role config SOURCE declaring a model,
    neither ``-c model=`` nor ``--profile``/``--profile-v2`` is emitted —
    codex falls back to its own default model."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = _registry_under_project(tmp_path)
    # No per-role config seeded → no model declared.

    argv = sa_mod.build_codex_argv(
        role="NONEXISTENT-ROLE-0153-TEST",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap",
    )

    assert "--profile" not in argv
    assert "--profile-v2" not in argv
    assert 'model="' not in " ".join(argv)


def test_codex_argv_c_model_appears_in_resume_branch_too(tmp_path, monkeypatch, fake_home):
    """When a codex rollout exists (resume branch), the model still rides
    ``-c model=`` and ``--profile`` is still absent — the resume vs fresh
    branch doesn't change the auth/model mechanism."""
    registry = _registry_under_project(tmp_path)
    # Seed an existing rollout id (resume branch).
    (registry / "developer.codex-session-id").write_text(
        "deadbeef-1111-2222-3333-444444444444\n", encoding="utf-8",
    )
    _seed_role_model(registry, "developer", "gpt-5-codex")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=registry,
        session_new=False,
        extra=[],
        prompt="continue",
    )

    assert "resume" in argv  # we're on the resume branch
    assert "--profile" not in argv
    assert "--profile-v2" not in argv
    assert 'model="gpt-5-codex"' in argv


# ---------- source-tree scan: no stale references ----------


def test_no_profile_v2_references_in_src(tmp_path) -> None:
    """0153 cosmetic cleanup pin: docstrings, comments, and canon
    files under src/ must not mention the dropped flag. Catches the
    case where someone replaces the active arg but leaves stale
    'Compose codex [profile-v2]' docstring or '# Usage: codex exec
    --profile-v2 <role>' headers in the per-role config.toml files.

    This test does NOT need the fake_home fixture — it scans the
    on-disk src/ tree, no file writes anywhere.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "greatminds"
    offenders: list[str] = []
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix not in (".py", ".md", ".toml", ".yaml"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "profile-v2" in text or "profile_v2" in text:
            offenders.append(str(f.relative_to(src.parent)))
    assert offenders == [], (
        "0153: no stale --profile-v2 references should remain under src/. "
        f"Offenders: {offenders}"
    )
