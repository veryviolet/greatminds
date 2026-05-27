"""Phase F canon pins for stand-profile role documentation."""

from greatminds.core.paths import find_canon_dir


def _role_text(name: str) -> str:
    return (find_canon_dir() / "roles" / name).read_text(encoding="utf-8")


def test_canon_mentions_stand_profile_lookup() -> None:
    text = _role_text("STAND-KEEPER.md")
    assert "coordination/stand-profiles/" in text
    assert "load_profile(coord, lease.profile)" in text


def test_tester_canon_excludes_deploy_scope() -> None:
    text = _role_text("TESTER.md")
    role_scope = text.split("## Bootstrap", maxsplit=1)[0].lower()
    assert "probe" in role_scope
    for forbidden in ("rsync", "build wheel", "install"):
        assert forbidden not in role_scope


def test_coordinate_md_has_stand_profiles_section() -> None:
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    assert "### 8.1 Stand profiles (`coordination/stand-profiles/`)" in text
    assert "coordination/stand-profiles/<name>.{yaml,md}" in text
