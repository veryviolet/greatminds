"""Task 0382: full-deploy rsync must exclude .git.

A per-task worktree's ``.git`` is a ``gitdir:`` FILE, but a destination
from a prior deploy may hold a stale ``.git`` DIRECTORY. rsync cannot
replace a non-empty directory with a regular file and aborts with exit
23 ("cannot delete non-empty directory .git"), bringing the singleton
stand DOWN and blocking all TESTER/EXPLORER stand validation.

The shipped template excludes .git and clears a stale destination git pointer.
"""
from __future__ import annotations

import re
from pathlib import Path

from greatminds.core.paths import find_canon_dir

FIXTURES = Path(__file__).parent / "fixtures_0382"


def _rsync_excludes(template_text: str) -> list[str]:
    return re.findall(r"--exclude=(\S+)", template_text)


def test_template_rsync_excludes_git() -> None:
    template = (
        find_canon_dir() / "templates" / "stand-profiles" / "full-deploy.yaml"
    ).read_text("utf-8")
    excludes = _rsync_excludes(template)
    assert ".git" in excludes, excludes
    assert 'rm -rf -- "{{ deploy_path }}/.git"' in template
    assert (
        template.index('rm -rf -- "{{ deploy_path }}/.git"')
        < template.index("rsync -aP")
    )
    # the prior excludes must survive the change (no behavior regression)
    for keep in (".venv*", ".worktrees", "__pycache__"):
        assert keep in excludes, (keep, excludes)
