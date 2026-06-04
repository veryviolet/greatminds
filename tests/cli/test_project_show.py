"""Tests for `greatminds project show` — read-only PROJECT.md surface.

Closes the protocol gap where agents must read coordination/PROJECT.md
but had no sanctioned CLI to obtain it (the mutations-via-CLI rule + cwd
ambiguity). Read-only; resolves the coordination dir regardless of cwd.
"""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from greatminds.cli import main as main_mod


def _project(tmp_path: Path, body: str | None) -> Path:
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    if body is not None:
        (coord / "PROJECT.md").write_text(body, encoding="utf-8")
    return tmp_path


def test_project_show_prints_project_md(tmp_path, monkeypatch):
    proj = _project(tmp_path, "# Lattice\nhosts: lattice-a, lattice-b\n")
    monkeypatch.chdir(proj)
    res = CliRunner().invoke(main_mod.cli, ["project", "show"])
    assert res.exit_code == 0, res.output
    assert "# Lattice" in res.output
    assert "lattice-a" in res.output


def test_project_show_resolves_from_subdir(tmp_path, monkeypatch):
    proj = _project(tmp_path, "PROJECT tokens here\n")
    sub = proj / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)  # cwd below the project — find_coord_dir walks up
    res = CliRunner().invoke(main_mod.cli, ["project", "show"])
    assert res.exit_code == 0, res.output
    assert "PROJECT tokens here" in res.output


def test_project_show_errors_when_missing(tmp_path, monkeypatch):
    proj = _project(tmp_path, None)  # coordination/ exists, no PROJECT.md
    monkeypatch.chdir(proj)
    res = CliRunner().invoke(main_mod.cli, ["project", "show"])
    assert res.exit_code != 0
    assert "PROJECT.md not found" in res.output
