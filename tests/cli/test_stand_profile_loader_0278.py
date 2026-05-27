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


def test_load_md_profile(tmp_path: Path) -> None:
    """MD-only file present → ``format='md'``, ``md_content`` carries
    the full prose; ``yaml_data`` is None."""
    pd = _profiles_dir(tmp_path)
    body = "# Manual deploy\n\nRun the bringup recipe by hand.\n"
    _write_md(pd, "manual", body)

    spec = sp.load_profile(tmp_path, "manual")
    assert spec.format == "md"
    assert spec.md_content == body
    assert spec.yaml_data is None
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
    """Neither YAML nor MD present → GreatMindsError mentioning both
    candidate paths so the operator can `touch` the right file."""
    _profiles_dir(tmp_path)
    with pytest.raises(GreatMindsError) as exc:
        sp.load_profile(tmp_path, "ghost-profile")
    msg = str(exc.value)
    assert "ghost-profile" in msg
    assert "ghost-profile.yaml" in msg
    assert "ghost-profile.md" in msg


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


def test_deploy_prerequisites_only_from_md_frontmatter(tmp_path: Path) -> None:
    """MD path: optional ``---``-delimited YAML frontmatter at the top
    carries ``deploy_prerequisites_only: true``; body kept intact."""
    pd = _profiles_dir(tmp_path)
    body = (
        "---\n"
        "deploy_prerequisites_only: true\n"
        "owner: STAND-KEEPER\n"
        "---\n"
        "# Warmup notes\n\n"
        "Just ensure venv exists. Don't deploy.\n"
    )
    _write_md(pd, "warmup-prose", body)
    spec = sp.load_profile(tmp_path, "warmup-prose")
    assert spec.format == "md"
    assert spec.deploy_prerequisites_only is True
    # Frontmatter exposed for callers that want other ad-hoc fields.
    assert spec.md_frontmatter == {
        "deploy_prerequisites_only": True,
        "owner": "STAND-KEEPER",
    }
    # Body keeps content AFTER the frontmatter only.
    assert "# Warmup notes" in (spec.md_content or "")
    assert "owner: STAND-KEEPER" not in (spec.md_content or "")


def test_md_without_frontmatter_keeps_full_body(tmp_path: Path) -> None:
    """An MD profile with no frontmatter at all → body == file text,
    flag defaults to False, frontmatter dict is None."""
    pd = _profiles_dir(tmp_path)
    body = "Plain prose. No frontmatter.\n"
    _write_md(pd, "plain", body)
    spec = sp.load_profile(tmp_path, "plain")
    assert spec.md_content == body
    assert spec.md_frontmatter is None
    assert spec.deploy_prerequisites_only is False


def test_md_malformed_frontmatter_falls_back_to_full_body(
    tmp_path: Path,
) -> None:
    """A malformed frontmatter block must NOT make the file
    unloadable — the loader treats it as absent so the operator's
    prose still drives SK's prompt."""
    pd = _profiles_dir(tmp_path)
    body = (
        "---\n"
        "[: unparseable :]\n"
        "---\n"
        "Body still readable.\n"
    )
    _write_md(pd, "weird", body)
    spec = sp.load_profile(tmp_path, "weird")
    assert spec.md_frontmatter is None
    # Whole file kept as body since frontmatter is treated absent.
    assert "Body still readable." in (spec.md_content or "")
    assert spec.deploy_prerequisites_only is False


# ---------- helper: profile_paths ----------


def test_profile_paths_returns_two_candidates(tmp_path: Path) -> None:
    """``profile_paths`` is the canonical helper for resolving the
    two candidate file paths. Tests that need to assert error
    messages or seed fixtures use it to stay in sync with the
    loader's lookup."""
    yaml_p, md_p = sp.profile_paths(tmp_path, "x-profile")
    assert yaml_p == tmp_path / "stand-profiles" / "x-profile.yaml"
    assert md_p == tmp_path / "stand-profiles" / "x-profile.md"
