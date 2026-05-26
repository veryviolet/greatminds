"""Tests for task 0274: refactor PROJECT.env + PROJECT.md templates.

Pre-0274 PROJECT.env.example was a hardcoded list of stand-host
secrets; PROJECT.md was a sprawling ``<TOKEN>`` table that
``render_role`` substituted from. Both were inconsistent with each
other and with the canonical ``PROJECT_VARIABLES.md`` doc.

0274 makes ``schema.project_env.system_vars`` the single source of
truth: setup generates PROJECT.env directly with one ``KEY=`` per
system var + schema-derived comments, and generates PROJECT.md
with a schema-synced System variables section in prose. The legacy
``<TOKEN>`` table format is removed from the template.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema source-of-truth ----------


def test_schema_lists_project_env_system_vars() -> None:
    """The schema must declare a ``project_env.system_vars`` mapping
    so future setup runs / docs build from canon, not hardcoded
    Python tables."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    pe = doc.get("project_env") or {}
    sv = pe.get("system_vars") or {}
    assert isinstance(sv, dict) and sv, (
        "0274: schema.project_env.system_vars must be a non-empty "
        "mapping (drives PROJECT.env + PROJECT.md generation)"
    )
    # The motivating system var must be present with its acquire
    # instructions populated.
    token_meta = sv.get("GREATMINDS_UPSTREAM_TOKEN")
    assert isinstance(token_meta, dict), (
        "0274: schema must declare GREATMINDS_UPSTREAM_TOKEN under "
        "project_env.system_vars"
    )
    assert (token_meta.get("description") or "").strip()
    assert (token_meta.get("acquire_instructions") or "").strip()


def test_schema_loader_returns_metadata_for_each_var() -> None:
    """The helper ``_load_project_env_system_vars_from_canon`` must
    return the schema's mapping verbatim (just narrowed to dict
    entries) so generators can iterate it deterministically."""
    out = setup_mod._load_project_env_system_vars_from_canon(
        find_canon_dir())
    assert "GREATMINDS_UPSTREAM_TOKEN" in out
    entry = out["GREATMINDS_UPSTREAM_TOKEN"]
    assert "description" in entry
    assert "acquire_instructions" in entry


# ---------- PROJECT.env generation ----------


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "coordination").mkdir(parents=True)
    return proj


def test_project_env_generated_from_schema(tmp_path: Path) -> None:
    """``_ensure_project_env`` writes a PROJECT.env that lists each
    schema-declared var with its description, acquire instructions,
    and a bare ``KEY=`` line (no default value — user fills in)."""
    proj = _project(tmp_path)
    status = setup_mod._ensure_project_env(
        proj / "coordination", find_canon_dir(), force=False)
    assert status == "written"

    pe = (proj / "coordination" / "PROJECT.env").read_text(
        encoding="utf-8")
    # Description appears as a comment.
    assert "GREATMINDS_UPSTREAM_TOKEN" in pe
    assert "GitHub PAT" in pe
    # Acquire instructions reach the file (indented under the desc).
    assert "Settings → Developer settings" in pe
    # Bare KEY= line (no default value).
    assert "\nGREATMINDS_UPSTREAM_TOKEN=\n" in pe \
        or pe.rstrip().endswith("GREATMINDS_UPSTREAM_TOKEN=")


def test_project_env_idempotent_when_already_present(tmp_path: Path) -> None:
    """Re-running setup without ``--force`` must NOT overwrite a
    user-edited PROJECT.env. Returns ``exists``."""
    proj = _project(tmp_path)
    pe_path = proj / "coordination" / "PROJECT.env"
    pe_path.write_text("# operator-edited\nGREATMINDS_UPSTREAM_TOKEN=secret123\n",
                        encoding="utf-8")
    status = setup_mod._ensure_project_env(
        proj / "coordination", find_canon_dir(), force=False)
    assert status == "exists"
    # File content untouched.
    assert "secret123" in pe_path.read_text(encoding="utf-8")


