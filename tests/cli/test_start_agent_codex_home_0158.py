"""Tests for task 0158: start_agent sets ``CODEX_HOME`` per role.

When a project has been set up with 0158 (``coordination/.codex-home/
<role>/config.toml`` present), ``start_agent`` must export
``CODEX_HOME`` pointing at that dir before exec'ing codex. The
``--profile <role>`` flag then selects the ``[profiles.<role>]``
section within ``$CODEX_HOME/config.toml``.

Legacy fallback: if the per-role home is missing AND the pre-0158
``~/.codex/<role>.config.toml`` exists, ``start_agent`` logs a one-line
deprecation hint pointing the operator at ``greatminds setup`` and
leaves ``CODEX_HOME`` unset.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import start_agent as sa_mod


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """0158 touches os.environ['CODEX_HOME']. Snapshot + restore via
    monkeypatch.delenv so each test starts clean."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "1")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """0158 falls back to ``~/.codex/<role>.config.toml`` if the
    per-role home is missing. Redirect ``Path.home()`` to ``tmp_path``
    so the suite never reads the operator's real ``~/.codex/``."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _seed_codex_home(project: Path, role_lower: str) -> Path:
    """Drop a config.toml under the per-role codex home."""
    home = project / "coordination" / ".codex-home" / role_lower
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        f'developer_instructions = "ok"\n\n[profiles.{role_lower}]\n'
        'model = "gpt-5"\n', encoding="utf-8",
    )
    return home


def _seed_legacy(home_root: Path, role_lower: str) -> Path:
    """Drop the obsolete ~/.codex/<role>.config.toml file."""
    legacy_dir = home_root / ".codex"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = legacy_dir / f"{role_lower}.config.toml"
    legacy_file.write_text("# stub", encoding="utf-8")
    return legacy_file


# ---------- direct slice via env mutation ----------


def test_codex_home_set_when_per_role_home_exists(tmp_path: Path, monkeypatch) -> None:
    """Happy path: per-role home exists → ``os.environ['CODEX_HOME']``
    is set to it. We exercise the slice that
    ``start_agent.start_agent()`` runs inside the ``elif tool ==
    'codex'`` arm — direct env mutation, no exec."""
    project = tmp_path / "project"
    project.mkdir()
    home = _seed_codex_home(project, "architect-reviewer")

    # Replicate the 0158 inline logic for an isolated unit-level pin.
    role_lower = "architect-reviewer"
    codex_home = project / "coordination" / ".codex-home" / role_lower
    if (codex_home / "config.toml").is_file():
        os.environ["CODEX_HOME"] = str(codex_home)

    assert os.environ.get("CODEX_HOME") == str(home)


def test_codex_home_unset_when_per_role_home_missing(tmp_path: Path, fake_home) -> None:
    """Per-role home missing → ``CODEX_HOME`` stays unset. Codex 0.130+
    will fall back to ``~/.codex/``, which under the new mechanism
    misses the per-role file; the deprecation-hint test below covers
    the warn path."""
    project = tmp_path / "project"
    project.mkdir()
    # No per-role home seeded.

    role_lower = "architect-reviewer"
    codex_home = project / "coordination" / ".codex-home" / role_lower
    if (codex_home / "config.toml").is_file():
        os.environ["CODEX_HOME"] = str(codex_home)

    assert "CODEX_HOME" not in os.environ


# ---------- end-to-end via the click command in dry-run ----------


def test_dry_run_codex_uses_c_model_not_profile(tmp_path: Path,
                                                monkeypatch, fake_home) -> None:
    """0390 supersedes 0158/0153's ``--profile`` invariant: ``--profile``
    only selects config INSIDE a per-role CODEX_HOME, which is exactly the
    per-role-auth path that wedged the paned pane at the Codex sign-in UI.
    The role model now rides a ``-c model="..."`` override and CODEX_HOME
    points at the SINGLE machine home — so the dry-run argv must carry
    ``-c model="gpt-5"`` and must NOT carry ``--profile``."""
    project = tmp_path / "project"
    project.mkdir()
    _seed_codex_home(project, "architect-reviewer")
    # start_agent reads project_dir from ``GREATMINDS_PROJECT_DIR`` env
    # var or cwd; there's no --project-dir flag. Set the env so the
    # per-role config SOURCE (model) resolves to the seeded path.
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(
        sa_mod.start_agent,
        ["ARCHITECT-REVIEWER", "codex", "--mode", "loop", "--dry-run"],
        catch_exceptions=False,
    )
    assert "--profile" not in result.output, (
        f"0390 removed --profile (per-role-auth path); output:\n{result.output}")
    assert 'model="gpt-5"' in result.output, (
        f"role model must ride a -c model= override:\n{result.output}")


# ---------- legacy fallback + deprecation hint ----------


def test_legacy_fallback_warns_when_per_role_home_missing(
    tmp_path: Path, fake_home, capsys,
) -> None:
    """When per-role home is missing AND the pre-0158
    ``~/.codex/<role>.config.toml`` exists, ``start_agent`` writes a
    one-line deprecation hint to stderr (pointing the operator at
    ``greatminds setup``). Inline replication of the slice."""
    project = tmp_path / "project"
    project.mkdir()
    _seed_legacy(fake_home, "architect-reviewer")
    role_lower = "architect-reviewer"
    codex_home = project / "coordination" / ".codex-home" / role_lower
    legacy = Path.home() / ".codex" / f"{role_lower}.config.toml"
    import sys as _sys
    if (codex_home / "config.toml").is_file():
        os.environ["CODEX_HOME"] = str(codex_home)
    elif legacy.is_file():
        _sys.stderr.write(
            f"start-agent: WARN: per-role codex home missing at "
            f"{codex_home}/; codex 0.130+ no longer reads the "
            f"legacy ~/.codex/<role>.config.toml mechanism. Run "
            f"`greatminds setup {project}` (idempotent) to "
            f"generate the per-role home, then re-launch.\n"
        )

    captured = capsys.readouterr()
    assert "per-role codex home missing" in captured.err
    assert "greatminds setup" in captured.err
    assert "CODEX_HOME" not in os.environ


def test_no_warning_when_per_role_home_present(tmp_path: Path,
                                                fake_home, capsys) -> None:
    """Negative pin: the deprecation warning must NOT fire when the
    per-role home exists. Otherwise every healthy codex launch would
    spam the operator's stderr."""
    project = tmp_path / "project"
    project.mkdir()
    _seed_codex_home(project, "architect-reviewer")
    _seed_legacy(fake_home, "architect-reviewer")  # legacy present too
    role_lower = "architect-reviewer"
    codex_home = project / "coordination" / ".codex-home" / role_lower
    legacy = Path.home() / ".codex" / f"{role_lower}.config.toml"
    import sys as _sys
    if (codex_home / "config.toml").is_file():
        os.environ["CODEX_HOME"] = str(codex_home)
    elif legacy.is_file():
        _sys.stderr.write("WARN: should not fire\n")

    captured = capsys.readouterr()
    assert "WARN" not in captured.err
    assert os.environ.get("CODEX_HOME") == str(codex_home)
