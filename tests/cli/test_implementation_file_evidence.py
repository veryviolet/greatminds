from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


def _coord(project: Path) -> Path:
    coord = project / "coordination"
    coord.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=project,
        check=True,
    )
    return coord


def test_implementation_file_evidence_rejects_phantom_path(
    tmp_path: Path,
) -> None:
    coord = _coord(tmp_path)
    block = {"files": ["README.md"]}

    with pytest.raises(GreatMindsError) as exc:
        task_mod._enforce_implementation_files_exist_or_are_changed(
            "implementation", block, coord
        )

    assert "README.md" in str(exc.value)
    assert "no git-status evidence" in str(exc.value)


def test_implementation_file_evidence_rejects_unchanged_tracked_path(
    tmp_path: Path,
) -> None:
    coord = _coord(tmp_path)
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    block = {"files": ["README.md"]}

    with pytest.raises(GreatMindsError) as exc:
        task_mod._enforce_implementation_files_exist_or_are_changed(
            "implementation", block, coord
        )

    assert "README.md" in str(exc.value)
    assert "no git-status evidence" in str(exc.value)


def test_implementation_file_evidence_allows_modified_tracked_path(
    tmp_path: Path,
) -> None:
    coord = _coord(tmp_path)
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("docs\nusage\n", encoding="utf-8")
    block = {"files": ["README.md"]}

    task_mod._enforce_implementation_files_exist_or_are_changed(
        "implementation", block, coord
    )


def test_implementation_file_evidence_checks_task_worktree_dir(
    tmp_path: Path,
) -> None:
    """Implementation evidence is relative to the per-task worktree where
    the implementer edited, not the main fleet checkout that holds
    coordination/."""
    coord = _coord(tmp_path)
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    wt = tmp_path / ".worktrees" / "0001-docs"
    wt.parent.mkdir()
    subprocess.run(
        ["git", "worktree", "add", "-q", str(wt), "-b", "task/0001-docs"],
        cwd=tmp_path,
        check=True,
    )
    (wt / "README.md").write_text("docs\nusage\n", encoding="utf-8")
    block = {"files": ["README.md"]}

    with pytest.raises(GreatMindsError):
        task_mod._enforce_implementation_files_exist_or_are_changed(
            "implementation", block, coord
        )
    task_mod._enforce_implementation_files_exist_or_are_changed(
        "implementation", block, coord, evidence_dir=wt
    )


def test_implementation_file_evidence_allows_untracked_path(
    tmp_path: Path,
) -> None:
    coord = _coord(tmp_path)
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    block = {"files": ["README.md"]}

    task_mod._enforce_implementation_files_exist_or_are_changed(
        "implementation", block, coord
    )


def test_implementation_file_evidence_allows_existing_path_outside_git(
    tmp_path: Path,
) -> None:
    coord = tmp_path / "coordination"
    coord.mkdir()
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    block = {"files": ["README.md"]}

    task_mod._enforce_implementation_files_exist_or_are_changed(
        "implementation", block, coord
    )


def test_implementation_file_evidence_allows_tracked_deletion(
    tmp_path: Path,
) -> None:
    coord = _coord(tmp_path)
    p = tmp_path / "obsolete.txt"
    p.write_text("remove me\n", encoding="utf-8")
    subprocess.run(["git", "add", "obsolete.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    p.unlink()
    block = {"files": ["obsolete.txt"]}

    task_mod._enforce_implementation_files_exist_or_are_changed(
        "implementation", block, coord
    )
