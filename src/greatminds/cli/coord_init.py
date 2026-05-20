#!/usr/bin/env python3
"""coord-init — bootstrap coordination into a fresh project.

Usage:
  coord-init [--project-dir <dir>] [--force]

Creates under <project-dir>:
  bin/                   symlinks (or copies) to canon scripts
  coord.yaml             from canon/coord.example.yaml (edit before launch)
  coordination/
    PROJECT.md           from canon/templates/PROJECT.md.template
    schema.yaml          copy of canon/schema.yaml
    .gitignore           journal.ndjson, intent/, .agent_registry/, .locks/, .id_counter
    feature_inbox/_TEMPLATE.yaml  (and other queues)
    feature_plan/, feature_dev/, feature_ui_dev/, feature_docs/,
    feature_test/, feature_docs_review/, feature_review/,
    feature_blocked/, verified/, archive/, user_feedback/,
    review_sessions/, stand_requests/, stand_wip/, stand_done/,
    intent/, inbox/<role>/

Idempotent: re-running on an initialised project copies missing pieces
only, never overwrites coordination/PROJECT.md or coord.yaml unless
--force is given.

After init:
  1. edit <project>/coord.yaml — set project_dir, tweak window list
  2. edit <project>/coordination/PROJECT.md — fill in tokens
  3. run bin/coord-tmux to spin up the tmux session
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from greatminds.core.util import die as _die_canonical


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


def die(msg: str) -> None:
    """Historical (msg-only) die signature preserved as a thin adapter."""
    _die_canonical(1, msg)


def info(msg: str) -> None:
    print(f"  {msg}")


def ensure_dir(p: Path) -> str:
    if p.is_dir():
        return "exists"
    p.mkdir(parents=True)
    return "created"


def copy_if_missing(src: Path, dst: Path, force: bool = False) -> str:
    if not src.is_file():
        return "(canon source missing)"
    if dst.is_file() and not force:
        return "exists"
    shutil.copyfile(src, dst)
    if src.stat().st_mode & 0o111:
        os.chmod(dst, dst.stat().st_mode | 0o755)
    return "copied" if not dst.is_file() else "overwritten" if force else "copied"


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-coord-init`` in pyproject.toml.

    Bootstraps a project directory to use the coordination protocol:
    creates the runtime queue tree, copies the schema / command / role
    docs from the packaged ``greatminds.data`` directory, and seeds the
    inbox + plugin overlay layout.

    No ``bin/*`` symlinks are created. With pip/pipx-installed
    ``greatminds``, the launcher and CLI tools (``greatminds-task``,
    ``greatminds-coordd``, …) live in the venv's ``bin/`` and are on
    PATH; per-project shims are unnecessary.
    """
    from greatminds.core.paths import find_canon_dir

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project-dir", type=Path, default=Path.cwd())
    ap.add_argument("--force", action="store_true",
                    help="overwrite coord.yaml and PROJECT.md if present")
    args = ap.parse_args(argv)

    canon = find_canon_dir()
    proj = args.project_dir.resolve()
    proj.mkdir(parents=True, exist_ok=True)
    print(f"coord-init: bootstrapping {proj} from canon {canon}")

    # project-root config (schema, command_START, role docs — canon-data,
    # kept locally so humans can `cat <project>/DEVELOPER.md` without
    # importing the package).
    print("\nproject-root config:")
    coord_example = canon / "coord.example.yaml"
    if coord_example.is_file():
        info(f"coord.yaml: {copy_if_missing(coord_example, proj / 'coord.yaml', args.force)}")
    info(f"schema.yaml: {copy_if_missing(canon / 'schema.yaml', proj / 'schema.yaml', force=True)}")
    info(f"command_START.yaml: {copy_if_missing(canon / 'command_START.yaml', proj / 'command_START.yaml', force=True)}")
    info(f"COORDINATE.md: {copy_if_missing(canon / 'COORDINATE.md', proj / 'COORDINATE.md', force=True)}")
    # role docs at project root (sourced from packaged greatminds.data/roles/)
    for role_md in ("ARCHITECT-PLANNER.md", "ARCHITECT-REVIEWER.md", "DEVELOPER.md",
                    "UI-DEVELOPER.md", "TECHNICAL-WRITER.md", "TESTER.md", "READER.md",
                    "EXPLORER.md", "STAND-KEEPER.md", "MAINTAINER.md", "USER.md",
                    "BOT-USER.md", "BOT-DEVELOPER.md"):
        src = canon / "roles" / role_md
        if src.is_file():
            copy_if_missing(src, proj / role_md, force=True)

    # coordination/ — runtime state (queues, journal, intent, inbox)
    coord = proj / "coordination"
    print(f"\ncoordination/ (runtime state):")
    info(f"dir: {ensure_dir(coord)}")
    info(f"PROJECT.md: {copy_if_missing(canon / 'templates' / 'PROJECT.md.template', coord / 'PROJECT.md', args.force)}")
    # .gitignore
    gi = coord / ".gitignore"
    if not gi.is_file() or args.force:
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
        info(".gitignore: written")
    else:
        info(".gitignore: exists")

    # queue dirs + gitkeep
    print("\nqueues:")
    for q in QUEUES:
        st = ensure_dir(coord / q)
        gk = coord / q / ".gitkeep"
        if not gk.is_file():
            gk.touch()
        info(f"{q}: {st}")
    ensure_dir(coord / "intent")
    info("intent: created/exists")

    # inbox per role
    inbox = coord / "inbox"
    ensure_dir(inbox)
    print("\ninbox per role:")
    for r in ROLES_LOWER:
        d = inbox / r
        ensure_dir(d)
        gk = d / ".gitkeep"
        if not gk.is_file():
            gk.touch()
        info(f"{r}: created/exists")

    # locks + agent_registry dirs (gitignored)
    ensure_dir(coord / ".locks")
    ensure_dir(coord / ".agent_registry")

    # Project-side plugin overlay. Canon plugins live in
    # /opt/coordination/plugins/ and are loaded directly via --plugin-dir;
    # this overlay is the project's per-install override layer.
    print("\nplugin overlay (project-overrides):")
    overlay = coord / "plugins.local" / "project-overrides"
    overlay_meta = overlay / ".claude-plugin"
    overlay_skills = overlay / "skills"
    ensure_dir(overlay)
    ensure_dir(overlay_meta)
    ensure_dir(overlay_skills)
    pj = overlay_meta / "plugin.json"
    if not pj.is_file():
        pj.write_text(
            '{\n'
            '  "name": "project-overrides",\n'
            '  "version": "0.1.0",\n'
            '  "description": "Project-side overlay for canon coordination plugins. Add SKILL.md under skills/<name>/ to override (same name) or extend (new name) canon skills. To disable a canon skill instead, use skillOverrides in <project>/.claude/settings.local.json.",\n'
            '  "author": { "name": "project-local" }\n'
            '}\n',
            encoding="utf-8",
        )
        info("plugin.json: written")
    else:
        info("plugin.json: exists")
    sg = overlay_skills / ".gitkeep"
    if not sg.is_file():
        sg.touch()
    rdme = overlay / "README.md"
    if not rdme.is_file():
        rdme.write_text(
            "# project-overrides\n\n"
            "Project-side overlay for canon coordination plugins. Loaded LAST\n"
            "by `bin/start_agent --plugin-dir` so last-wins precedence applies.\n\n"
            "## Three usage patterns\n\n"
            "**Replace a canon skill (override by name).** Create\n"
            "`skills/<same-name>/SKILL.md` with the same `name:` in its\n"
            "frontmatter as the canon skill — your project skill shadows\n"
            "the canon one.\n\n"
            "**Disable a canon skill.** Edit\n"
            "`<project>/.claude/settings.local.json` and add a `skillOverrides`\n"
            "field: `{\"<canon-skill-name>\": \"off\"}`.\n\n"
            "**Add a project-only skill.** Create `skills/<new-name>/SKILL.md`\n"
            "with a unique `name:`. It joins the loaded skill pool for this\n"
            "project; no canon counterpart needed.\n",
            encoding="utf-8",
        )
        info("README.md: written")
    else:
        info("README.md: exists")

    # mcp.local.json — empty stub; projects add per-project MCP servers
    # here. Canon MCPs come from /opt/coordination/mcp/canon.json.
    mcpl = coord / "mcp.local.json"
    if not mcpl.is_file():
        mcpl.write_text('{\n  "mcpServers": {}\n}\n', encoding="utf-8")
        info("mcp.local.json: written")
    else:
        info("mcp.local.json: exists")

    # PROJECT.env.example — committed template; real PROJECT.env (gitignored)
    # is copied from this and filled in per-environment with secrets.
    pe_ex = coord / "PROJECT.env.example"
    if not pe_ex.is_file():
        pe_ex.write_text(
            "# coordination/PROJECT.env.example — TEMPLATE for the real PROJECT.env.\n"
            "#\n"
            "# Copy this file to PROJECT.env (gitignored) and fill in real values.\n"
            "# bin/start_agent sources PROJECT.env before launching Claude/codex/cursor\n"
            "# so MCP servers and skill Bash blocks resolve ${VAR} via the env.\n"
            "#\n"
            "# Canon token-contract (full list + semantics in\n"
            "# <canon>/templates/PROJECT.md.template):\n"
            "\n"
            "# Project root (usually exported automatically by start_agent; set\n"
            "# explicitly only if you need a different value than $COORD_PROJECT_DIR).\n"
            "#PROJECT_ROOT=/opt/your_project\n"
            "\n"
            "# Postgres DSN with credentials (used by postgres MCP server).\n"
            "# Example: postgresql://user:password@host:5432/dbname\n"
            "COORD_POSTGRES_DSN=\n"
            "\n"
            "# Stand hostnames (if your project has a stand).\n"
            "STAND_HOST_A=\n"
            "STAND_HOST_B=\n"
            "\n"
            "# Stand REST API base URLs.\n"
            "STAND_URL_A=\n"
            "STAND_URL_B=\n",
            encoding="utf-8",
        )
        info("PROJECT.env.example: written")
    else:
        info("PROJECT.env.example: exists")

    # <project>/.claude/settings.local.json — Claude Code picks this up
    # from cwd automatically. Create with standard coordination hooks
    # only if absent; existing files are left alone.
    #
    # Hook commands reference the installed entry-points by name; they
    # must be on PATH (which pip/pipx install puts them on for the env
    # the user runs Claude Code from).
    cclaude = proj / ".claude"
    ensure_dir(cclaude)
    sl = cclaude / "settings.local.json"
    if not sl.is_file():
        # .format() template: {{...}} → literal {...}, single braces are subs.
        # The Stop hook command contains a shell expansion ${COORD_ROLE:-UNKNOWN}
        # which must reach the final file as $X; we wrap each side as {{ }}.
        settings_tpl = (
            "{{\n"
            '  "permissions": {{ "allow": [] }},\n'
            '  "autoMode": {{ "allow": ["$defaults"] }},\n'
            '  "hooks": {{\n'
            '    "Stop": [{{\n'
            '      "matcher": "",\n'
            '      "hooks": [{{\n'
            '        "type": "command",\n'
            '        "command": "greatminds-stop-decide \\"${{COORD_ROLE:-UNKNOWN}}\\" --host claude --project-dir {proj}"\n'
            '      }}]\n'
            '    }}]\n'
            '  }}\n'
            "}}\n"
        )
        sl.write_text(settings_tpl.format(proj=str(proj)), encoding="utf-8")
        info(".claude/settings.local.json: written")
    else:
        info(".claude/settings.local.json: exists")

    print("\ndone.")
    print()
    print("Next:")
    print(f"  1. edit {proj}/coord.yaml — confirm project_dir, window list")
    print(f"  2. edit {proj}/coordination/PROJECT.md — fill in tokens")
    print(f"  3. run: cd {proj} && greatminds-coord-tmux")
    return 0


if __name__ == "__main__":
    sys.exit(main())
