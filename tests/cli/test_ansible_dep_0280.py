"""Local ACP installs do not require optional stand execution dependencies."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement


def project():
    return tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text())['project']


def test_base_install_excludes_ansible_and_removed_inotify_driver():
    names = {Requirement(value).name for value in project()['dependencies']}
    assert 'ansible-core' not in names
    assert 'inotify_simple' not in names
    assert 'agent-client-protocol' in names


def test_stands_extra_retains_validated_ansible_range():
    requirements = [Requirement(value) for value in project()['optional-dependencies']['stands']]
    ansible = next(item for item in requirements if item.name == 'ansible-core')
    assert '2.17.14' in ansible.specifier
    assert '2.15.0' not in ansible.specifier
    assert '2.18.0' not in ansible.specifier
