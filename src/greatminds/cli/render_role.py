"""Render a role's bootstrap command with project tokens substituted.

``ROLE`` is one of the keys under ``roles:`` in ``command_START.yaml``
(e.g. ``DEVELOPER``, ``ARCHITECT-PLANNER``, ``UI-DEVELOPER-FAST``).

``--project-dir`` points at an installed project containing
``coordination/PROJECT.md`` (default: current working directory).
``--canon-dir`` defaults to the packaged ``greatminds.data`` shipped
with this wheel; pass to override (test fixtures, dev clones).

Prints the rendered bootstrap text to stdout; warnings about unresolved
tokens go to stderr.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, info, warn


TOKEN_RE = re.compile(r"<([A-Z_][A-Z0-9_]*)>")
TABLE_ROW_RE = re.compile(r"^\|\s*`<([A-Z_][A-Z0-9_]*)>`\s*\|\s*(.+?)\s*\|\s*$")

# Documentation placeholders that look like <TOKEN> but are intentional
# in-text variables, not project-installation tokens.
DOC_PLACEHOLDERS = {"ISO", "X", "Y", "SEQ", "TOKEN", "ROLE", "TASK", "PATH"}


def read_project_tokens(project_md: Path) -> dict[str, str]:
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
    missing: set[str] = set()

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in tokens:
            return tokens[name]
        if name not in DOC_PLACEHOLDERS:
            missing.add(name)
        return m.group(0)

    return TOKEN_RE.sub(repl, text), missing


@click.command(name="render-role",
               short_help="render a role bootstrap with PROJECT.md tokens",
               help=__doc__)
@click.argument("role")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing coordination/PROJECT.md (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
def render_role(role: str, project_dir: Path | None, canon_dir: Path | None) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    yaml_path = canon_dir / "command_START.yaml"
    if not yaml_path.exists():
        err(f"error: {yaml_path} not found")
        raise click.exceptions.Exit(2)

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    common = data.get("common", "").rstrip()
    roles = data.get("roles") or {}
    role_entry = roles.get(role)
    if role_entry is None:
        available = ", ".join(sorted(roles))
        err(f"error: role '{role}' not found. Available: {available}")
        raise click.exceptions.Exit(2)

    if (project_dir / "PROJECT.md").is_file() and project_dir.name == "coordination":
        project_md = project_dir / "PROJECT.md"
    else:
        project_md = project_dir / "coordination" / "PROJECT.md"
    tokens = read_project_tokens(project_md)

    body = role_entry.get("body", "")
    body_with_common = body.replace("{{COMMON}}", common)
    # 0311 lifecycle fix: the inter-tick tail is lifecycle-specific. A driven
    # role must NEVER sleep/ScheduleWakeup/loop — a headless ``claude -p`` turn
    # that sleeps never returns and freezes coordd's run-lock. Self-loop / chat
    # roles keep the "sleep 7200 / infinite loop" tail. Substitute the right
    # one for the role's launch mode into the {{LIFECYCLE_TAIL}} placeholder.
    launch = (role_entry.get("launch") or "").lower()
    tail_key = "lifecycle_driven" if launch == "driven" else "lifecycle_loop"
    tail = (data.get(tail_key) or "").rstrip()
    body_with_common = body_with_common.replace("{{LIFECYCLE_TAIL}}", tail)
    rendered, missing = substitute_tokens(body_with_common, tokens)

    # 0337 (DOD2): append the machine-readable CLI-only coordination-access
    # rule so it reaches the AGENT-FACING surface — `render-role` is what
    # start-agent / the driven driver inject into each role's prompt. The
    # role_contract render is NOT on that path; this command is.
    from greatminds.cli.role_contract import (
        load_coordination_access, format_coordination_access)
    ca_block = format_coordination_access(load_coordination_access(canon_dir))
    if ca_block:
        rendered = rendered.rstrip() + "\n\n" + ca_block

    # The rendered prompt is the script's only stdout product (consumers
    # pipe / capture it via subprocess) — keep it as a plain echo, not
    # coloured, so it round-trips cleanly.
    click.echo(rendered.rstrip())
    if missing:
        warn("")
        warn(f"warning: {len(missing)} unresolved token(s): {', '.join(sorted(missing))}")
        warn(f"  (looked in {project_md})")


if __name__ == "__main__":
    render_role()
