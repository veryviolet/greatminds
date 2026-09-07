"""Known old template bytes remain explicit inputs, never hidden upgrade rules."""
from pathlib import Path

import pytest
import yaml

from greatminds.cli.stand_profile import load_profile
from greatminds.runtime.bootstrap import bootstrap


@pytest.mark.parametrize('in_worktree', [False, True])
def test_old_profile_is_preserved_and_not_replaced_by_packaged_example(tmp_path, in_worktree):
    project = tmp_path / 'project'
    project.mkdir()
    bootstrap(project)
    worktree = tmp_path / 'worktree'
    selected_root = worktree if in_worktree else project
    profiles = selected_root / 'coordination/stand-profiles'
    profiles.mkdir(parents=True, exist_ok=True)
    fixture = Path(__file__).parent / 'fixtures_0382/full-deploy.no-git-exclude.yaml'
    original = fixture.read_bytes()
    selected = profiles / 'full-deploy.yaml'
    selected.write_bytes(original)
    packaged = worktree / 'src/greatminds/data/templates/stand-profiles/full-deploy.yaml'
    packaged.parent.mkdir(parents=True)
    packaged.write_text(yaml.safe_dump({'name': 'example', 'hosts': 'different-host', 'tasks': []}))
    bootstrap(project)
    spec = load_profile(project / '.greatminds', 'full-deploy', worktree=worktree)
    assert spec.path == selected
    assert spec.source == ('lease-worktree' if in_worktree else 'main')
    assert selected.read_bytes() == original
