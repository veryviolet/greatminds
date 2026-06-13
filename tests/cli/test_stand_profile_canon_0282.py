"""Phase F canon pins for stand-profile documentation.

The per-role prose pins (coordd profile lookup, TESTER deploy
exclusion) moved to schema: the profile lookup is schema.stand_profile,
TESTER's no-deploy is schema.roles.TESTER.forbidden_actions (pinned by
test_schema_role_contracts_0288). COORDINATE.md §8.1 still carries the
operator-facing convention.
"""

from greatminds.core.paths import find_canon_dir


def test_coordinate_md_has_stand_profiles_section() -> None:
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    assert "### 8.1 Stand profiles (`coordination/stand-profiles/`)" in text
    assert "coordination/stand-profiles/<name>.yaml" in text
    assert "Only YAML/ansible profiles are executable" in text
