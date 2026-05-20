#!/usr/bin/env python3
"""Lint <TOKEN> usage across the canon coordination files.

Usage:
    lint-tokens [--canon-dir <dir>]

Scans command_START.yaml, COORDINATE.md, *.md role files, and templates/ for
`<TOKEN>` patterns. Compares with the token catalog in PROJECT_VARIABLES.md.

Exits non-zero if unknown tokens are found. Unused catalog tokens are warned
about but do not fail.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from greatminds.core.paths import find_canon_dir

TOKEN_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")
ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}")
# **Tokens used:** STAND_HOST_A, STAND_HOST_B (PROJECT.md), COORD_POSTGRES_DSN (PROJECT.env)
TOKENS_USED_RE = re.compile(
    r"\*\*Tokens used:\*\*\s*([A-Z_][A-Z0-9_,\s\(\)\./]*)",
    re.IGNORECASE,
)
CATALOG_ROW_RE = re.compile(r"`<([A-Z_][A-Z0-9_]*)>`")

SCAN_GLOBS = [
    "command_START.yaml",
    "COORDINATE.md",
    "*.md",
    "templates/**/*.md",
    "plugins/**/SKILL.md",
    "mcp/*.json",
]
EXCLUDE_NAMES = {"PROJECT_VARIABLES.md", "INSTALL.md", "README.md", "BOT_STREAM_DIVERGENCE.md"}

# Tokens that look like <TOKEN> but are documentation placeholders for the
# task-id syntax inside paths, not real project variables.
KNOWN_NON_TOKENS = {
    "ISO", "X", "Y", "N", "SEQ", "TOKEN", "ROLE", "TASK", "PATH",
    # documentation placeholders that appear inside SKILL.md prose
    # for illustrative purposes (project-agnostic examples, fragment
    # placeholders, generic SvelteKit/test/etc tokens):
    "DB", "DSN", "TASK_ID", "TEST_DB_DSN", "PUBLIC_API_URL", "YOUR_TOKEN",
}


def read_catalog(canon_dir: Path) -> set[str]:
    """Extract the canonical token set from PROJECT_VARIABLES.md.

    Skips documentation placeholders (KNOWN_NON_TOKENS); these may appear in
    explanatory prose like "replace each <TOKEN> with…" and are not real
    project variables.
    """
    catalog: set[str] = set()
    var_path = canon_dir / "PROJECT_VARIABLES.md"
    if not var_path.exists():
        return catalog
    for line in var_path.read_text(encoding="utf-8").splitlines():
        for m in CATALOG_ROW_RE.finditer(line):
            name = m.group(1)
            if name in KNOWN_NON_TOKENS:
                continue
            catalog.add(name)
    return catalog


def scan_file(path: Path) -> dict[str, list[int]]:
    """Return {token: [line_numbers]} found in file.

    Detects three reference forms (all map to the same canonical token):
    - `<TOKEN>` — prose-style placeholder (command_START.yaml, PROJECT.md)
    - `${TOKEN}` or `${TOKEN:-default}` — shell-style env-var (SKILL.md Bash
       blocks, mcp/canon.json, settings.local.json templates)
    - `**Tokens used:** TOKEN1, TOKEN2 (...)` paragraph in SKILL.md —
       declaration that the skill expects these env vars at runtime
    """
    found: dict[str, list[int]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return found
    for line_num, line in enumerate(text.splitlines(), start=1):
        for m in TOKEN_RE.finditer(line):
            name = m.group(1)
            if name in KNOWN_NON_TOKENS:
                continue
            found.setdefault(name, []).append(line_num)
        for m in ENV_RE.finditer(line):
            name = m.group(1)
            if name in KNOWN_NON_TOKENS:
                continue
            found.setdefault(name, []).append(line_num)
        m = TOKENS_USED_RE.search(line)
        if m:
            body = m.group(1)
            # extract uppercase identifiers, ignore parenthetical annotations
            # like "(PROJECT.md)" or "(PROJECT.env)"
            for n in re.findall(r"\b([A-Z_][A-Z0-9_]*)\b", body):
                if n in KNOWN_NON_TOKENS or n in {"PROJECT", "env", "md"}:
                    continue
                found.setdefault(n, []).append(line_num)
    return found


def iter_files(canon_dir: Path):
    seen: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for path in canon_dir.glob(pattern):
            if path.is_file() and path.name not in EXCLUDE_NAMES and path not in seen:
                seen.add(path)
                yield path


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-lint-tokens`` in pyproject.toml."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--canon-dir",
        type=Path,
        default=None,
        help="canon data directory (default: greatminds.data shipped with the package)",
    )
    args = parser.parse_args(argv)
    canon_dir: Path = args.canon_dir if args.canon_dir is not None else find_canon_dir()

    catalog = read_catalog(canon_dir)
    if not catalog:
        print("error: PROJECT_VARIABLES.md has no token catalog", file=sys.stderr)
        return 2

    used: dict[str, list[tuple[Path, int]]] = {}
    for path in iter_files(canon_dir):
        found = scan_file(path)
        for name, lines in found.items():
            for line_num in lines:
                used.setdefault(name, []).append((path, line_num))

    unknown = sorted(set(used) - catalog)
    unused = sorted(catalog - set(used))

    if unknown:
        print(f"ERROR: {len(unknown)} unknown token(s) used but not declared in PROJECT_VARIABLES.md:")
        for name in unknown:
            print(f"  <{name}>:")
            for path, line_num in used[name][:5]:
                rel = path.relative_to(canon_dir)
                print(f"    {rel}:{line_num}")
            if len(used[name]) > 5:
                print(f"    ... and {len(used[name]) - 5} more occurrences")
        print()

    if unused:
        print(f"warning: {len(unused)} catalog token(s) never used:")
        for name in unused:
            print(f"  <{name}>")
        print()

    if not unknown and not unused:
        print(f"OK: {len(used)} tokens used, all declared in catalog.")

    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
