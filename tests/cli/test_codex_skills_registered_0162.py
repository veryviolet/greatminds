"""Tests for task 0162: canon skills registered into per-role codex
homes for codex 0.130+.

Pre-0162: 24 canon SKILL.md files were physically installed at
``.venv/lib/python3.13/site-packages/greatminds/data/plugins/*/skills/
*/SKILL.md`` but codex 0.130 agents only saw system/local skills in
their active list. Canon shared (``coordination-protocol/*``) and
per-role (``role-explorer/*``, etc.) were missing — codex never
registered them.

0162: ``_setup_codex_homes_per_role`` enumerates canon SKILL.md
folders for each role and appends ``[[skills.config]]`` entries to
the generated ``<CODEX_HOME>/config.toml``. codex 0.130 reads these
at startup and registers each path's SKILL.md.

Per codex docs (developers.openai.com/codex/config-reference): the
``skills.config.<index>.path`` key points at a folder containing
SKILL.md; ``enabled`` is a bool.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod


def _make_canon(tmp_path: Path) -> Path:
    """Build a synthetic canon dir with the codex profiles and a few
    shared + per-role plugin SKILL.md folders."""
    canon = tmp_path / "canon"
    # Codex profiles (so _setup_codex_homes_per_role has a per-role
    # config.toml to copy + append to).
    profiles = canon / "codex" / "profiles"
    profiles.mkdir(parents=True)
    for role in ("developer", "explorer"):
        (profiles / f"{role}.config.toml").write_text(
            f'developer_instructions = "stub {role}"\n\n'
            f'[profiles.{role}]\n'
            'model = "gpt-5.5"\n',
            encoding="utf-8",
        )
    # Shared plugin: two skills.
    for sk in ("fsm-mechanics", "stand-protocol"):
        sd = canon / "plugins" / "coordination-protocol" / "skills" / sk
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(f"# {sk}\nstub\n", encoding="utf-8")
    # Per-role plugin for explorer only.
    for sk in ("exploratory-probing", "bug-as-mini-task"):
        sd = canon / "plugins" / "role-explorer" / "skills" / sk
        sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(f"# {sk}\nstub\n", encoding="utf-8")
    # A claude-only sibling skill that must be SKIPPED for codex.
    claude_only = (canon / "plugins" / "coordination-protocol" / "skills"
                   / "iteration-and-blocking-claude")
    claude_only.mkdir(parents=True)
    (claude_only / "SKILL.md").write_text("# claude-only\n", encoding="utf-8")
    return canon


# ---------- enumeration helper ----------


def test_canon_skill_dirs_for_role_includes_shared(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path)
    skills = setup_mod._codex_skill_dirs_for_role(canon, "developer")
    names = [s.name for s in skills]
    assert "fsm-mechanics" in names
    assert "stand-protocol" in names


def test_canon_skill_dirs_for_role_includes_per_role(tmp_path: Path) -> None:
    canon = _make_canon(tmp_path)
    skills = setup_mod._codex_skill_dirs_for_role(canon, "explorer")
    names = [s.name for s in skills]
    assert "exploratory-probing" in names
    assert "bug-as-mini-task" in names


def test_canon_skill_dirs_for_role_excludes_other_role_per_role(tmp_path: Path) -> None:
    """A role without a dedicated plugin dir gets only the shared
    skills. DEVELOPER has no ``role-developer`` dir; must NOT receive
    explorer's skills."""
    canon = _make_canon(tmp_path)
    skills = setup_mod._codex_skill_dirs_for_role(canon, "developer")
    names = [s.name for s in skills]
    assert "exploratory-probing" not in names
    assert "bug-as-mini-task" not in names


def test_canon_skill_dirs_for_role_skips_claude_only_skills(tmp_path: Path) -> None:
    """``*-claude`` skills are claude-host-specific (different MCP /
    plugin wiring) and don't apply to codex agents."""
    canon = _make_canon(tmp_path)
    skills = setup_mod._codex_skill_dirs_for_role(canon, "developer")
    names = [s.name for s in skills]
    assert not any(n.endswith("-claude") for n in names), (
        f"claude-only skills must be skipped for codex; got {names}"
    )


def test_canon_skill_dirs_for_role_order_is_deterministic(tmp_path: Path) -> None:
    """0162 config.toml output must be deterministic so a future
    ``greatminds setup`` re-run (with cleared home) produces byte-
    identical config — no spurious diffs.

    Order is: (1) shared plugin skills alphabetically, then (2) per-
    role plugin skills alphabetically. Across plugins skills aren't
    globally sorted; the two layers stay distinct in output so the
    config is readable per-plugin-section.
    """
    canon = _make_canon(tmp_path)
    skills_a = setup_mod._codex_skill_dirs_for_role(canon, "explorer")
    skills_b = setup_mod._codex_skill_dirs_for_role(canon, "explorer")
    assert skills_a == skills_b, (
        f"non-deterministic order across calls: {skills_a!r} vs {skills_b!r}"
    )
    # Within the shared plugin, names sort.
    shared = [s.name for s in skills_a
              if "coordination-protocol" in str(s)]
    assert shared == sorted(shared), shared
    per_role = [s.name for s in skills_a if "role-explorer" in str(s)]
    assert per_role == sorted(per_role), per_role


