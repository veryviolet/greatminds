"""Tests for task 0295 (0276i): the md-only ``liveness-prose``
profile ships in the canon templates so fresh setups expose the
md execution path without operator-level scaffolding.

Pre-0295 the only md profiles in canon (``full-deploy.md``,
``smoke-only.md``) had yaml twins; the loader's YAML-preferred
rule meant ``execute_md_profile`` never ran live. The new
``liveness-prose.md`` has NO yaml twin, so the loader resolves it
as MD and SK exercises the prose-driven path end-to-end.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod
from greatminds.cli import stand_profile as sp
from greatminds.core.paths import find_canon_dir


# ---------- canon source ----------


def test_liveness_prose_md_lives_in_canon() -> None:
    """0295: the ``liveness-prose.md`` template must ship in
    ``src/greatminds/data/templates/stand-profiles/`` so
    ``greatminds setup`` seeds it into each fresh project."""
    p = (find_canon_dir() / "templates" / "stand-profiles"
         / "liveness-prose.md")
    assert p.is_file(), (
        f"0295: canon liveness-prose.md missing at {p}"
    )


def test_liveness_prose_has_no_yaml_twin() -> None:
    """The whole POINT of liveness-prose is to exercise the md
    branch — if a yaml file with the same stem appeared, the
    loader would pick it instead per the YAML-preferred rule."""
    yaml_twin = (find_canon_dir() / "templates" / "stand-profiles"
                 / "liveness-prose.yaml")
    assert not yaml_twin.exists(), (
        "0295: liveness-prose MUST NOT have a yaml twin — that "
        "would short-circuit the md execution path the profile "
        "exists to exercise"
    )


def test_liveness_prose_uses_dollar_substitution() -> None:
    """The prose must reference ``${var}`` substitutions so the md
    renderer demonstrates its value — otherwise the file is just
    a static prose snippet."""
    text = (find_canon_dir() / "templates" / "stand-profiles"
            / "liveness-prose.md").read_text(encoding="utf-8")
    for var in ("${host}", "${user}", "${deploy_path}",
                 "${task_id}", "${lease_id}"):
        assert var in text, (
            f"0295: liveness-prose.md must reference {var} "
            "(canon substitution variables)"
        )


def test_liveness_prose_has_frontmatter_with_prereq_flag() -> None:
    """0276 contract: prose profiles may carry an optional YAML
    frontmatter block with metadata (``deploy_prerequisites_only``).
    liveness-prose declares it false to make the contract visible
    at the top of the file."""
    text = (find_canon_dir() / "templates" / "stand-profiles"
            / "liveness-prose.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), (
        "0295: liveness-prose.md must start with --- frontmatter"
    )
    # The frontmatter declares the prereq-only flag explicitly.
    head = text.split("\n---\n", 1)[0]
    assert "deploy_prerequisites_only" in head


# ---------- setup seeder picks up the new file ----------


def test_setup_seeds_liveness_prose(tmp_path: Path) -> None:
    """``_seed_stand_profiles`` must copy the new md template
    into the project's ``coordination/stand-profiles/`` on each
    fresh setup. Counts: 5 files now (full-deploy.yaml/md +
    smoke-only.yaml/md + liveness-prose.md)."""
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)
    copied, skipped = setup_mod._seed_stand_profiles(
        coord, find_canon_dir())
    assert copied == 5
    assert (coord / "stand-profiles" / "liveness-prose.md").is_file()


# ---------- Phase B loader resolves to md format ----------


def test_loader_resolves_liveness_prose_as_md(tmp_path: Path) -> None:
    """End-to-end: setup seeds → loader returns format='md' for
    liveness-prose (no yaml twin → md branch wins)."""
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)
    setup_mod._seed_stand_profiles(coord, find_canon_dir())
    spec = sp.load_profile(coord, "liveness-prose")
    assert spec.format == "md"
    assert spec.md_content is not None
    assert "${host}" in spec.md_content
    assert spec.deploy_prerequisites_only is False
