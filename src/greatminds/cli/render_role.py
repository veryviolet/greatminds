#!/usr/bin/env python3
"""Render a role's bootstrap command with project tokens substituted.

Usage:
    render-role <ROLE> [--project-dir <dir>] [--canon-dir <dir>]

ROLE is one of the keys under `roles:` in command_START.yaml (e.g. DEVELOPER,
ARCHITECT-PLANNER, UI-DEVELOPER-FAST).

--project-dir points at an installed project that contains coordination/PROJECT.md
(default: current working directory).
--canon-dir points at the canon /opt/coordination (default: parent of this script).

Prints the rendered bootstrap text to stdout. Pipe into /loop or copy by hand.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

TOKEN_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")
TABLE_ROW_RE = re.compile(r"^\|\s*`<([A-Z_][A-Z0-9_]*)>`\s*\|\s*(.+?)\s*\|\s*$")

# Documentation placeholders that look like <TOKEN> but are intentional
# in-text variables, not project-installation tokens. Render leaves them as
# is and does not warn.
DOC_PLACEHOLDERS = {"ISO", "X", "Y", "SEQ", "TOKEN", "ROLE", "TASK", "PATH"}


def read_project_tokens(project_md: Path) -> dict[str, str]:
    """Parse PROJECT.md and return token→value dict.

    PROJECT.md uses 2-column tables: | `<TOKEN>` | value |
    Strips surrounding backticks from values so rendered text doesn't end up
    with double-quoting.
    """
    tokens: dict[str, str] = {}
    if not project_md.exists():
        return tokens
    for raw_line in project_md.read_text(encoding="utf-8").splitlines():
        m = TABLE_ROW_RE.match(raw_line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
                value = value[1:-1]
            tokens[m.group(1)] = value
    return tokens


def substitute_tokens(text: str, tokens: dict[str, str]) -> tuple[str, set[str]]:
    """Replace <TOKEN> with values from `tokens`. Returns (rendered, missing)."""
    missing: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in tokens:
            return tokens[name]
        if name not in DOC_PLACEHOLDERS:
            missing.add(name)
        return m.group(0)

    return TOKEN_RE.sub(repl, text), missing


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-render-role`` in pyproject.toml."""
    from greatminds.core.paths import find_canon_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", help="Role key from command_START.yaml")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to project root containing coordination/PROJECT.md (default: cwd)",
    )
    parser.add_argument(
        "--canon-dir",
        type=Path,
        default=None,
        help="Path to canon data (default: packaged greatminds.data)",
    )
    args = parser.parse_args(argv)

    canon_dir: Path = args.canon_dir if args.canon_dir is not None else find_canon_dir()
    yaml_path = canon_dir / "command_START.yaml"
    if not yaml_path.exists():
        print(f"error: {yaml_path} not found", file=sys.stderr)
        return 2

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    common = data.get("common", "").rstrip()
    roles = data.get("roles") or {}
    role_name = args.role
    role = roles.get(role_name)
    if role is None:
        available = ", ".join(sorted(roles))
        print(f"error: role '{role_name}' not found. Available: {available}", file=sys.stderr)
        return 2

    # Resolve PROJECT.md: if --project-dir already points at a coordination/
    # directory, look there directly; otherwise append coordination/.
    if (args.project_dir / "PROJECT.md").is_file() and args.project_dir.name == "coordination":
        project_md = args.project_dir / "PROJECT.md"
    else:
        project_md = args.project_dir / "coordination" / "PROJECT.md"
    tokens = read_project_tokens(project_md)

    body = role.get("body", "")
    body_with_common = body.replace("{{COMMON}}", common)
    rendered, missing = substitute_tokens(body_with_common, tokens)

    print(rendered.rstrip())
    if missing:
        print("", file=sys.stderr)
        print(
            f"warning: {len(missing)} unresolved token(s): {', '.join(sorted(missing))}",
            file=sys.stderr,
        )
        print(f"  (looked in {project_md})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
