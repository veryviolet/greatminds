"""Regression tests for task 0153: codex-cli 0.130.0 removed
``--profile-v2``; build_codex_argv must emit ``--profile`` (which has
been the canonical flag since codex-cli 0.125 and is the only one
left in 0.130+).

The fix was a mechanical rename in commit 7c30289. These tests pin
the argv shape so a future refactor can't accidentally re-introduce
the dropped flag.

REVIEWER iter-2 ask: monkeypatch ``Path.home`` so the per-test stub
``~/.codex/<role>.config.toml`` writes land in ``tmp_path``, NOT the
real user's home dir. Iter-1 wrote/unlinked under the actual
``$HOME/.codex/`` which could clobber the operator's codex profile
configs when the suite runs on a live host.
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
    """0153 iter-2: redirect ``Path.home()`` to a per-test tmp dir.

    ``build_codex_argv`` resolves the profile config via
    ``Path.home() / '.codex' / f'{role_lower}.config.toml'``. With
    this fixture every test reads/writes a fully sandboxed
    ``~/.codex/`` rooted at ``tmp_path``; the real ``$HOME/.codex/``
    is never touched.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _seed_profile_toml(home: Path, role_lower: str) -> Path:
    """Drop a stub config under the fake home dir."""
    p = home / ".codex" / f"{role_lower}.config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# stub config for 0153 test\n", encoding="utf-8")
    return p


def test_codex_argv_emits_profile_not_profile_v2(tmp_path, monkeypatch, fake_home):
    """0153 contract: ``--profile <role>`` is emitted, not the dropped
    ``--profile-v2``. Pre-fix the latter would produce ``error:
    unexpected argument '--profile-v2' found`` from codex-cli 0.130+
    and stall every fresh start-agent."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = tmp_path / "registry"
    registry.mkdir()
    _seed_profile_toml(fake_home, "developer")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap prompt",
    )

    assert "--profile" in argv, f"argv must contain --profile; argv={argv}"
    assert "--profile-v2" not in argv, (
        "0153 regression: --profile-v2 was removed by codex-cli 0.130 "
        f"and stalls every fresh launch on current upstream. argv={argv}"
    )


def test_codex_argv_profile_followed_by_role_lower(tmp_path, monkeypatch, fake_home):
    """The flag value is the lowercased role name (``architect-reviewer``
    not ``ARCHITECT-REVIEWER``). codex looks up
    ``~/.codex/<role-lower>.config.toml`` by that name; uppercase
    would fail the file lookup."""
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = tmp_path / "registry"
    registry.mkdir()
    _seed_profile_toml(fake_home, "architect-reviewer")

    argv = sa_mod.build_codex_argv(
        role="ARCHITECT-REVIEWER",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap",
    )

    i = argv.index("--profile")
    assert argv[i + 1] == "architect-reviewer"


def test_codex_argv_no_profile_when_no_config_toml(tmp_path, monkeypatch, fake_home):
    """Negative pin: if ~/.codex/<role>.config.toml is absent, neither
    --profile nor --profile-v2 is emitted (codex would error on either
    pointing at a missing file). This is the existing 0043 contract;
    pin it again so a 0153-style 'rename --profile-v2 to --profile'
    refactor can't accidentally start emitting --profile unconditionally.
    """
    monkeypatch.setattr(sa_mod, "discover_codex_session", lambda role, **_kw: "")
    registry = tmp_path / "registry"
    registry.mkdir()
    # No stub seeded in fake_home → file genuinely absent.

    argv = sa_mod.build_codex_argv(
        role="NONEXISTENT-ROLE-0153-TEST",
        registry_dir=registry,
        session_new=True,
        extra=[],
        prompt="bootstrap",
    )

    assert "--profile" not in argv
    assert "--profile-v2" not in argv


def test_codex_argv_profile_appears_in_resume_branch_too(tmp_path, monkeypatch, fake_home):
    """When a codex rollout exists (resume branch), the profile flag
    is still --profile, not --profile-v2. The resume vs fresh branch
    doesn't change the flag name."""
    registry = tmp_path / "registry"
    registry.mkdir()
    # Seed an existing rollout id (resume branch).
    (registry / "developer.codex-session-id").write_text(
        "deadbeef-1111-2222-3333-444444444444\n", encoding="utf-8",
    )
    _seed_profile_toml(fake_home, "developer")

    argv = sa_mod.build_codex_argv(
        role="DEVELOPER",
        registry_dir=registry,
        session_new=False,
        extra=[],
        prompt="continue",
    )

    assert "resume" in argv  # we're on the resume branch
    assert "--profile" in argv
    assert "--profile-v2" not in argv


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
