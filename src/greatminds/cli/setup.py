"""greatminds setup — bootstrap a project to use the coordination protocol.

Creates the runtime queue tree, copies the schema / command_START /
role docs from the packaged ``greatminds.data`` directory, and seeds
the inbox + plugin-overlay layout.

After this command, the project has everything it needs to run the
fleet. Next steps:

  1. edit ``<project>/coord.yaml`` to confirm project_dir + window list
  2. fill in ``<project>/coordination/PROJECT.md`` tokens (project name,
     stand hosts, env paths, etc.)
  3. run ``greatminds launch --target tmux`` to start the fleet

No ``bin/*`` shims are created. With greatminds installed in your env
(pip, pipx, uv, poetry, pixi, conda), the unified ``greatminds`` binary
lives on PATH; per-project shims are unnecessary.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import click

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import header, info, ok, warn


QUEUES = [
    "feature_inbox", "feature_plan", "feature_dev", "feature_ui_dev",
    "feature_docs", "feature_test", "feature_docs_review",
    "feature_review", "feature_blocked", "verified", "archive",
    "user_feedback", "review_sessions",
    "stand_requests", "stand_wip", "stand_done",
    "bot_inbox", "bot_wip", "bot_done", "bot_verified", "bot_archive",
]

ROLES_LOWER = [
    "architect-planner", "architect-reviewer", "developer", "ui-developer",
    "technical-writer", "tester", "reader", "explorer", "stand-keeper",
    "user", "maintainer", "bot-user", "bot-developer",
]

ROLE_DOCS = [
    "ARCHITECT-PLANNER.md", "ARCHITECT-REVIEWER.md", "DEVELOPER.md",
    "UI-DEVELOPER.md", "TECHNICAL-WRITER.md", "TESTER.md", "READER.md",
    "EXPLORER.md", "STAND-KEEPER.md", "MAINTAINER.md", "USER.md",
    "BOT-USER.md", "BOT-DEVELOPER.md",
]


def _ensure_dir(p: Path) -> str:
    if p.is_dir():
        return "exists"
    p.mkdir(parents=True)
    return "created"


def _copy_if_missing(src: Path, dst: Path, force: bool = False) -> str:
    if not src.is_file():
        return "(canon source missing)"
    existed = dst.is_file()
    if existed and not force:
        return "exists"
    shutil.copyfile(src, dst)
    if src.stat().st_mode & 0o111:
        os.chmod(dst, dst.stat().st_mode | 0o755)
    return "overwritten" if existed else "copied"


@click.command(short_help="bootstrap a project (create queues + copy canon docs)",
               help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--force", is_flag=True,
              help="overwrite coord.yaml and PROJECT.md if present")
@click.option("--lang", "lang", default="en", metavar="CODE",
              help="user-facing language for agents (chat replies, console "
                   "status, errors). ISO code: en, ru, zh, es, fr, ja, etc. "
                   "Internal artifacts (task fields, journal, code) stay "
                   "English regardless. Default: en.")
def setup(project_dir: Path | None, force: bool, lang: str) -> None:
    project_dir = (project_dir or Path.cwd()).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    canon = find_canon_dir()
    header(f"greatminds setup: bootstrapping {project_dir}")
    info(f"  canon source: {canon}")

    # project-root config (schema, command_START, role docs — canon-data,
    # kept locally so humans can `cat <project>/DEVELOPER.md` without
    # importing the package).
    header("\nproject-root config:")
    coord_example = canon / "coord.example.yaml"
    if coord_example.is_file():
        info(f"  coord.yaml: {_copy_if_missing(coord_example, project_dir / 'coord.yaml', force)}")
    info(f"  schema.yaml: {_copy_if_missing(canon / 'schema.yaml', project_dir / 'schema.yaml', force=True)}")
    info(f"  command_START.yaml: {_copy_if_missing(canon / 'command_START.yaml', project_dir / 'command_START.yaml', force=True)}")
    info(f"  COORDINATE.md: {_copy_if_missing(canon / 'COORDINATE.md', project_dir / 'COORDINATE.md', force=True)}")
    for role_md in ROLE_DOCS:
        src = canon / "roles" / role_md
        if src.is_file():
            _copy_if_missing(src, project_dir / role_md, force=True)

    # coordination/ — runtime state
    coord = project_dir / "coordination"
    header("\ncoordination/ (runtime state):")
    info(f"  dir: {_ensure_dir(coord)}")
    info(f"  PROJECT.md: {_copy_if_missing(canon / 'templates' / 'PROJECT.md.template', coord / 'PROJECT.md', force)}")
    # Substitute the <GREATMINDS_LANG> token in PROJECT.md to the requested
    # value (default `en`). The template ships with `| `<GREATMINDS_LANG>` | `en` |`
    # so we rewrite that row's value column. Always apply this — language
    # is a project-level decision, not a "first install only" thing.
    project_md = coord / "PROJECT.md"
    if project_md.is_file():
        text = project_md.read_text(encoding="utf-8")
        import re
        new_text, n = re.subn(
            r"(`<GREATMINDS_LANG>`\s*\|\s*)`[^`]*`",
            rf"\1`{lang}`",
            text,
        )
        if n:
            project_md.write_text(new_text, encoding="utf-8")
            info(f"  GREATMINDS_LANG: {lang}")

    gi = coord / ".gitignore"
    if not gi.is_file() or force:
        gi.write_text(
            "# Runtime churn — NOT version-controlled. Everything else\n"
            "# under coordination/ (PROJECT.md, queue task files,\n"
            "# verified/archive history, templates) IS tracked.\n"
            "journal.ndjson\n"
            ".notify_state.json\n"
            "intent/\n"
            ".agent_registry/\n"
            ".locks/\n"
            ".id_counter\n"
            "heartbeat.*\n"
            "inbox/*/*\n"
            "!inbox/*/.gitkeep\n"
            "*.legacy\n"
            "PROJECT.env\n",
            encoding="utf-8",
        )
        info("  .gitignore: written")
    else:
        info("  .gitignore: exists")

    header("\nqueues:")
    for q in QUEUES:
        st = _ensure_dir(coord / q)
        gk = coord / q / ".gitkeep"
        if not gk.is_file():
            gk.touch()
        info(f"  {q}: {st}")
    _ensure_dir(coord / "intent")
    info("  intent: created/exists")

    header("\ninbox per role:")
    inbox = coord / "inbox"
    _ensure_dir(inbox)
    for r in ROLES_LOWER:
        d = inbox / r
        _ensure_dir(d)
        gk = d / ".gitkeep"
        if not gk.is_file():
            gk.touch()
        info(f"  {r}: created/exists")

    _ensure_dir(coord / ".locks")
    _ensure_dir(coord / ".agent_registry")

    # plugin overlay
    header("\nplugin overlay (project-overrides):")
    overlay = coord / "plugins.local" / "project-overrides"
    overlay_meta = overlay / ".claude-plugin"
    overlay_skills = overlay / "skills"
    _ensure_dir(overlay)
    _ensure_dir(overlay_meta)
    _ensure_dir(overlay_skills)
    pj = overlay_meta / "plugin.json"
    if not pj.is_file():
        pj.write_text(
            '{\n'
            '  "name": "project-overrides",\n'
            '  "version": "0.1.0",\n'
            '  "description": "Project-side overlay for canon coordination plugins.",\n'
            '  "author": { "name": "project-local" }\n'
            '}\n',
            encoding="utf-8",
        )
        info("  plugin.json: written")
    else:
        info("  plugin.json: exists")
    sg = overlay_skills / ".gitkeep"
    if not sg.is_file():
        sg.touch()

    mcpl = coord / "mcp.local.json"
    if not mcpl.is_file():
        mcpl.write_text('{\n  "mcpServers": {}\n}\n', encoding="utf-8")
        info("  mcp.local.json: written")
    else:
        info("  mcp.local.json: exists")

    pe_ex = coord / "PROJECT.env.example"
    if not pe_ex.is_file():
        pe_ex.write_text(
            "# coordination/PROJECT.env.example — TEMPLATE for PROJECT.env.\n"
            "# Copy to PROJECT.env (gitignored) and fill secrets per env.\n"
            "# greatminds-start-agent sources PROJECT.env before launching\n"
            "# Claude/codex/cursor so MCP servers resolve ${VAR}.\n"
            "\n"
            "#PROJECT_ROOT=/opt/your_project\n"
            "GREATMINDS_POSTGRES_DSN=\n"
            "STAND_HOST_A=\n"
            "STAND_HOST_B=\n"
            "STAND_URL_A=\n"
            "STAND_URL_B=\n",
            encoding="utf-8",
        )
        info("  PROJECT.env.example: written")
    else:
        info("  PROJECT.env.example: exists")

    # .claude/settings.local.json — Stop hook for Claude Code integration
    cclaude = project_dir / ".claude"
    _ensure_dir(cclaude)
    sl = cclaude / "settings.local.json"
    if not sl.is_file():
        settings_tpl = (
            "{{\n"
            '  "permissions": {{ "allow": [] }},\n'
            '  "autoMode": {{ "allow": ["$defaults"] }},\n'
            '  "hooks": {{\n'
            '    "Stop": [{{\n'
            '      "matcher": "",\n'
            '      "hooks": [{{\n'
            '        "type": "command",\n'
            '        "command": "greatminds stop-decide \\"${{GREATMINDS_ROLE:-UNKNOWN}}\\" --host claude --project-dir {proj}"\n'
            '      }}]\n'
            '    }}]\n'
            '  }}\n'
            "}}\n"
        )
        sl.write_text(settings_tpl.format(proj=str(project_dir)), encoding="utf-8")
        info("  .claude/settings.local.json: written")
    else:
        info("  .claude/settings.local.json: exists")

    ok("\ndone.")
    info("\nNext:")
    info(f"  1. edit {project_dir}/coord.yaml — confirm project_dir + window list")
    info(f"  2. fill {project_dir}/coordination/PROJECT.md tokens")
    info(f"  3. run: cd {project_dir} && greatminds launch --target tmux")


if __name__ == "__main__":
    setup()
