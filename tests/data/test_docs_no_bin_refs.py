"""Canon data files must reference the unified `greatminds <sub>` CLI."""
from __future__ import annotations

import re
from pathlib import Path


BANNED = re.compile(
    r"\bbin/(task|inbox|stand|plan|gate_check|wake_check|watchdog)\b"
)
DATA = Path(__file__).resolve().parents[2] / "src" / "greatminds" / "data"
ROOT = Path(__file__).resolve().parents[2]
DOC_SUFFIXES = {".md", ".toml", ".yaml", ".yml"}
HISTORICAL_MARKERS = re.compile(
    r"\b(legacy|deprecated|obsolete|historical|retired)\b|"
    r"\bremoved\b|"
    r"\bthe old\b|"
    r"\bpre-[0-9]|\bpost-[0-9]|"
    r"\b0\.1\.x\b|\b1\.3\.0\b|\b1\.5\.0\b|\b1\.6\.0\b|"
    r"\b0(1[0-9][0-9]|2[0-9][0-9]|3[0-9][0-9])\b"
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def test_no_stale_bin_refs_in_canon_data():
    offenders: list[str] = []
    for f in DATA.rglob("*"):
        if not f.is_file() or f.suffix not in (".toml", ".md", ".yaml"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if BANNED.search(line):
                offenders.append(
                    f"{f.relative_to(DATA)}:{n}: {line.rstrip()}"
                )
    assert not offenders, (
        "bin/* refs in canon data — replace with `greatminds <sub>`:\n"
        + "\n".join(offenders)
    )


def test_no_stale_per_role_codex_home_auth_docs():
    """.codex-home dirs are config sources, not CODEX_HOME auth homes."""
    stale = re.compile(
        r"CODEX_HOME=<project>/coordination/\.codex-home/<role>|"
        r"CODEX_HOME=<project>/coordination/\.codex-home/[a-z-]+"
    )
    roots = [ROOT / "docs", DATA]
    offenders: list[str] = []
    for root in roots:
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix not in (".toml", ".md", ".yaml"):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if stale.search(line):
                    offenders.append(
                        f"{f.relative_to(ROOT)}:{n}: {line.rstrip()}"
                    )
    assert not offenders, (
        "Codex auth docs: per-role .codex-home is config source only; "
        "CODEX_HOME must point at the single machine Codex home:\n"
        + "\n".join(offenders)
    )


def _public_doc_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "pyproject.toml"]
    for root in (ROOT / "docs", DATA):
        files.extend(
            f for f in root.rglob("*")
            if f.is_file() and f.suffix in DOC_SUFFIXES
        )
    return sorted(files)


def test_public_docs_and_canon_do_not_describe_previous_contracts():
    offenders: list[str] = []
    for f in _public_doc_files():
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if HISTORICAL_MARKERS.search(line):
                offenders.append(
                    f"{f.relative_to(ROOT)}:{n}: {line.rstrip()}"
                )
    assert not offenders, (
        "public docs/canon must describe only the current contract:\n"
        + "\n".join(offenders)
    )


def test_public_docs_and_canon_are_english_only():
    offenders: list[str] = []
    for f in _public_doc_files():
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if CYRILLIC.search(line):
                offenders.append(
                    f"{f.relative_to(ROOT)}:{n}: {line.rstrip()}"
                )
    assert not offenders, (
        "public docs/canon must be English-only:\n" + "\n".join(offenders)
    )


def test_quickstarts_cover_agent_tools_and_stand_registry_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first_project = (
        ROOT / "docs" / "getting-started" / "first-project.md"
    ).read_text(encoding="utf-8")
    stand_ops = (
        ROOT / "docs" / "concepts" / "stand-operations.md"
    ).read_text(encoding="utf-8")
    combined = "\n".join([readme, first_project, stand_ops])

    for required in (
        "Claude Code",
        "OpenAI Codex",
        "Default tool",
        "coord.yaml",
        "coordination/PROJECT.env",
        "coordination/stand-profiles.yaml",
        "coordination/stand-profiles/",
        "Ansible",
        "used_for",
        "default_for",
        "stand_profile_registry",
        "greatminds stand profiles list",
        "greatminds stand profiles doctor",
        "--profile-approval USER_APPROVED",
        "requires_explicit_user_approval",
        "allowed_roles",
    ):
        assert required in combined
