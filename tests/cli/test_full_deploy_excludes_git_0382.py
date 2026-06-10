"""Task 0382: full-deploy rsync must exclude .git.

A per-task worktree's ``.git`` is a ``gitdir:`` FILE, but a destination
from a prior deploy may hold a stale ``.git`` DIRECTORY. rsync cannot
replace a non-empty directory with a regular file and aborts with exit
23 ("cannot delete non-empty directory .git"), bringing the singleton
stand DOWN and blocking all TESTER/EXPLORER stand validation.

Durable fix: the shipped full-deploy template excludes ``.git`` from the
worktree→stand rsync; and the prior pristine template (which rsynced
``.git``) is registered as a stale shipped hash so ``greatminds update``
reseeds existing fleets' pristine seeded profiles to the fixed template.
"""
from __future__ import annotations

import re
from pathlib import Path

from greatminds.cli import setup as setup_mod
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
    # the prior excludes must survive the change (no behavior regression)
    for keep in (".venv*", ".worktrees", "__pycache__"):
        assert keep in excludes, (keep, excludes)


def test_prior_template_hash_registered_stale() -> None:
    """The pre-fix shipped template (rsync without --exclude=.git) is in
    the stale-hash allowlist, so a pristine seeded copy on an existing
    fleet is reseeded to the .git-excluding template by ``greatminds
    update`` rather than being mistaken for an operator-customized file."""
    prior = (FIXTURES / "full-deploy.no-git-exclude.yaml").read_text("utf-8")
    assert (
        setup_mod._sha256(prior)
        in setup_mod._STALE_SHIPPED_PROFILE_HASHES["full-deploy.yaml"]
    )
    # The fixture is the genuine regression: it rsyncs .git and otherwise
    # sniffs as "current" (add_host topology), so without the registered
    # hash the reseed would leave it untouched.
    assert "--exclude=.git" not in prior
    assert setup_mod._profile_uses_add_host(prior)


def test_reseed_refreshes_no_git_exclude_profile(tmp_path: Path) -> None:
    """A pristine seeded full-deploy that still rsyncs .git is refreshed
    in place to the current template (which excludes .git); old bytes are
    backed up under stand-profiles/.backups/."""
    prior = (FIXTURES / "full-deploy.no-git-exclude.yaml").read_text("utf-8")
    coord = tmp_path / "coordination"
    (coord / "stand-profiles").mkdir(parents=True)
    (coord / "stand-profiles" / "full-deploy.yaml").write_text(prior, "utf-8")
    canon = find_canon_dir()

    result = setup_mod.reseed_stale_stand_profiles(coord, canon)

    assert result["reseeded"] == ["full-deploy.yaml"]
    refreshed = (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8")
    assert "--exclude=.git" in refreshed
    backup = coord / "stand-profiles" / ".backups" / "full-deploy.yaml"
    assert backup.is_file() and backup.read_text("utf-8") == prior
