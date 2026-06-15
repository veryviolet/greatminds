"""Tests for task 0277 (0276 Phase A): schema + canon convention
for stand profile files.

Schema-only / canon-docs change. Runtime loader, lease metadata
plumbing, validators land in 0276 Phases B-G. Phase A's job is to
freeze the contract so downstream work can build on it.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


# ---------- schema.stand_profile section ----------


def _load_schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_schema_declares_stand_profile_section() -> None:
    """0277 Phase A: ``schema.stand_profile`` must exist with the
    canonical mapping shape so future phases can rely on it."""
    sp = _load_schema().get("stand_profile")
    assert isinstance(sp, dict), (
        "0277: schema.stand_profile must be a non-empty mapping"
    )


def test_stand_profile_directory_points_to_canon_path() -> None:
    """The directory is the per-project ``coordination/stand-profiles``
    folder. Pin the path so docs + future runtime loader agree."""
    sp = _load_schema()["stand_profile"]
    assert sp.get("directory") == "coordination/stand-profiles"


def test_stand_profile_declares_yaml_only_format() -> None:
    """1.6.0: YAML (ansible-subset) is the ONLY on-disk dialect — MD
    (prose) profiles were removed; the deploy runs deterministically in
    coordd, not via LLM-executed prose."""
    formats = _load_schema()["stand_profile"].get("formats") or []
    assert formats == ["yaml"], (
        f"0277: schema.stand_profile.formats must be [yaml] only "
        f"(got {formats!r})"
    )


def test_stand_profile_yaml_dialect_and_required_fields() -> None:
    """The YAML dialect is an ansible-playbook subset. Required fields
    pin the bare minimum SK's loader will enforce."""
    sp = _load_schema()["stand_profile"]
    assert sp.get("yaml_dialect") == "ansible_playbook"
    required = set(sp.get("yaml_required_fields") or [])
    assert {"name", "hosts", "tasks"}.issubset(required), (
        f"0277: yaml_required_fields must include name, hosts, tasks "
        f"(got {sorted(required)})"
    )


def test_stand_profile_lookup_pattern_references_profile_enum() -> None:
    """The lookup pattern documents ``<lease.profile>`` so future
    readers tie profile-file resolution to the active lease registry entry
    rather than a separate identifier scheme."""
    sp = _load_schema()["stand_profile"]
    pattern = sp.get("lookup") or ""
    assert "<lease.profile>" in pattern, (
        f"0277: schema.stand_profile.lookup must reference "
        f"<lease.profile> (got {pattern!r})"
    )
    assert "coordination/stand-profiles.yaml" in pattern
    assert ".file" in pattern


def test_stand_profile_declares_prereq_only_flag_name() -> None:
    """Phase A reserves the lease-metadata flag name SK will read in
    Phase D. Codified in schema so the name doesn't drift between
    phases."""
    sp = _load_schema()["stand_profile"]
    assert sp.get("deploy_prerequisites_only_flag") \
        == "deploy_prerequisites_only", (
        "0277: deploy_prerequisites_only_flag must be reserved here "
        "so Phase D's lease metadata uses the matching name"
    )


# ---------- canon docs document the convention ----------


def test_coordinate_md_documents_stand_profiles_directory() -> None:
    """COORDINATE.md (the canon protocol doc) must point at
    ``coordination/stand-profiles/`` so role onboarding sees the
    convention without needing to read schema."""
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    assert "coordination/stand-profiles" in text, (
        "0277: COORDINATE.md must document the stand-profiles "
        "directory convention"
    )


def test_coordinate_md_documents_lookup_convention() -> None:
    """Docs must spell out registry lookup and YAML playbook files."""
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    assert "coordination/stand-profiles.yaml" in text
    assert "coordination/stand-profiles/<file>.yaml" in text
    assert "lease.profile" in text
    assert "Registry `file`" in text, (
        "0277: COORDINATE.md must document the lookup convention "
        "(registry + YAML file + lease.profile tie-in)"
    )


def test_coordinate_md_references_schema_source() -> None:
    """Docs must point readers at ``schema.stand_profile`` as the
    source of truth — otherwise the doc and schema can drift."""
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    assert "schema.stand_profile" in text or "stand_profile" in text
