"""Tests for task 0278 (0276 Phase B): stand-profile loader.

Pure parser tests. No SK runtime, no subprocess, no real coord dir.
The fixtures build a minimal ``<tmp>/stand-profiles/`` directory and
exercise the loader's public API.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_profile as sp
from greatminds.core.errors import GreatMindsError


# ---------- fixtures ----------


def _profiles_dir(tmp_path: Path) -> Path:
    d = tmp_path / sp.STAND_PROFILES_DIRNAME
    d.mkdir(parents=True)
    return d


def _write_yaml(profiles_dir: Path, name: str, data: dict) -> Path:
    p = profiles_dir / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def _write_md(profiles_dir: Path, name: str, text: str) -> Path:
    p = profiles_dir / f"{name}.md"
    p.write_text(text, encoding="utf-8")
    return p


def _valid_playbook(name: str = "full-deploy") -> dict:
    return {
        "name": name,
        "hosts": "avatar",
        "tasks": [
            {"name": "ping", "ansible.builtin.ping": None},
        ],
    }


# ---------- happy paths ----------


def test_load_yaml_profile(tmp_path: Path) -> None:
    """YAML file present → ``format='yaml'``, ``yaml_data`` populated
    verbatim, ``md_content`` is None."""
    pd = _profiles_dir(tmp_path)
    _write_yaml(pd, "full-deploy", _valid_playbook())

    spec = sp.load_profile(tmp_path, "full-deploy")
    assert spec.name == "full-deploy"
    assert spec.format == "yaml"
    assert spec.path.name == "full-deploy.yaml"
    assert isinstance(spec.yaml_data, dict)
    assert spec.yaml_data["hosts"] == "avatar"
    assert spec.md_content is None
    assert spec.deploy_prerequisites_only is False


def test_yaml_preferred_when_both_exist(tmp_path: Path) -> None:
    """Both formats present for the same profile name → YAML wins
    (matches schema.stand_profile.lookup docstring)."""
    pd = _profiles_dir(tmp_path)
    _write_yaml(pd, "full-deploy", _valid_playbook())
    _write_md(pd, "full-deploy", "# ignored\n")

    spec = sp.load_profile(tmp_path, "full-deploy")
    assert spec.format == "yaml"


# ---------- errors ----------


def test_profile_not_found(tmp_path: Path) -> None:
    """No YAML present → GreatMindsError naming the expected YAML path
    (1.6.0: MD profiles removed, so only the .yaml candidate is named)."""
    _profiles_dir(tmp_path)
    with pytest.raises(GreatMindsError) as exc:
        sp.load_profile(tmp_path, "ghost-profile")
    msg = str(exc.value)
    assert "ghost-profile" in msg
    assert "ghost-profile.yaml" in msg


def test_yaml_missing_required_field_rejected(tmp_path: Path) -> None:
    """A YAML profile without one of the schema's required fields
    (e.g. no ``hosts``) must be rejected with a clear field-list
    message — silent acceptance would defer the failure to runtime."""
    pd = _profiles_dir(tmp_path)
    _write_yaml(pd, "bogus", {"name": "x", "tasks": []})  # missing hosts
    with pytest.raises(GreatMindsError) as exc:
        sp.load_profile(tmp_path, "bogus")
    assert "hosts" in str(exc.value)


def test_yaml_invalid_top_level_rejected(tmp_path: Path) -> None:
    """A YAML file whose top-level is a list (not a mapping) is
    invalid — playbook syntax requires a mapping per schema dialect."""
    pd = _profiles_dir(tmp_path)
    (pd / "list-not-map.yaml").write_text("- not a mapping\n",
                                            encoding="utf-8")
    with pytest.raises(GreatMindsError) as exc:
        sp.load_profile(tmp_path, "list-not-map")
    assert "mapping" in str(exc.value).lower()


def test_yaml_invalid_yaml_rejected(tmp_path: Path) -> None:
    """Malformed YAML surfaces the parser's complaint, not a
    None-deref crash."""
    pd = _profiles_dir(tmp_path)
    (pd / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(GreatMindsError) as exc:
        sp.load_profile(tmp_path, "broken")
    assert "invalid YAML" in str(exc.value)


def test_empty_profile_name_rejected(tmp_path: Path) -> None:
    """Defensive: empty/None profile name short-circuits before
    filesystem lookup."""
    with pytest.raises(GreatMindsError):
        sp.load_profile(tmp_path, "")


# ---------- deploy_prerequisites_only extraction ----------


def test_deploy_prerequisites_only_from_yaml_vars(tmp_path: Path) -> None:
    """YAML path: ``vars.deploy_prerequisites_only: true`` lifts into
    the spec's typed flag."""
    pd = _profiles_dir(tmp_path)
    data = _valid_playbook()
    data["vars"] = {"deploy_prerequisites_only": True, "other": 7}
    _write_yaml(pd, "warmup", data)

    spec = sp.load_profile(tmp_path, "warmup")
    assert spec.deploy_prerequisites_only is True


def test_deploy_prerequisites_only_yaml_default_false(tmp_path: Path) -> None:
    """No ``vars`` block at all → flag defaults to False (most common
    case for a regular full-deploy)."""
    pd = _profiles_dir(tmp_path)
    _write_yaml(pd, "full-deploy", _valid_playbook())
    spec = sp.load_profile(tmp_path, "full-deploy")
    assert spec.deploy_prerequisites_only is False


# ---------- deploy_host extraction (0363 / GitHub #9) ----------


def test_deploy_host_from_yaml_vars(tmp_path: Path) -> None:
    """YAML path: ``vars.deploy_host`` lifts into ``spec.host`` so coordd
    can thread it into ``lease_meta``."""
    pd = _profiles_dir(tmp_path)
    data = _valid_playbook()
    data["vars"] = {"deploy_host": "srv5-mlgpu-2.area.zov"}
    _write_yaml(pd, "mlgpu2", data)

    spec = sp.load_profile(tmp_path, "mlgpu2")
    assert spec.host == "srv5-mlgpu-2.area.zov"


def test_deploy_host_default_none(tmp_path: Path) -> None:
    """No ``vars.deploy_host`` → ``spec.host`` is None (deploy_lease then
    falls back to the profile name)."""
    pd = _profiles_dir(tmp_path)
    _write_yaml(pd, "full-deploy", _valid_playbook())
    spec = sp.load_profile(tmp_path, "full-deploy")
    assert spec.host is None


def test_deploy_host_blank_is_none(tmp_path: Path) -> None:
    """A blank/whitespace ``vars.deploy_host`` collapses to None rather
    than an empty-string host that would defeat the profile-name fallback."""
    pd = _profiles_dir(tmp_path)
    data = _valid_playbook()
    data["vars"] = {"deploy_host": "   "}
    _write_yaml(pd, "blankhost", data)
    spec = sp.load_profile(tmp_path, "blankhost")
    assert spec.host is None


def test_profile_paths_returns_two_candidates(tmp_path: Path) -> None:
    """``profile_paths`` is the canonical helper for resolving the
    two candidate file paths. Tests that need to assert error
    messages or seed fixtures use it to stay in sync with the
    loader's lookup."""
    yaml_p, md_p = sp.profile_paths(tmp_path, "x-profile")
    assert yaml_p == tmp_path / "stand-profiles" / "x-profile.yaml"
    assert md_p == tmp_path / "stand-profiles" / "x-profile.md"
