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

import json
import os
import re
import shutil
import sys
from pathlib import Path

import click

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import err, header, info, ok, warn


SESSION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _default_session_name(project_dir: Path) -> str:
    """Default session name = basename(project_dir.resolve()).

    Strips a single leading dot (so e.g. ``/opt/.work`` → ``work``).
    Falls back to ``"agents"`` when basename resolves empty (``/``).
    """
    try:
        name = project_dir.resolve().name
    except (OSError, RuntimeError):
        name = ""
    if name.startswith("."):
        name = name[1:]
    return name or "agents"


def _validate_session_name(name: str) -> None:
    """Raise Click Exit(2) on an invalid session name.

    Disallowed: empty, whitespace, ``/``, ``:``, ``@``, length > 64.
    Allowed alphabet: ``[A-Za-z0-9_.-]``.
    """
    if not SESSION_RE.match(name):
        err(f"session name must match `[A-Za-z0-9_.-]{{1,64}}` — got {name!r}")
        raise click.exceptions.Exit(2)


def _greatminds_bin() -> str:
    """Resolve the absolute command string to invoke greatminds.

    Returns the absolute path to the ``greatminds`` console script (as
    found by ``shutil.which`` and normalized via ``Path.resolve()``),
    or — if not found — a ``<sys.executable> -m greatminds.cli.main``
    fallback (``sys.executable`` is already absolute). Either form is
    PATH-independent AND cwd-independent, which is the whole point:
    hook commands embedded into ``.claude/settings.local.json`` must
    work in claude sessions opened without the project venv on PATH
    (e.g. a maintainer Claude launched directly from the repo, not via
    start-agent) and from arbitrary working directories.

    ``shutil.which`` can return a relative path when PATH contains
    relative entries (e.g. ``.venv/bin``), so we always normalize.
    """
    found = shutil.which("greatminds")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m greatminds.cli.main"


def _build_settings_local_json(project_dir: Path) -> str:
    """Return the JSON text for ``.claude/settings.local.json``.

    Every hook command begins with an absolute reference to greatminds
    (path or ``python -m`` fallback) so the file is portable across
    claude sessions regardless of PATH.
    """
    gm_bin = _greatminds_bin()
    stop_cmd = (
        f'{gm_bin} stop-decide "${{GREATMINDS_ROLE:-UNKNOWN}}" '
        f'--host claude --project-dir {project_dir}'
    )
    settings = {
        "permissions": {"allow": []},
        "autoMode": {"allow": ["$defaults"]},
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": stop_cmd},
                    ],
                },
            ],
        },
    }
    return json.dumps(settings, indent=2) + "\n"


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


def _copy_codex_profiles_if_missing(canon: Path) -> tuple[int, int]:
    """Copy shipped codex profiles to ``~/.codex/<basename>.config.toml``.

    Per-user, idempotent: if a destination file already exists it is NOT
    overwritten — the user may have customized it. Returns
    ``(copied, skipped)`` counts for the setup summary.

    start_agent.py picks these up automatically via ``--profile-v2 <role>``
    when ``~/.codex/<role>.config.toml`` exists (task 0047 wired the full
    path: shipped profiles → user-side config → codex agent).
    """
    src_dir = canon / "codex" / "profiles"
    if not src_dir.is_dir():
        return (0, 0)
    dst_dir = Path.home() / ".codex"
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return (0, 0)
    copied = 0
    skipped = 0
    for src in sorted(src_dir.glob("*.config.toml")):
        dst = dst_dir / src.name
        if dst.is_file():
            skipped += 1
            continue
        try:
            shutil.copyfile(src, dst)
            copied += 1
        except OSError:
            # Per-profile failure is non-fatal — keep going.
            continue
    return (copied, skipped)


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


# ---------------------------------------------------------------------------
# task 0076: pre-trust config. TESTER and STAND-KEEPER cannot walk
# through Claude Code's "Do you trust this folder?" or codex's
# "Allow Codex to run" dialogs on the avatar host (Unauthorized
# Persistence classifier blocks interactive trust acceptance). Without
# pre-trust, fresh agent spawns on a toy project sit at the dialog and
# the role contract never starts. This adds an opt-in pre-trust step
# scoped per-project: writes ONE entry for THIS project's abs path
# into the user-level config files (~/.claude.json and
# ~/.codex/config.toml), idempotent, never touching other projects'
# trust state.
# ---------------------------------------------------------------------------


