"""Tests for task 0281 (0276 Phase E): stand-profile presets.

Phase E ships canon ``full-deploy`` + ``smoke-only`` profile
templates in both YAML and MD form, and wires ``greatminds setup``
to copy them into ``coordination/stand-profiles/`` on each fresh
project (idempotent).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod
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
    """The canonical ``full-deploy.yaml`` parses as YAML, top-level
    is a list, the single play has the schema-required fields
    (``name``, ``hosts``, ``tasks``)."""
    src = (find_canon_dir() / "templates" / "stand-profiles"
           / "full-deploy.yaml")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    play = data[0]
    for field in ("name", "hosts", "tasks"):
        assert field in play, (
            f"0281: full-deploy.yaml play missing {field!r}"
        )
    assert isinstance(play["tasks"], list) and play["tasks"]


def test_full_deploy_yaml_tags_prerequisite_steps() -> None:
    """At least one task in full-deploy.yaml must carry the
    ``prerequisite`` tag so warmup leases (Phase C's
    ``deploy_prerequisites_only`` flag) can isolate the prep steps."""
    src = (find_canon_dir() / "templates" / "stand-profiles"
           / "full-deploy.yaml")
    data = yaml.safe_load(src.read_text(encoding="utf-8"))
    play = data[0]
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
    assert isinstance(data, list) and len(data) == 1
    play = data[0]
    for field in ("name", "hosts", "tasks"):
        assert field in play


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)
    return coord


def test_seed_stand_profiles_copies_all_four(tmp_path: Path) -> None:
    """First-time call must copy every canon preset; returned counts
    match (copied=3, skipped=0)."""
    coord = _coord(tmp_path)
    copied, skipped = setup_mod._seed_stand_profiles(
        coord, find_canon_dir())
    assert (copied, skipped) == (3, 0)
    landing = coord / "stand-profiles"
    assert landing.is_dir()
    for name in CANON_NAMES:
        assert (landing / name).is_file(), (
            f"0281: setup must copy {name} into "
            "coordination/stand-profiles/"
        )


def test_seed_stand_profiles_is_idempotent(tmp_path: Path) -> None:
    """Re-running setup must NOT overwrite operator-edited copies.
    Second call reports skipped=3, copied=0; content of the
    operator-edited file survives intact."""
    coord = _coord(tmp_path)
    setup_mod._seed_stand_profiles(coord, find_canon_dir())
    target = coord / "stand-profiles" / "full-deploy.yaml"
    operator_edit = "# OPERATOR EDIT — keep this line\n"
    target.write_text(operator_edit, encoding="utf-8")

    copied, skipped = setup_mod._seed_stand_profiles(
        coord, find_canon_dir())
    assert (copied, skipped) == (0, 3)
    assert target.read_text(encoding="utf-8") == operator_edit, (
        "0281: setup must NOT overwrite operator-edited preset files"
    )


def test_seed_stand_profiles_skips_dotfiles_and_other_extensions(
    tmp_path: Path,
) -> None:
    """Defensive: a stray ``.gitkeep`` or ``.bak`` file under the
    canon source must NOT be copied. The filter accepts only
    ``.yaml`` / ``.md``, no dotfiles. We monkeypatch a synthetic
    canon dir to avoid mutating the real one."""
    fake_canon = tmp_path / "fake-canon"
    src = fake_canon / "templates" / "stand-profiles"
    src.mkdir(parents=True)
    (src / "ok.yaml").write_text("dummy: 1\n", encoding="utf-8")
    (src / "ok.md").write_text("dummy\n", encoding="utf-8")
    (src / ".gitkeep").write_text("", encoding="utf-8")
    (src / "ignore.txt").write_text("nope\n", encoding="utf-8")

    coord = _coord(tmp_path)
    copied, skipped = setup_mod._seed_stand_profiles(coord, fake_canon)
    assert copied == 2
    landing = coord / "stand-profiles"
    landed = {p.name for p in landing.iterdir() if p.is_file()}
    assert landed == {"ok.yaml", "ok.md"}


def test_seed_stand_profiles_missing_canon_dir_is_noop(
    tmp_path: Path,
) -> None:
    """If the canon dir lacks ``templates/stand-profiles/`` (partial
    install / dev build pre-packaging), the helper returns ``(0, 0)``
    silently so setup still succeeds."""
    fake_canon = tmp_path / "no-templates"
    fake_canon.mkdir()
    coord = _coord(tmp_path)
    copied, skipped = setup_mod._seed_stand_profiles(coord, fake_canon)
    assert (copied, skipped) == (0, 0)
    # Setup helper doesn't create the target dir when there's nothing
    # to seed; the user's first manual file can do so.
    assert not (coord / "stand-profiles").exists(), (
        "no-source path must not create an empty target dir"
    )


# ---------- Phase B loader resolves the presets after setup ----------


def test_profile_loader_finds_presets_after_setup(tmp_path: Path) -> None:
    """End-to-end: after ``_seed_stand_profiles``, Phase B's
    ``load_profile`` resolves each preset name to a ProfileSpec.
    YAML wins on conflict (YAML + MD both seeded for the same name)."""
    coord = _coord(tmp_path)
    setup_mod._seed_stand_profiles(coord, find_canon_dir())

    full = sp.load_profile(coord, "full-deploy")
    assert full.format == "yaml"
    # Canon presets ship as list-of-plays (standard ansible form);
    # the loader preserves the original shape.
    assert isinstance(full.yaml_data, (dict, list)), full.yaml_data
    smoke = sp.load_profile(coord, "smoke-only")
    assert smoke.format == "yaml"
