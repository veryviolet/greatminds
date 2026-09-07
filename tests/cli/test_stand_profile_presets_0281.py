"""Tests for task 0281 (0276 Phase E): stand-profile presets.

Phase E ships canon ``full-deploy`` + ``smoke-only`` profile
YAML examples for explicit project selection.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_profile as sp
from greatminds.core.paths import find_canon_dir


CANON_NAMES = (
    # 1.6.0: YAML/ansible only — MD/prose profiles removed.
    "full-deploy.yaml",
    "smoke-only.yaml",
    "vite-dev.yaml",
)


# ---------- canon source ----------


def test_canon_ships_all_four_preset_files() -> None:
    """The four canonical presets must live under
    ``src/greatminds/data/templates/stand-profiles/`` so the wheel
    carries them and setup can copy them out."""
    src_dir = find_canon_dir() / "templates" / "stand-profiles"
    assert src_dir.is_dir(), (
        "0281: canon templates/stand-profiles/ dir must exist"
    )
    present = {p.name for p in src_dir.iterdir() if p.is_file()}
    for name in CANON_NAMES:
        assert name in present, (
            f"0281: canon preset {name!r} missing from {src_dir}"
        )


def test_full_deploy_yaml_is_valid_ansible_subset() -> None:
    """The canonical ``full-deploy.yaml`` parses as YAML, top-level is a
    list of plays; the deploy play (the last one — an add_host bootstrap
    play precedes it) has the schema-required fields."""
    src = (find_canon_dir() / "templates" / "stand-profiles"
           / "full-deploy.yaml")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 1
    play = data[-1]
    for field in ("name", "hosts", "tasks"):
        assert field in play, (
            f"0281: full-deploy.yaml deploy play missing {field!r}"
        )
    assert isinstance(play["tasks"], list) and play["tasks"]


def test_full_deploy_yaml_tags_prerequisite_steps() -> None:
    """At least one task in full-deploy.yaml must carry the
    ``prerequisite`` tag so warmup leases (Phase C's
    ``deploy_prerequisites_only`` flag) can isolate the prep steps."""
    src = (find_canon_dir() / "templates" / "stand-profiles"
           / "full-deploy.yaml")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    play = data[-1]
    has_prereq = any(
        "prerequisite" in (t.get("tags") or [])
        for t in play["tasks"]
        if isinstance(t, dict)
    )
    assert has_prereq, (
        "0281: full-deploy.yaml must tag at least one task with "
        "'prerequisite' for warmup-lease support"
    )


def test_smoke_only_yaml_is_valid_subset() -> None:
    src = (find_canon_dir() / "templates" / "stand-profiles"
           / "smoke-only.yaml")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) >= 1
    play = data[-1]
    for field in ("name", "hosts", "tasks"):
        assert field in play