def _install_claude_pretrust(project_dir: Path) -> str:
    """Mark ``project_dir`` as trust-accepted in ``~/.claude.json``.

    Schema (verified by probing a real Claude Code install):

      {
        "projects": {
          "<abs-project-path>": {
            "hasTrustDialogAccepted": true,
            ...
          }
        }
      }

    Idempotent: returns ``"existing"`` if already True, ``"written"``
    if newly added, ``"skipped: <reason>"`` on any failure (no crash —
    pre-trust is opt-in and best-effort).
    """
    abs_dir = str(project_dir.resolve())
    home_cfg = Path.home() / ".claude.json"
    try:
        if home_cfg.is_file():
            data = json.loads(home_cfg.read_text(encoding="utf-8"))
        else:
            data = {}
    except (OSError, json.JSONDecodeError) as exc:
        return f"skipped: ~/.claude.json unreadable ({exc})"
    if not isinstance(data, dict):
        return "skipped: ~/.claude.json is not a JSON object"

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return "skipped: ~/.claude.json's 'projects' is not an object"

    entry = projects.setdefault(abs_dir, {})
    if not isinstance(entry, dict):
        return f"skipped: ~/.claude.json projects[{abs_dir}] is not an object"

    if entry.get("hasTrustDialogAccepted") is True:
        return "existing"

    entry["hasTrustDialogAccepted"] = True
    # Atomic write: tempfile + rename in the same directory.
    tmp = home_cfg.with_suffix(home_cfg.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(home_cfg)
    except OSError as exc:
        return f"skipped: write failed ({exc})"
    return "written"


def _install_codex_pretrust(project_dir: Path) -> str:
    """Mark ``project_dir`` as trusted in ``~/.codex/config.toml``.

    Schema (verified):

      [projects."<abs-project-path>"]
      trust_level = "trusted"

    Idempotent: returns ``"existing"`` if a matching block is already
    present, ``"written"`` if newly added, ``"skipped: <reason>"`` on
    any failure. Append-only — never modifies existing sections, so a
    user-customized trust_level (e.g. "untrusted") is preserved.
    """
    abs_dir = str(project_dir.resolve())
    home_cfg = Path.home() / ".codex" / "config.toml"
    try:
        home_cfg.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"skipped: ~/.codex/ mkdir failed ({exc})"
    try:
        existing = home_cfg.read_text(encoding="utf-8") if home_cfg.is_file() else ""
    except OSError as exc:
        return f"skipped: ~/.codex/config.toml unreadable ({exc})"

    marker = f'[projects."{abs_dir}"]'
    if marker in existing:
        return "existing"

    # Append a fresh entry. Ensure a leading newline if the existing
    # content doesn't end with one (avoid concatenating to the last line).
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    block = f'\n{marker}\ntrust_level = "trusted"\n'
    new_content = existing + suffix + block
    tmp = home_cfg.with_suffix(home_cfg.suffix + ".tmp")
    try:
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(home_cfg)
    except OSError as exc:
        return f"skipped: write failed ({exc})"
    return "written"


@click.command(short_help="bootstrap a project (create queues + copy canon docs)",
               help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--force", is_flag=True,
              help="overwrite PROJECT.md if present. NOTE: coord.yaml "
                   "is NEVER overwritten by setup (init-style — delete it "
                   "first to regenerate). --force does not apply to coord.yaml.")
@click.option("--lang", "lang", default="en", metavar="CODE",
              help="user-facing language for agents (chat replies, console "
                   "status, errors). ISO code: en, ru, zh, es, fr, ja, etc. "
                   "Internal artifacts (task fields, journal, code) stay "
                   "English regardless. Default: en.")
@click.option("--session", "session", default=None, metavar="NAME",
              help="canonical session name for the generated coord.yaml "
                   "(default: basename of project_dir). Must match "
                   "[A-Za-z0-9_.-]{1,64}; used as the systemd template "
                   "instance and the tmux session name.")
@click.option("--pre-trust", "pre_trust", is_flag=True, default=False,
              help="pre-accept Claude Code's 'Do you trust this folder?' "
                   "and codex's 'Allow Codex to run' dialogs for this "
                   "project. Writes a single per-project entry into "
                   "~/.claude.json and ~/.codex/config.toml; idempotent; "
                   "never touches other projects' entries. Intended for "
                   "toy / test fleets where TESTER / STAND-KEEPER cannot "
                   "walk dialogs interactively (task 0076).")
def setup(project_dir: Path | None, force: bool, lang: str,
          session: str | None, pre_trust: bool) -> None:
    project_dir = (project_dir or Path.cwd()).resolve()
    project_dir.mkdir(parents=True, exist_ok=True)
    canon = find_canon_dir()
    header(f"greatminds setup: bootstrapping {project_dir}")
    info(f"  canon source: {canon}")

    # project-root config (schema, command_START, role docs — canon-data,
    # kept locally so humans can `cat <project>/DEVELOPER.md` without
    # importing the package).
    header("\nproject-root config:")

    # coord.yaml generation (init-style: never overwrite a user-edited file).
    # The canonical 11-window roster lives in src/greatminds/data/coord.yaml.template;
    # we substitute {SESSION, PROJECT_DIR} into it and write to <project>/coord.yaml.
    coord_yaml_path = project_dir / "coord.yaml"
    resolved_session: str | None = None
    if coord_yaml_path.is_file():
        info(f"  coord.yaml: exists, skipping — delete it first to regenerate")
        # Read the session name from the existing file so we can still register
        # the project with the daemon below (0015 — runs on both gen + skip paths).
        try:
            import yaml as _yaml
            existing = _yaml.safe_load(
                coord_yaml_path.read_text(encoding="utf-8")
            )
            if isinstance(existing, dict):
                v = existing.get("session")
                if isinstance(v, str) and v:
                    resolved_session = v
        except Exception:  # noqa: BLE001 — best-effort, don't block setup
            resolved_session = None
    else:
        # `session is None` → flag omitted, derive default;
        # `session == ""` → user explicitly passed empty, treat as invalid.
        session_name = (
            _default_session_name(project_dir) if session is None else session
        )
        _validate_session_name(session_name)
        template_path = canon / "coord.yaml.template"
        if template_path.is_file():
            tpl = template_path.read_text(encoding="utf-8")
            body = (
                tpl.replace("__SESSION__", session_name)
                   .replace("__PROJECT_DIR__", str(project_dir))
            )
            coord_yaml_path.write_text(body, encoding="utf-8")
            info(f"  coord.yaml: written (session: {session_name})")
            resolved_session = session_name
        else:
            warn("  coord.yaml: template missing in canon, skipping generation")
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
        sl.write_text(_build_settings_local_json(project_dir), encoding="utf-8")
        info("  .claude/settings.local.json: written")
    else:
        info("  .claude/settings.local.json: exists")

    # Codex profile copy (task 0047) — install shipped profiles into
    # ~/.codex/<role>.config.toml so start_agent.py's --profile-v2 path
    # actually finds them. Per-user, idempotent: existing files are NOT
    # overwritten (preserve user customizations).
    copied, skipped = _copy_codex_profiles_if_missing(canon)
    if copied or skipped:
        info(
            f"  codex profiles → ~/.codex/: {copied} copied, "
            f"{skipped} preserved (existing)"
        )

    # Pre-trust install (task 0076) — opt-in. Adds ONE entry for this
    # project's abs path into ~/.claude.json and ~/.codex/config.toml so
    # TESTER / STAND-KEEPER can spawn fresh tool sessions on toy fleets
    # without the trust dialog interrupting. Other projects' trust state
    # is untouched.
    if pre_trust:
        claude_state = _install_claude_pretrust(project_dir)
        codex_state = _install_codex_pretrust(project_dir)
        info(f"  pre-trust → ~/.claude.json: {claude_state}")
        info(f"  pre-trust → ~/.codex/config.toml: {codex_state}")

    # Register project with the daemon (task 0015) — runs on both
    # fresh-gen and skip-existing paths. Graceful degradation: if
    # register_project fails for any reason (no systemd-user yet,
    # registry path not writable, etc.), setup still exits 0 with a
    # warning; the user can finish via `greatminds daemon install`.
    if resolved_session:
        try:
            from greatminds.cli.daemon import register_project
            register_project(resolved_session, project_dir.resolve())
            info(f"  daemon registry: {resolved_session} → {project_dir}")
        except Exception as exc:  # noqa: BLE001 — graceful degrade per plan
            warn(
                f"  daemon registry: could not register "
                f"(greatminds daemon install will fix). reason: {exc}"
            )

    ok("\ndone.")
    info("\nNext:")
    info(f"  1. edit {project_dir}/coord.yaml — confirm project_dir + window list")
    info(f"  2. fill {project_dir}/coordination/PROJECT.md tokens")
    info(f"  3. run: cd {project_dir} && greatminds launch --target tmux")


if __name__ == "__main__":
    setup()
