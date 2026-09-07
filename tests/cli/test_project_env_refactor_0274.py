"""Packaged environment metadata and project-context templates remain valid."""
import yaml
from greatminds.core.paths import find_canon_dir


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


def test_project_variables_md_no_longer_in_canon() -> None:
    """0274: the canon ``PROJECT_VARIABLES.md`` doc is removed —
    schema.project_env.system_vars + PROJECT.md docs replace it."""
    assert not (find_canon_dir() / "PROJECT_VARIABLES.md").is_file(), (
        "0274: src/greatminds/data/PROJECT_VARIABLES.md must be "
        "deleted (replaced by schema.project_env + PROJECT.md docs)"
    )


def test_project_md_template_mentions_dollar_curly_form() -> None:
    """Discoverability: the template must explicitly tell the user
    to reference env vars via ``${name}`` shell form (not ``<TOKEN>``)
    so the format is signposted on the first read."""
    tmpl = (find_canon_dir() / "templates" / "PROJECT.md.template") \
        .read_text(encoding="utf-8")
    assert "${name}" in tmpl