def test_canon_skill_dirs_skips_skills_without_skill_md(tmp_path: Path) -> None:
    """Defensive: a skill folder with no SKILL.md is malformed (the
    plugin author's mistake) — codex would crash on a non-existent
    path. Skip silently."""
    canon = _make_canon(tmp_path)
    # Add a broken skill dir.
    broken = (canon / "plugins" / "coordination-protocol" / "skills"
              / "broken-no-skill-md")
    broken.mkdir()
    skills = setup_mod._codex_skill_dirs_for_role(canon, "developer")
    names = [s.name for s in skills]
    assert "broken-no-skill-md" not in names


# ---------- generated config.toml carries skill entries ----------


def test_generated_config_has_skills_config_entries(tmp_path: Path) -> None:
    """End-to-end via the setup helper: after generating a per-role
    codex home, the config.toml contains ``[[skills.config]]`` entries
    for each canon skill folder."""
    canon = _make_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._setup_codex_homes_per_role(canon, project)

    cfg = (project / "coordination" / ".codex-home" / "developer"
           / "config.toml").read_text(encoding="utf-8")
    assert "[[skills.config]]" in cfg
    assert 'enabled = true' in cfg
    # Path entries reference the canon dirs.
    assert "fsm-mechanics" in cfg
    assert "stand-protocol" in cfg


def test_generated_config_per_role_skills_present(tmp_path: Path) -> None:
    """0162: explorer's per-role skills appear in explorer's config,
    NOT in developer's."""
    canon = _make_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._setup_codex_homes_per_role(canon, project)

    explorer_cfg = (project / "coordination" / ".codex-home" / "explorer"
                    / "config.toml").read_text(encoding="utf-8")
    developer_cfg = (project / "coordination" / ".codex-home" / "developer"
                     / "config.toml").read_text(encoding="utf-8")
    assert "exploratory-probing" in explorer_cfg
    assert "exploratory-probing" not in developer_cfg


def test_generated_config_preserves_developer_instructions(tmp_path: Path) -> None:
    """Negative pin (0332 CONFIG_PROFILE_V2 split): the base
    ``config.toml`` keeps the top-level ``developer_instructions = ...``
    but must NOT carry a ``[profiles.<role>]`` table — codex 0.135
    rejects that when ``--profile`` is passed. The profile table is
    split into the sibling ``<role>.config.toml`` layer, and skill
    entries still append AFTER the base content."""
    canon = _make_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._setup_codex_homes_per_role(canon, project)

    home = project / "coordination" / ".codex-home" / "developer"
    cfg = (home / "config.toml").read_text(encoding="utf-8")
    assert cfg.startswith("developer_instructions = ")
    # 0332: the profile table is split OUT of config.toml.
    assert "[profiles.developer]" not in cfg
    # developer_instructions must precede the appended skill entries.
    instr_idx = cfg.index("developer_instructions = ")
    skill_idx = cfg.index("[[skills.config]]")
    assert instr_idx < skill_idx, (
        "0162: skill entries must be appended AFTER the base content"
    )
    # The profile keys live in the <role>.config.toml layer.
    layer = (home / "developer.config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-5.5"' in layer


def test_generated_config_idempotent_does_not_double_write_skills(tmp_path: Path) -> None:
    """Re-running setup must NOT re-append skill entries to an
    existing config.toml. The contract is 'existing files NOT
    overwritten' — operator-owned after first write."""
    canon = _make_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._setup_codex_homes_per_role(canon, project)
    cfg_path = (project / "coordination" / ".codex-home" / "developer"
                / "config.toml")
    first = cfg_path.read_text(encoding="utf-8")
    first_skill_count = first.count("[[skills.config]]")

    setup_mod._setup_codex_homes_per_role(canon, project)  # re-run
    second = cfg_path.read_text(encoding="utf-8")
    second_skill_count = second.count("[[skills.config]]")

    assert first == second, "config must be byte-identical after re-run"
    assert first_skill_count == second_skill_count, (
        "0162: skill entries must not duplicate on re-run"
    )


def test_skills_use_absolute_paths(tmp_path: Path) -> None:
    """codex 0.130 expects ``skills.config.<index>.path`` to point at
    a real directory. Relative paths would resolve against codex's
    cwd (unspecified) — use absolute paths."""
    canon = _make_canon(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    setup_mod._setup_codex_homes_per_role(canon, project)

    cfg = (project / "coordination" / ".codex-home" / "developer"
           / "config.toml").read_text(encoding="utf-8")
    # Find a path line.
    import re
    matches = re.findall(r'path = "(.+)"', cfg)
    assert matches, "no path entries found"
    for p in matches:
        assert Path(p).is_absolute(), (
            f"0162: skill path must be absolute; got {p!r}"
        )
