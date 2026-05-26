"""Tests for task 0258: ``greatminds migrate-stand-history`` one-shot.

After 0247 (1.3.0 BREAKING) every existing fleet still carries an
``coordination/stand_done/`` directory full of historical evidence
files. The migration command moves them under
``coordination/archive/stand-history/`` so the legacy dir can be
deleted with no data loss.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(cwd)
    env["GREATMINDS_ROLE"] = "MAINTAINER"
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *args],
        capture_output=True, text=True, env=env, cwd=str(cwd),
    )


def _make_coord_with_history(tmp_path: Path,
                              names: tuple[str, ...]) -> Path:
    """Toy coordination dir with ``stand_done/`` pre-populated."""
    coord = tmp_path / "proj" / "coordination"
    (coord / "stand_done").mkdir(parents=True)
    for name in names:
        (coord / "stand_done" / name).write_text(
            f"# legacy evidence {name}\n",
            encoding="utf-8",
        )
    return coord


def test_migrate_moves_yaml_and_md_into_archive(tmp_path: Path) -> None:
    """Happy path: every regular file under stand_done/ lands in
    archive/stand-history/, sources removed, dirs preserved."""
    coord = _make_coord_with_history(
        tmp_path,
        ("0099-deploy-evidence.yaml", "0100-smoke.md", "0101-deploy.yaml"),
    )
    cp = _run("migrate-stand-history", "--coord", str(coord),
              cwd=tmp_path / "proj")
    assert cp.returncode == 0, cp.stderr + cp.stdout

    archive = coord / "archive" / "stand-history"
    assert archive.is_dir()
    moved = sorted(p.name for p in archive.iterdir() if p.is_file())
    assert moved == ["0099-deploy-evidence.yaml",
                     "0100-smoke.md", "0101-deploy.yaml"]
    # Sources removed.
    assert not list((coord / "stand_done").iterdir())
    assert "moved 3 file" in cp.stdout


def test_migrate_dry_run_is_noop(tmp_path: Path) -> None:
    """``--dry-run`` reports the planned moves but touches nothing."""
    coord = _make_coord_with_history(
        tmp_path,
        ("0099.yaml", "0100.md"),
    )
    cp = _run("migrate-stand-history", "--coord", str(coord), "--dry-run",
              cwd=tmp_path / "proj")
    assert cp.returncode == 0, cp.stderr + cp.stdout
    assert "DRY-RUN" in cp.stdout
    # Files still in their original location.
    assert sorted(p.name for p in (coord / "stand_done").iterdir()) \
        == ["0099.yaml", "0100.md"]
    # Archive dir was NOT created by the dry-run.
    assert not (coord / "archive" / "stand-history").exists()


def test_migrate_idempotent_on_missing_source(tmp_path: Path) -> None:
    """No legacy stand_done/ → command exits 0 with an informative line.
    A re-run after a successful migration must remain a no-op."""
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)

    cp = _run("migrate-stand-history", "--coord", str(coord),
              cwd=tmp_path / "proj")
    assert cp.returncode == 0, cp.stderr + cp.stdout
    assert "nothing to migrate" in cp.stdout


def test_migrate_skips_existing_targets(tmp_path: Path) -> None:
    """If a name already exists under archive/stand-history/, the
    legacy source is kept for manual review (no silent overwrite)."""
    coord = _make_coord_with_history(
        tmp_path,
        ("0099.yaml", "0100.yaml"),
    )
    # Seed an existing archive entry whose name collides with one source.
    pre = coord / "archive" / "stand-history"
    pre.mkdir(parents=True)
    (pre / "0099.yaml").write_text("# pre-existing\n", encoding="utf-8")

    cp = _run("migrate-stand-history", "--coord", str(coord),
              cwd=tmp_path / "proj")
    assert cp.returncode == 0, cp.stderr + cp.stdout
    # The non-colliding file moved; the colliding one stayed put.
    assert (pre / "0100.yaml").read_text(encoding="utf-8") \
        .startswith("# legacy evidence")
    assert (coord / "stand_done" / "0099.yaml").is_file(), (
        "0258: a collision must NOT overwrite an existing archive entry"
    )
    assert (pre / "0099.yaml").read_text(encoding="utf-8") \
        == "# pre-existing\n"
    assert "skipped" in cp.stdout


def test_migrate_ignores_dotfiles(tmp_path: Path) -> None:
    """Hidden bookkeeping (e.g. ``.gitkeep``) under stand_done/ must
    not be migrated — only real evidence files move."""
    coord = _make_coord_with_history(tmp_path, ("0099.yaml",))
    (coord / "stand_done" / ".gitkeep").write_text("", encoding="utf-8")

    cp = _run("migrate-stand-history", "--coord", str(coord),
              cwd=tmp_path / "proj")
    assert cp.returncode == 0, cp.stderr + cp.stdout
    assert (coord / "stand_done" / ".gitkeep").is_file(), (
        "0258: dotfiles stay in source — they're not history"
    )
    moved = sorted(p.name for p in
                   (coord / "archive" / "stand-history").iterdir()
                   if p.is_file())
    assert moved == ["0099.yaml"]
