"""Lint <TOKEN> usage across the canon coordination files.

Scans ``command_START.yaml``, ``COORDINATE.md``, role docs (``*.md``)
and ``templates/`` for ``<TOKEN>`` patterns. Compares with the token
catalog in ``PROJECT_VARIABLES.md``.

Exits non-zero if unknown tokens are found. Unused catalog tokens are
warned about but do not fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, ok, warn


TOKEN_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")
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

# Tokens that look like <TOKEN> but are documentation placeholders for
# the task-id syntax inside paths, not real project variables.
KNOWN_NON_TOKENS = {
    "ISO", "X", "Y", "N", "SEQ", "TOKEN", "ROLE", "TASK", "PATH",
    "DB", "DSN", "TASK_ID", "TEST_DB_DSN", "PUBLIC_API_URL", "YOUR_TOKEN",
}


def read_catalog(canon_dir: Path) -> set[str]:
    catalog: set[str] = set()
    var_path = canon_dir / "PROJECT_VARIABLES.md"
    if not var_path.exists():
        return catalog
    for line in var_path.read_text(encoding="utf-8").splitlines():
        m = CATALOG_ROW_RE.search(line)
        if m:
            name = m.group(1)
            if name not in KNOWN_NON_TOKENS:
                catalog.add(name)
    return catalog


def iter_files(canon_dir: Path):
    seen: set[Path] = set()
    for pattern in SCAN_GLOBS:
        for f in canon_dir.glob(pattern):
            if not f.is_file():
                continue
            if f.name in EXCLUDE_NAMES:
                continue
            if f in seen:
                continue
            seen.add(f)
            yield f


def scan_file(path: Path) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for line_num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in TOKEN_RE.finditer(line):
            name = m.group(1)
            if name in KNOWN_NON_TOKENS:
                continue
            found.setdefault(name, []).append(line_num)
    return found


@click.command(name="lint-tokens",
               short_help="lint <TOKEN> usage vs PROJECT_VARIABLES catalog",
               help=__doc__)
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
def lint_tokens(canon_dir: Path | None) -> None:
    canon_dir = canon_dir or find_canon_dir()
    catalog = read_catalog(canon_dir)
    if not catalog:
        err("error: PROJECT_VARIABLES.md has no token catalog")
        raise click.exceptions.Exit(2)

    used: dict[str, list[tuple[Path, int]]] = {}
    for path in iter_files(canon_dir):
        for name, lines in scan_file(path).items():
            for line_num in lines:
                used.setdefault(name, []).append((path, line_num))

    unknown = sorted(set(used) - catalog)
    unused = sorted(catalog - set(used))

    if unknown:
        err(f"ERROR: {len(unknown)} unknown token(s) used but not declared in PROJECT_VARIABLES.md:")
        for name in unknown:
            err(f"  <{name}>:")
            for path, line_num in used[name][:5]:
                rel = path.relative_to(canon_dir)
                err(f"    {rel}:{line_num}")
            if len(used[name]) > 5:
                err(f"    ... and {len(used[name]) - 5} more occurrences")
        click.echo()

    if unused:
        warn(f"warning: {len(unused)} catalog token(s) never used:")
        for name in unused:
            warn(f"  <{name}>")
        click.echo()

    if not unknown and not unused:
        ok(f"OK: {len(used)} tokens used, all declared in catalog.")

    if unknown:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    lint_tokens()
