"""Tests for task 0367 (follow-up to 0366 / GitHub #16+#17): migrate
stale seeded stand profiles to the add_host / STAND_HOST topology.

Existing fleets keep a stale seeded ``coordination/stand-profiles/*.yaml``
because ``setup`` never overwrites an existing profile. The pre-rewrite
templates target ``hosts: stand`` / ``hosts: "${STAND_HOST}"`` with no
add_host bootstrap and no inventory, so the host-agnostic executor
matches zero hosts and deploys nothing (the vacuous-deploy trap).

``reseed_stale_stand_profiles`` refreshes a PRISTINE seeded copy (one
whose bytes hash to a version greatminds itself shipped) to the current
template, while leaving operator-customized profiles untouched. It runs
only on the deliberate migrate/update path, never from plain setup.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


FIXTURES = Path(__file__).parent / "fixtures_0367"


def _coord_with_profile(tmp_path: Path, name: str, content: str) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / "stand-profiles").mkdir(parents=True)
    (coord / "stand-profiles" / name).write_text(content, encoding="utf-8")
    return coord


# ---------- embedded-hash guard ----------


def test_stale_fixture_hashes_are_registered() -> None:
    """The committed stale fixtures are real greatminds-shipped profiles;
    their sha256 must appear in the embedded stale-hash allowlist (else
    the migration would never recognise them as reseedable)."""
    fd = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    so = (FIXTURES / "smoke-only.stale-4a896e1.yaml").read_text(encoding="utf-8")
    assert setup_mod._sha256(fd) in \
        setup_mod._STALE_SHIPPED_PROFILE_HASHES["full-deploy.yaml"]
    assert setup_mod._sha256(so) in \
        setup_mod._STALE_SHIPPED_PROFILE_HASHES["smoke-only.yaml"]


def test_stale_fixtures_lack_add_host_topology() -> None:
    """Sanity: the fixtures genuinely predate the rewrite (no add_host /
    stand_nodes), so refreshing them is a real fix, not a no-op."""
    for f in ("full-deploy.stale-1140ac0.yaml", "smoke-only.stale-4a896e1.yaml"):
        text = (FIXTURES / f).read_text(encoding="utf-8")
        assert not setup_mod._profile_uses_add_host(text)


def test_current_templates_use_add_host_topology() -> None:
    """The shipped templates we reseed TO must carry the add_host
    topology — otherwise the migration would replace one vacuous profile
    with another."""
    src = find_canon_dir() / "templates" / "stand-profiles"
    for name in ("full-deploy.yaml", "smoke-only.yaml", "vite-dev.yaml"):
        text = (src / name).read_text(encoding="utf-8")
        assert setup_mod._profile_uses_add_host(text), name


# ---------- reseed behaviour ----------


def test_reseed_refreshes_pristine_stale_profile(tmp_path: Path) -> None:
    """A pristine stale full-deploy is refreshed to the current template
    and its old bytes are backed up under stand-profiles/.backups/."""
    stale = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", stale)
    canon = find_canon_dir()

    result = setup_mod.reseed_stale_stand_profiles(coord, canon)

    assert result["reseeded"] == ["full-deploy.yaml"]
    refreshed = (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8")
    template = (canon / "templates" / "stand-profiles" / "full-deploy.yaml").read_text("utf-8")
    assert refreshed == template
    assert setup_mod._profile_uses_add_host(refreshed)
    backup = coord / "stand-profiles" / ".backups" / "full-deploy.yaml"
    assert backup.is_file() and backup.read_text("utf-8") == stale


def test_reseed_is_idempotent(tmp_path: Path) -> None:
    """Second run sees the refreshed (add_host) profile as current and
    does nothing — no spurious re-backup, no reseed."""
    stale = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", stale)
    canon = find_canon_dir()

    setup_mod.reseed_stale_stand_profiles(coord, canon)
    second = setup_mod.reseed_stale_stand_profiles(coord, canon)

    assert second["reseeded"] == []
    assert second["current"] == ["full-deploy.yaml"]


def test_reseed_leaves_customized_stale_profile_alone(tmp_path: Path) -> None:
    """A stale-looking profile (no add_host) whose bytes match NO shipped
    version is an operator edit: left in place and flagged customized."""
    custom = (
        "---\n"
        "- name: my custom legacy deploy\n"
        "  hosts: stand\n"
        "  tasks:\n"
        "    - name: do a thing\n"
        "      ansible.builtin.command: /bin/true\n"
    )
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", custom)

    result = setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert result["customized"] == ["full-deploy.yaml"]
    assert result["reseeded"] == []
    assert (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8") == custom
    assert not (coord / "stand-profiles" / ".backups").exists()


def test_reseed_leaves_operator_add_host_variant_alone(tmp_path: Path) -> None:
    """An operator's own add_host variant (already migrated, but not
    byte-identical to the template) counts as current — never clobbered."""
    variant = (
        "---\n"
        "- hosts: localhost\n"
        "  tasks:\n"
        "    - ansible.builtin.add_host: {name: x, groups: stand_nodes}\n"
        "- hosts: stand_nodes\n"
        "  tasks: [{name: noop, ansible.builtin.command: /bin/true}]\n"
        "# operator's own tweak\n"
    )
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", variant)

    result = setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert result["current"] == ["full-deploy.yaml"]
    assert result["reseeded"] == []
    assert (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8") == variant


def test_reseed_skips_profiles_never_seeded(tmp_path: Path) -> None:
    """Only profiles actually present on disk are touched; an absent
    smoke-only / vite-dev is silently skipped (not created)."""
    stale = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", stale)

    setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert not (coord / "stand-profiles" / "smoke-only.yaml").exists()
    assert not (coord / "stand-profiles" / "vite-dev.yaml").exists()


def test_reseed_no_profiles_dir_is_noop(tmp_path: Path) -> None:
    """A project that never seeded any stand profiles yields all-empty
    classification and creates nothing."""
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)

    result = setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert result == {
        "reseeded": [], "current": [], "customized": [], "missing_template": [],
    }
    assert not (coord / "stand-profiles").exists()


def test_reseed_missing_template_reported(tmp_path: Path) -> None:
    """If a known profile is on disk but the canon template is gone
    (partial/dev build), it is reported as missing_template, never
    blanked."""
    stale = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", stale)
    empty_canon = tmp_path / "empty-canon"
    (empty_canon / "templates" / "stand-profiles").mkdir(parents=True)

    result = setup_mod.reseed_stale_stand_profiles(coord, empty_canon)

    assert result["missing_template"] == ["full-deploy.yaml"]
    assert (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8") == stale


# ---------- setup stays additive (does NOT reseed) ----------


def test_setup_seed_does_not_reseed_stale(tmp_path: Path) -> None:
    """Guard the invariant: plain ``_seed_stand_profiles`` (run on every
    setup) must NOT overwrite a stale profile — only the explicit
    migrate/update reseed does. Setup stays strictly additive."""
    stale = (FIXTURES / "full-deploy.stale-1140ac0.yaml").read_text(encoding="utf-8")
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", stale)

    copied, skipped = setup_mod._seed_stand_profiles(coord, find_canon_dir())

    assert (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8") == stale
    # full-deploy skipped (exists); smoke-only + vite-dev newly copied.
    assert skipped >= 1