def test_project_env_force_backs_up_then_overwrites(tmp_path: Path) -> None:
    """``--force`` overwrites the file but preserves the prior copy
    as ``PROJECT.env.bak`` so the operator can recover their values."""
    proj = _project(tmp_path)
    coord = proj / "coordination"
    pe_path = coord / "PROJECT.env"
    pe_path.write_text("# pre-existing\nGREATMINDS_UPSTREAM_TOKEN=KEEPME\n",
                        encoding="utf-8")

    status = setup_mod._ensure_project_env(coord, find_canon_dir(),
                                            force=True)
    assert status == "overwritten"
    # Backup exists with the prior content.
    bak = coord / "PROJECT.env.bak"
    assert bak.is_file()
    assert "KEEPME" in bak.read_text(encoding="utf-8")
    # New file has schema-derived content.
    new_text = pe_path.read_text(encoding="utf-8")
    assert "GitHub PAT" in new_text


# ---------- PROJECT.md template + schema-synced section ----------


def test_template_has_no_angle_bracket_tokens() -> None:
    """0274 explicit pin: PROJECT.md.template must NOT contain any
    ``<TOKEN>`` placeholders. The legacy table format is gone;
    schema-driven section is the new contract."""
    import re
    tmpl = (find_canon_dir() / "templates" / "PROJECT.md.template") \
        .read_text(encoding="utf-8")
    # Allow ``{{SYSTEM_VARS_DOCS}}`` interpolation marker (Mustache-
    # style), reject ``<UPPER_NAME>`` token form.
    leaks = re.findall(r"<[A-Z][A-Z0-9_]*>", tmpl)
    assert leaks == [], (
        f"0274: template must not contain <TOKEN> placeholders "
        f"(found: {sorted(set(leaks))})"
    )


def test_project_md_section_lists_each_system_var(tmp_path: Path) -> None:
    """``_ensure_project_md`` generates a System variables section
    with one ``### NAME`` heading per schema entry + that entry's
    description."""
    proj = _project(tmp_path)
    status = setup_mod._ensure_project_md(
        proj / "coordination", find_canon_dir(), force=False, lang="en")
    assert status == "written"
    md = (proj / "coordination" / "PROJECT.md").read_text(encoding="utf-8")
    assert "## System variables" in md
    assert "### GREATMINDS_UPSTREAM_TOKEN" in md
    assert "GitHub PAT" in md  # description rendered
    # Language footer.
    assert "Language: en" in md


def test_project_md_language_footer_uses_setup_arg(tmp_path: Path) -> None:
    """``--lang`` flows into the Language footer (replaces the old
    table-row substitution)."""
    proj = _project(tmp_path)
    setup_mod._ensure_project_md(
        proj / "coordination", find_canon_dir(), force=False, lang="ru")
    md = (proj / "coordination" / "PROJECT.md").read_text(encoding="utf-8")
    assert "Language: ru" in md
    assert "Language: en" not in md


# ---------- PROJECT_VARIABLES.md deprecated/deleted ----------


def test_project_variables_md_no_longer_in_canon() -> None:
    """0274: the canon ``PROJECT_VARIABLES.md`` doc is removed —
    schema.project_env.system_vars + PROJECT.md docs replace it."""
    assert not (find_canon_dir() / "PROJECT_VARIABLES.md").is_file(), (
        "0274: src/greatminds/data/PROJECT_VARIABLES.md must be "
        "deleted (replaced by schema.project_env + PROJECT.md docs)"
    )


# ---------- end-to-end shape ----------


def test_project_md_template_mentions_dollar_curly_form() -> None:
    """Discoverability: the template must explicitly tell the user
    to reference env vars via ``${name}`` shell form (not ``<TOKEN>``)
    so the format is signposted on the first read."""
    tmpl = (find_canon_dir() / "templates" / "PROJECT.md.template") \
        .read_text(encoding="utf-8")
    assert "${name}" in tmpl
