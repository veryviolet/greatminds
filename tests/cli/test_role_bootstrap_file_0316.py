"""Tests for task 0316 (0311 Phase 2b): role contract delivered via
``--append-system-prompt-file``.

In the driven model each turn is a fresh ``claude -p`` call, so the
role contract must live in the system prompt (available every turn,
independent of --resume history) rather than as the first
user-message. 0316 generates ``coordination/.bootstrap/<role>.md``
from render-role at setup/launch; the 0315 driver passes it to
``--append-system-prompt-file``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import start_agent as sa_mod
from greatminds.cli import setup as setup_mod


# ---------- bootstrap_path helper ----------


def test_bootstrap_path_under_dot_bootstrap(tmp_path: Path) -> None:
    """0316: the path is ``<coord>/.bootstrap/<role-lower>.md``."""
    coord = tmp_path / "coordination"
    p = sa_mod.bootstrap_path(coord, "DEVELOPER")
    assert p == coord / ".bootstrap" / "developer.md"


def test_bootstrap_path_lowercases_role(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    p = sa_mod.bootstrap_path(coord, "ARCHITECT-PLANNER")
    assert p.name == "architect-planner.md"


# ---------- write_role_bootstrap ----------


def test_write_role_bootstrap_writes_render_output(
    tmp_path: Path, monkeypatch,
) -> None:
    """0316: the bootstrap file content == render-role output. We
    stub render_prompt so the test is hermetic (no canon needed)."""
    coord = tmp_path / "coordination"
    project = tmp_path / "proj"
    monkeypatch.setattr(
        sa_mod, "render_prompt",
        lambda role, pd: f"BOOTSTRAP FOR {role}\ncanon refs here",
    )
    out = sa_mod.write_role_bootstrap(coord, project, "DEVELOPER")
    assert out == coord / ".bootstrap" / "developer.md"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "BOOTSTRAP FOR DEVELOPER" in text
    assert "canon refs here" in text


def test_write_role_bootstrap_creates_parent_dir(
    tmp_path: Path, monkeypatch,
) -> None:
    """The ``.bootstrap/`` dir is created if absent."""
    coord = tmp_path / "coordination"
    assert not (coord / ".bootstrap").exists()
    monkeypatch.setattr(sa_mod, "render_prompt",
                        lambda role, pd: "x")
    sa_mod.write_role_bootstrap(coord, tmp_path, "TESTER")
    assert (coord / ".bootstrap").is_dir()


def test_write_role_bootstrap_propagates_render_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """A render-role failure raises — driven roles need the file;
    the SEED caller (setup) wraps this in try/except for graceful
    degradation, but the primitive itself surfaces the error."""
    from greatminds.core.errors import GreatMindsError
    coord = tmp_path / "coordination"

    def boom(role, pd):
        raise GreatMindsError("render-role failed")
    monkeypatch.setattr(sa_mod, "render_prompt", boom)
    with pytest.raises(GreatMindsError):
        sa_mod.write_role_bootstrap(coord, tmp_path, "DEVELOPER")


# ---------- setup seeds bootstraps ----------


def test_seed_role_bootstraps_writes_all_roles(
    tmp_path: Path, monkeypatch,
) -> None:
    """0316: ``_seed_role_bootstraps`` writes one file per role in
    ROLES_LOWER; returns the count."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    monkeypatch.setattr(
        setup_mod, "warn", lambda *a, **kw: None,
    )
    # Stub the underlying primitive so we don't run render-role.
    from greatminds.cli import start_agent
    monkeypatch.setattr(
        start_agent, "render_prompt",
        lambda role, pd: f"contract:{role}",
    )
    written = setup_mod._seed_role_bootstraps(coord, tmp_path)
    assert written == len(setup_mod.ROLES_LOWER)
    # Spot-check a couple of role files landed.
    assert (coord / ".bootstrap" / "developer.md").is_file()
    assert (coord / ".bootstrap" / "tester.md").is_file()


def test_seed_role_bootstraps_continues_on_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """A render failure for ONE role must not abort the whole
    seed — other roles still get their bootstrap, and the count
    reflects only the successes."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    monkeypatch.setattr(setup_mod, "warn", lambda *a, **kw: None)
    from greatminds.cli import start_agent
    from greatminds.core.errors import GreatMindsError

    def selective(role, pd):
        if role == "DEVELOPER":
            raise GreatMindsError("boom")
        return f"contract:{role}"
    monkeypatch.setattr(start_agent, "render_prompt", selective)

    written = setup_mod._seed_role_bootstraps(coord, tmp_path)
    # All but DEVELOPER succeed.
    assert written == len(setup_mod.ROLES_LOWER) - 1
    assert not (coord / ".bootstrap" / "developer.md").is_file()
    assert (coord / ".bootstrap" / "tester.md").is_file()


# ---------- contract-via-system-prompt shape ----------


def test_bootstrap_content_is_render_role_verbatim(
    tmp_path: Path, monkeypatch,
) -> None:
    """0316 contract: the bootstrap file IS the render-role output
    (role contract + canon refs) so the system prompt carries the
    full contract on every -p turn."""
    coord = tmp_path / "coordination"
    rendered = (
        "# DEVELOPER\nResponsibilities: …\n"
        "Read COORDINATE.md, schema.yaml, PROJECT.md.\n"
    )
    monkeypatch.setattr(sa_mod, "render_prompt",
                        lambda role, pd: rendered.rstrip())
    out = sa_mod.write_role_bootstrap(coord, tmp_path, "DEVELOPER")
    # File == render output (+ trailing newline).
    assert out.read_text(encoding="utf-8") == rendered.rstrip() + "\n"
