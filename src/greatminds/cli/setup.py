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
import subprocess
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


def _load_project_env_system_vars_from_canon(canon: Path) -> dict[str, dict]:
    """0274: read ``project_env.system_vars`` from schema.yaml.

    Returns an ordered mapping of var name → metadata
    (``description``, ``acquire_instructions``, ``required``,
    ``usage_locations``). Empty dict if the schema lacks the section
    — setup then writes a header-only PROJECT.env so the file exists
    for users to populate manually.
    """
    import yaml
    schema_path = canon / "schema.yaml"
    if not schema_path.is_file():
        return {}
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    pe = doc.get("project_env") or {}
    sv = pe.get("system_vars") or {}
    out: dict[str, dict] = {}
    for name, meta in sv.items():
        if isinstance(name, str) and isinstance(meta, dict):
            out[name] = meta
    return out


def _wrap_lines(text: str, prefix: str, width: int = 72) -> str:
    """Wrap ``text`` into ``# ``-prefixed lines for .env file comments.

    Multi-line values from schema (YAML block scalars) carry literal
    newlines; we re-flow each paragraph so the .env file reads
    cleanly in a terminal. Empty paragraphs become an empty comment
    line.
    """
    import textwrap
    out_lines: list[str] = []
    for paragraph in text.splitlines():
        para = paragraph.rstrip()
        if not para:
            out_lines.append(prefix.rstrip())
            continue
        wrapped = textwrap.fill(
            para,
            width=width,
            initial_indent=prefix,
            subsequent_indent=prefix,
            break_long_words=False,
            break_on_hyphens=False,
        )
        out_lines.append(wrapped)
    return "\n".join(out_lines)


def _render_project_env_entry(name: str, meta: dict) -> str:
    """Render a single ``KEY=value`` block for PROJECT.env: a
    leading description comment, optional acquire-instructions
    comment, then ``KEY=`` (no default value — user fills in)."""
    lines: list[str] = []
    desc = (meta.get("description") or "").strip()
    if desc:
        lines.append(_wrap_lines(f"{name} — {desc}", "# "))
    acq = (meta.get("acquire_instructions") or "").strip()
    if acq:
        if lines:
            lines.append("#")
        lines.append(_wrap_lines(acq, "#   "))
    if meta.get("required") is True:
        lines.append("# REQUIRED")
    lines.append(f"{name}=")
    return "\n".join(lines)


def _render_project_env_body(system_vars: dict[str, dict]) -> str:
    if not system_vars:
        return "# (no system vars declared in schema.project_env.system_vars)"
    blocks = [_render_project_env_entry(n, m) for n, m in system_vars.items()]
    return "\n\n".join(blocks)


def _render_system_vars_docs(system_vars: dict[str, dict]) -> str:
    """Render the System variables section body for PROJECT.md.

    One ``### NAME`` heading per var + its description from schema +
    a one-liner pointer to PROJECT.env for setup instructions.
    """
    if not system_vars:
        return "_(no system vars declared in schema.project_env.system_vars)_"
    out: list[str] = []
    for name, meta in system_vars.items():
        out.append(f"### {name}")
        desc = (meta.get("description") or "").strip()
        if desc:
            out.append("")
            out.append(desc)
        out.append("")
        out.append(
            "See `coordination/PROJECT.env` for the entry + setup "
            "instructions."
        )
        out.append("")
    return "\n".join(out).rstrip()


def _ensure_project_env(coord: Path, canon: Path, force: bool) -> str:
    """0274: write/refresh ``coordination/PROJECT.env`` from schema.

    Behavior:
    - File missing → write from template + schema. Returns "written".
    - File present + force=True → backup to ``.bak`` then overwrite.
      Returns "overwritten".
    - File present + force=False → leave alone (preserves user's
      filled-in values). Returns "exists".
    """
    target = coord / "PROJECT.env"
    if target.is_file() and not force:
        return "exists"

    tmpl_path = canon / "templates" / "PROJECT.env.template"
    if not tmpl_path.is_file():
        return "template-missing"
    template = tmpl_path.read_text(encoding="utf-8")
    system_vars = _load_project_env_system_vars_from_canon(canon)
    body = _render_project_env_body(system_vars)
    rendered = template.replace("{{SYSTEM_VARS_ENTRIES}}", body)

    status = "written"
    if target.is_file():
        backup = target.with_suffix(".env.bak")
        backup.write_text(target.read_text(encoding="utf-8"),
                          encoding="utf-8")
        status = "overwritten"
    target.write_text(rendered, encoding="utf-8")
    return status


def _ensure_project_md(coord: Path, canon: Path, force: bool,
                        lang: str) -> str:
    """0274: write/refresh ``coordination/PROJECT.md`` from template
    + schema-driven System variables section.

    Behavior mirrors ``_ensure_project_env``: missing → written;
    force=True → overwritten (legacy file preserved as ``.md.bak``);
    force=False → exists.

    ``lang`` is appended as a one-line ``Language: <lang>`` near the
    top of the project-context section so users can edit it later
    without losing the schema-driven sections on re-runs.
    """
    target = coord / "PROJECT.md"
    if target.is_file() and not force:
        return "exists"

    tmpl_path = canon / "templates" / "PROJECT.md.template"
    if not tmpl_path.is_file():
        return "template-missing"
    template = tmpl_path.read_text(encoding="utf-8")
    system_vars = _load_project_env_system_vars_from_canon(canon)
    docs = _render_system_vars_docs(system_vars)
    rendered = template.replace("{{SYSTEM_VARS_DOCS}}", docs)
    # Inject language hint at the bottom (operator can move/edit
    # later; keeps the template surface clean of a `<TOKEN>`).
    rendered = rendered.rstrip() + f"\n\nLanguage: {lang}\n"

    status = "written"
    if target.is_file():
        backup = target.with_suffix(".md.bak")
        backup.write_text(target.read_text(encoding="utf-8"),
                          encoding="utf-8")
        status = "overwritten"
    target.write_text(rendered, encoding="utf-8")
    return status


def _seed_stand_profiles(coord: Path, canon: Path) -> tuple[int, int]:
    """0281 (0276 Phase E): copy canon stand-profile presets into
    ``coord/stand-profiles/``.

    Source: ``<canon>/templates/stand-profiles/*.{yaml,md}``. Target:
    ``coord/stand-profiles/<name>``. Idempotent — files that already
    exist (operator-edited) are NOT overwritten. Returns
    ``(copied, skipped)`` counts for the setup log line.

    Sub-directory creation: if the source dir is missing (a partial
    install / dev build that hasn't been packaged yet), returns
    ``(0, 0)`` silently so setup still succeeds.
    """
    src_dir = canon / "templates" / "stand-profiles"
    if not src_dir.is_dir():
        return (0, 0)
    target_dir = coord / "stand-profiles"
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for src in sorted(src_dir.iterdir()):
        if not src.is_file():
            continue
        if src.suffix not in (".yaml", ".md"):
            continue
        if src.name.startswith("."):
            continue
        dst = target_dir / src.name
        if dst.is_file():
            skipped += 1
            continue
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        copied += 1
    return (copied, skipped)


def _load_claude_settings_allow_from_canon(canon: Path) -> list[str]:
    """0191: read ``claude_settings.permissions.allow`` from schema.yaml.

    Returns the canonical list of Bash allow-rules that /loop claude-
    host roles need (git ops, etc.). Empty list if schema is missing
    the section — callers add nothing but the Stop hook in that case.
    """
    import yaml
    schema_path = canon / "schema.yaml"
    if not schema_path.is_file():
        return []
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    cs = doc.get("claude_settings") or {}
    allow = ((cs.get("permissions") or {}).get("allow") or [])
    return [str(x) for x in allow if isinstance(x, str)]


def _load_claude_settings_auto_mode_from_canon(canon: Path) -> list[str]:
    """0267: read ``claude_settings.auto_mode.allow`` from schema.yaml.

    The classifier's auto-mode ceiling silently blocks several
    commands that ``permissions.allow`` would otherwise permit (the
    motivating case: ``git push origin main``). Schema's
    ``auto_mode.allow`` list maps directly into the file's
    ``autoMode.allow`` key. Defaults to ``["$defaults"]`` if the
    section is missing so the file remains valid for older fleets.
    """
    import yaml
    schema_path = canon / "schema.yaml"
    if not schema_path.is_file():
        return ["$defaults"]
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ["$defaults"]
    cs = doc.get("claude_settings") or {}
    allow = ((cs.get("auto_mode") or {}).get("allow") or [])
    out = [str(x) for x in allow if isinstance(x, str)]
    return out or ["$defaults"]


DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"


def _load_claude_settings_model_from_canon(canon: Path) -> str:
    """0309: read ``claude_settings.model`` from schema.yaml.

    Returns the configured default model id (or
    ``DEFAULT_CLAUDE_MODEL`` when the schema lacks the field). The
    motivating concern was fleet-level model selection: pre-0309
    each fleet inherited claude's machine-level global default,
    which drifted across hosts. Schema is the canonical override.
    """
    import yaml
    schema_path = canon / "schema.yaml"
    if not schema_path.is_file():
        return DEFAULT_CLAUDE_MODEL
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return DEFAULT_CLAUDE_MODEL
    cs = doc.get("claude_settings") or {}
    model = cs.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_CLAUDE_MODEL


def _build_settings_local_json(project_dir: Path,
                                canon: Path | None = None) -> str:
    """Return the JSON text for ``.claude/settings.local.json``.

    Every hook command begins with an absolute reference to greatminds
    (path or ``python -m`` fallback) so the file is portable across
    claude sessions regardless of PATH. The ``permissions.allow`` list
    comes from schema's ``claude_settings:`` section so /loop roles
    can do git ops without operator approval (0191).

    0236: chat-mode roles (PLANNER, MAINTAINER) need an
    UserPromptSubmit hook in addition to Stop. The Stop hook fires
    only at END of turn — pending inbox surfaces AFTER reply, so a
    rapid USER topic-switch hides the inbox between turns. The
    UserPromptSubmit hook fires at START of each USER prompt and
    forces the agent to drain inbox first.
    """
    gm_bin = _greatminds_bin()
    stop_cmd = (
        f'{gm_bin} stop-decide "${{GREATMINDS_ROLE:-UNKNOWN}}" '
        f'--host claude --project-dir {project_dir} --phase stop'
    )
    ups_cmd = (
        f'{gm_bin} stop-decide "${{GREATMINDS_ROLE:-UNKNOWN}}" '
        f'--host claude --project-dir {project_dir} '
        f'--phase user-prompt-submit'
    )
    allow: list[str] = []
    auto_mode_allow: list[str] = ["$defaults"]
    if canon is not None:
        allow = _load_claude_settings_allow_from_canon(canon)
        auto_mode_allow = _load_claude_settings_auto_mode_from_canon(canon)
    settings = {
        # 0309: default claude model so each fleet picks Opus 4.8
        # without depending on machine-level claude global settings.
        # The schema can override via ``claude_settings.model``;
        # operator's manual override in an existing file is preserved
        # by _ensure_claude_settings_local (additive merge).
        "model": _load_claude_settings_model_from_canon(canon)
                  if canon is not None else "claude-opus-4-8",
        "permissions": {"allow": allow},
        # 0267: schema-driven auto_mode raises the classifier ceiling
        # for the specific commands (push origin main, follow-tags)
        # that operators previously had to type with the `!` prefix.
        "autoMode": {"allow": auto_mode_allow},
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": stop_cmd},
                    ],
                },
            ],
            # 0236: close the end-of-turn inbox gap for chat-mode
            # roles (PLANNER / MAINTAINER). Stop-decide --phase=user-
            # prompt-submit checks inbox before each USER turn is
            # processed; only chat-mode roles enforce, loop-mode
            # roles see a no-op (coordd SIGINT already covers them).
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": ups_cmd},
                    ],
                },
            ],
        },
    }
    return json.dumps(settings, indent=2) + "\n"


def _ensure_claude_settings_local(project_dir: Path, canon: Path) -> str:
    """0191: write or extend ``<project>/.claude/settings.local.json``.

    Three cases:
    1. File missing → write from template (Stop hook + schema's allow
       list + ``autoMode.allow: ["$defaults"]``).
    2. File present, valid JSON → union ``permissions.allow`` with
       schema's allow (dedup, preserve operator's existing rules),
       leave ``autoMode``, ``hooks``, and any other top-level keys
       UNTOUCHED. Operator's customizations survive setup re-runs.
    3. File present but unreadable (corrupt JSON, IO error) → leave
       it alone, log "could not parse"; the operator owns the file.

    Returns a one-word status string for the setup summary line:
    ``"written"`` | ``"extended"`` | ``"unchanged"`` | ``"unreadable"``.
    """
    cclaude = project_dir / ".claude"
    cclaude.mkdir(parents=True, exist_ok=True)
    target = cclaude / "settings.local.json"

    if not target.is_file():
        target.write_text(
            _build_settings_local_json(project_dir, canon=canon),
            encoding="utf-8",
        )
        return "written"

    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unreadable"
    if not isinstance(existing, dict):
        return "unreadable"

    canonical_allow = _load_claude_settings_allow_from_canon(canon)
    canonical_auto = _load_claude_settings_auto_mode_from_canon(canon)
    canonical_model = _load_claude_settings_model_from_canon(canon)
    added = False

    # 0309: only set ``model`` when the operator hasn't already
    # written one. Preserves explicit manual override; sets the
    # canon default on legacy files that pre-date 0309.
    if "model" not in existing:
        existing["model"] = canonical_model
        added = True

    if canonical_allow:
        perms = existing.setdefault("permissions", {})
        if not isinstance(perms, dict):
            perms = {}
            existing["permissions"] = perms
        current_allow = perms.get("allow")
        if not isinstance(current_allow, list):
            current_allow = []
        seen = set(str(x) for x in current_allow if isinstance(x, str))
        for rule in canonical_allow:
            if rule not in seen:
                current_allow.append(rule)
                seen.add(rule)
                added = True
        perms["allow"] = current_allow

    # 0267: same additive-merge for auto_mode. Operator's custom
    # entries (e.g. their own '$defaults' tweak or extra Bash globs)
    # are preserved; only the schema's rules not yet present are
    # appended.
    if canonical_auto:
        auto = existing.setdefault("autoMode", {})
        if not isinstance(auto, dict):
            auto = {}
            existing["autoMode"] = auto
        current_auto = auto.get("allow")
        if not isinstance(current_auto, list):
            current_auto = []
        seen_auto = set(str(x) for x in current_auto if isinstance(x, str))
        for rule in canonical_auto:
            if rule not in seen_auto:
                current_auto.append(rule)
                seen_auto.add(rule)
                added = True
        auto["allow"] = current_auto

    if not added:
        return "unchanged"
    target.write_text(
        json.dumps(existing, indent=2) + "\n",
        encoding="utf-8",
    )
    return "extended"


QUEUES = [
    "feature_inbox", "feature_plan", "feature_dev", "feature_ui_dev",
    "feature_docs", "feature_test", "feature_docs_review",
    "feature_review", "feature_blocked", "verified", "archive",
    "user_feedback", "review_sessions",
    # 0247 (1.3.0): stand_requests, stand_wip, stand_done REMOVED.
    # Lease-based singleton stand resource (.stand/state.yaml)
    # replaces them.
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


def _codex_skill_dirs_for_role(canon: Path, role: str) -> list[Path]:
    """0162: enumerate canon SKILL.md directories codex should register
    for ``role``.

    Each returned path is the directory containing a ``SKILL.md`` file
    (codex 0.130's ``skills.config.<index>.path`` semantics). Returned
    in deterministic alphabetical order for stable config.toml output.

    Two layers, in this order:
      * Shared canon plugins (``canon/plugins/coordination-protocol/
        skills/*/SKILL.md``) — installed for every codex role.
      * Per-role plugins (``canon/plugins/role-<role-lower>/skills/*/
        SKILL.md``) — installed only when the dir exists for this role.

    Codex-specific exclusions: skill folders whose name ends in
    ``-claude`` are skipped (claude-host skills don't apply to codex
    agents). Pre-0162 the canon ``coordination-protocol-claude``
    variant was already skipped at the plugin level; this preserves
    that contract.
    """
    out: list[Path] = []
    role_lower = role.lower()
    plugin_dirs = [
        canon / "plugins" / "coordination-protocol",
        canon / "plugins" / f"role-{role_lower}",
    ]
    for pdir in plugin_dirs:
        skills_dir = pdir / "skills"
        if not skills_dir.is_dir():
            continue
        for sd in sorted(skills_dir.iterdir()):
            if not sd.is_dir():
                continue
            if sd.name.endswith("-claude"):
                continue
            if (sd / "SKILL.md").is_file():
                out.append(sd.resolve())
    return out


def _setup_codex_homes_per_role(canon: Path,
                                project_dir: Path) -> tuple[int, int]:
    """0158: install per-role codex homes at
    ``<project>/coordination/.codex-home/<role>/config.toml``.

    Replaces the pre-0158 ``~/.codex/<role>.config.toml`` mechanism that
    codex 0.130.0 silently stopped reading. codex 0.130+ only loads
    ``$CODEX_HOME/config.toml``; ``--profile <role>`` then selects the
    ``[profiles.<role>]`` section within. start_agent.py sets
    ``CODEX_HOME=<project>/coordination/.codex-home/<role>`` at launch.

    0162: after copying the shipped role profile, append
    ``[[skills.config]]`` entries for canon SKILL.md folders so codex
    0.130 registers them at startup. Without this, canon skills (e.g.
    ``coordination-protocol/fsm-mechanics``, ``role-explorer/
    exploratory-probing``) are physically installed in the wheel but
    not visible in the agent's active skills list — auto-invocation by
    description-keyword match cannot fire because codex never
    registered them.

    Per-project, idempotent: an existing per-role ``config.toml`` is
    NOT overwritten — the operator may have customized it. Skill
    entries are appended only on FIRST write; later canon skill bumps
    require operator to delete the per-role home and re-run setup
    (the deliberate trade-off: preserve operator customizations).

    Returns ``(written, skipped)`` for the setup summary.
    """
    src_dir = canon / "codex" / "profiles"
    if not src_dir.is_dir():
        return (0, 0)
    homes_root = project_dir / "coordination" / ".codex-home"
    try:
        homes_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return (0, 0)
    written = 0
    skipped = 0
    for src in sorted(src_dir.glob("*.config.toml")):
        role = src.stem.replace(".config", "")
        role_home = homes_root / role
        try:
            role_home.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        dst = role_home / "config.toml"
        if dst.is_file():
            skipped += 1
            continue
        try:
            shutil.copyfile(src, dst)
            # 0162: append [[skills.config]] entries for canon skill
            # folders. codex 0.130 reads these from config.toml and
            # registers each path's SKILL.md at agent startup.
            skill_dirs = _codex_skill_dirs_for_role(canon, role)
            if skill_dirs:
                with dst.open("a", encoding="utf-8") as f:
                    f.write(
                        "\n\n# 0162: canon skills (SKILL.md folders) "
                        "registered for codex 0.130+ via the\n"
                        "# ``skills.config`` array. Each entry's path "
                        "points at a directory containing\n"
                        "# SKILL.md; codex enumerates them at startup. "
                        "These are operator-owned after\n"
                        "# first write — to pick up new shipped skills, "
                        "delete this file and re-run\n"
                        "# `greatminds setup <project>`.\n"
                    )
                    for sd in skill_dirs:
                        f.write("\n[[skills.config]]\n")
                        f.write(f'path = "{sd}"\n')
                        f.write("enabled = true\n")
            written += 1
        except OSError:
            continue
    return (written, skipped)


def _load_curated_plugins(canon: Path) -> dict:
    """0175: read the curated marketplace plugin list from schema.yaml.

    Returns a dict like::

        {
          "claude_marketplace": {
            "ARCHITECT-PLANNER": ["sourcegraph", "sentry", "huggingface-skills"],
            ...
          },
          "codex_marketplace": {
            "TECHNICAL-WRITER": [],
            ...
          },
        }

    The list lives under ``plugins:`` at schema.yaml's top level. USER
    curated the names (no fabrication — every entry is a real plugin
    on ``anthropics/claude-plugins-official``). Codex side is deferred
    pending a separate USER curation.
    """
    import yaml
    schema_path = canon / "schema.yaml"
    if not schema_path.is_file():
        return {}
    try:
        doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    plugins = doc.get("plugins")
    return plugins if isinstance(plugins, dict) else {}


def _resolve_claude_binary() -> str | None:
    """0203: resolve the ``claude`` binary across npm-install locations.

    Common case: claude is npm-installed under ``~/.local/bin`` or
    ``~/.npm-global/bin``. ssh non-login shells don't source
    ``~/.profile``, so ``~/.local/bin`` isn't on PATH → bare
    ``subprocess.run(["claude", ...])`` fails with FileNotFoundError.
    Resolve via shutil.which first, then fall back to common
    install paths.

    Returns the absolute path (or "claude" if shutil.which found it)
    or None when no executable claude exists anywhere we check.
    """
    p = shutil.which("claude")
    if p:
        return p
    for cand in (
        Path.home() / ".local/bin/claude",
        Path.home() / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _claude_plugin_list_names(claude_bin: str) -> set[str]:
    """0203 iter-2: parse ``claude plugin list`` output into a name set.

    Listing format: ``  ❯ <name>@<marketplace>`` (one per line, ❯ is
    the install-status bullet claude emits). Strip the bullet and
    the ``@<marketplace>`` suffix so the set holds bare names that
    match the curated table entries. Returns empty set on any
    subprocess failure (treated as 'nothing installed')."""
    try:
        listing = subprocess.run(
            [claude_bin, "plugin", "list"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    names: set[str] = set()
    for raw in (listing.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("❯"):  # ❯ U+276F
            line = line[1:].strip()
        head = line.split(maxsplit=1)[0]
        name = head.split("@", 1)[0]
        if name and not name.endswith(":"):  # skip "Installed plugins:" header
            names.add(name)
    return names


def _install_claude_plugins_for_role(
    role: str, plugins: list[str],
    verbose: bool = False,
    *,
    claude_bin: str | None = None,
    pre_campaign_installed: set[str] | None = None,
    installed_this_run: set[str] | None = None,
) -> tuple[int, int, int, int, list[str]]:
    """0175 / 0203: install each curated claude plugin for a role.

    Per-plugin classification (PLANNER §7 amendment to 0203):
    - **fresh-install** — name absent from claude home + absent from
      the in-flight run set; ``claude plugin install`` runs.
    - **preserved-prior** — name was in claude home BEFORE this setup
      campaign (snapshot taken once by the aggregate caller).
    - **preserved-dedupe** — name installed earlier this run for a
      different role (cross-role dedupe within the campaign).
    - **failed** — install rc != 0 or subprocess exception.

    Returns ``(installed_fresh, preserved_prior, preserved_dedupe,
    failed, failed_names)``. Pre-0203-iter-2 the helper conflated
    preserved-prior + preserved-dedupe into one counter, producing
    the «6 installed, 8 preserved» ambiguity on Lattice.

    The aggregate caller (``_install_role_plugins_per_host``) supplies
    a shared ``pre_campaign_installed`` snapshot + mutable
    ``installed_this_run`` set so cross-role dedupe is observable.
    Solo callers (tests) can pass None for both; the function falls
    back to per-role snapshot semantics.
    """
    if not plugins:
        return (0, 0, 0, 0, [])

    # 0203: resolve claude across npm-install locations so ssh non-
    # login invocations (PATH=/usr/bin only) still find it.
    if claude_bin is None:
        claude_bin = _resolve_claude_binary()
    if claude_bin is None:
        warn(
            f"claude binary not found in PATH or common locations "
            f"(~/.local/bin, ~/.npm-global/bin, /usr/local/bin); "
            f"skipping plugin install for {role}. Add claude to "
            f"PATH or install it via `npm install -g @anthropic-ai/"
            f"claude-code` to enable plugin install."
        )
        return (0, 0, 0, len(plugins), list(plugins))

    # Fallback for solo callers / tests: snapshot per-role.
    if pre_campaign_installed is None:
        pre_campaign_installed = _claude_plugin_list_names(claude_bin)
    if installed_this_run is None:
        installed_this_run = set()

    inst_fresh = 0
    pres_prior = 0
    pres_dedupe = 0
    failed = 0
    failed_names: list[str] = []

    for name in plugins:
        if name in pre_campaign_installed:
            pres_prior += 1
            info(
                f"  plugin {name} preserved "
                f"(already in claude home before campaign)"
            )
            continue
        if name in installed_this_run:
            pres_dedupe += 1
            info(
                f"  plugin {name} preserved "
                f"(installed earlier this run for another role)"
            )
            continue
        try:
            cp = subprocess.run(
                [claude_bin, "plugin", "install",
                 f"{name}@claude-plugins-official"],
                capture_output=True, text=True, timeout=60,
            )
            if cp.returncode == 0:
                inst_fresh += 1
                installed_this_run.add(name)
                info(
                    f"  plugin {name} installed via claude plugin install"
                )
            else:
                failed += 1
                failed_names.append(name)
                # 0203: surface stderr regardless of --verbose — silent
                # failures hid the npm-PATH issue for an entire release
                # cycle.
                stderr_excerpt = (cp.stderr or "").strip().splitlines()
                hint = stderr_excerpt[0][:160] if stderr_excerpt else "(no stderr)"
                warn(f"  claude plugin install {name} failed: {hint}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed += 1
            failed_names.append(name)
            warn(f"  claude plugin install {name} errored: {exc}")
    return (inst_fresh, pres_prior, pres_dedupe, failed, failed_names)


def _install_role_plugins_per_host(
    canon: Path,
    verbose: bool = False,
) -> tuple[int, int, int, int, list[str]]:
    """0175 / 0203: install the curated marketplace plugins per role.

    Returns aggregate ``(installed_fresh, preserved_prior,
    preserved_dedupe, failed, failed_names)`` across all roles.
    Splitting ``preserved`` into prior (already in claude home before
    this campaign) and dedupe (installed earlier this run for another
    role) closes the «6 installed, 8 preserved» ambiguity flagged on
    Lattice (PLANNER §7 amendment to 0203).
    """
    # Opt-out for tests + CI hosts that don't want setup mutating the
    # host's claude plugin registry. Production setup leaves this
    # unset; the suite's conftest.py sets it so integration tests
    # don't shell out to the real ``claude`` binary.
    if os.environ.get("GREATMINDS_SKIP_PLUGIN_INSTALL"):
        return (0, 0, 0, 0, [])
    curated = _load_curated_plugins(canon)
    if not curated:
        return (0, 0, 0, 0, [])

    # 0203 iter-2: resolve claude once + snapshot pre-campaign state
    # once so the per-role helper can distinguish "preserved (already
    # there)" from "preserved (we installed it a moment ago for
    # another role)".
    claude_bin = _resolve_claude_binary()
    if claude_bin is None:
        # Per-role helper handles the no-binary case (all-failed with
        # named plugins). Pass through; nothing else to snapshot.
        pre_campaign_installed: set[str] = set()
    else:
        pre_campaign_installed = _claude_plugin_list_names(claude_bin)
    installed_this_run: set[str] = set()

    total_inst_fresh = 0
    total_pres_prior = 0
    total_pres_dedupe = 0
    total_failed = 0
    all_failed_names: list[str] = []

    claude_table = curated.get("claude_marketplace") or {}
    for role, plugins in claude_table.items():
        if not plugins:
            continue
        ci, pp, pd, cf, cfn = _install_claude_plugins_for_role(
            role, list(plugins), verbose=verbose,
            claude_bin=claude_bin,
            pre_campaign_installed=pre_campaign_installed,
            installed_this_run=installed_this_run,
        )
        if verbose:
            print(
                f"  claude plugins for {role}: "
                f"{ci} fresh, {pp} pre-existing, {pd} dedupe, "
                f"{cf} failed",
            )
        total_inst_fresh += ci
        total_pres_prior += pp
        total_pres_dedupe += pd
        total_failed += cf
        all_failed_names.extend(cfn)

    codex_table = curated.get("codex_marketplace") or {}
    deferred = [r for r, p in codex_table.items()
                if isinstance(p, list) and not p]
    if verbose and deferred:
        print(
            f"  codex plugins deferred for {len(deferred)} role(s): "
            f"{', '.join(sorted(deferred))} (awaiting USER curation)",
        )

    return (total_inst_fresh, total_pres_prior, total_pres_dedupe,
            total_failed, all_failed_names)


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


def _install_git_pre_commit_hook(project_dir: Path) -> str:
    """Write .git/hooks/pre-commit that invokes
    ``greatminds check-git-permission commit`` so only roles listed in
    schema.git_permissions.commit can produce a successful commit.

    Returns a status string ("written" / "existing" / "skipped: <reason>").
    Idempotent: never overwrites an existing hook (preserves user
    customizations); never installs if .git/ is absent.
    Task 0091 item 2.
    """
    git_dir = project_dir / ".git"
    if not git_dir.is_dir():
        info("  git pre-commit hook: skipped (no .git/ in project)")
        return "skipped: no .git/"
    hooks_dir = git_dir / "hooks"
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn(f"  git pre-commit hook: skipped: hooks dir unwritable ({exc})")
        return f"skipped: {exc}"
    hook_path = hooks_dir / "pre-commit"
    if hook_path.exists():
        info("  git pre-commit hook: existing (not overwritten)")
        return "existing"
    # Pin to the interpreter that ran setup (sys.executable). Git's
    # pre-commit hook runs with a sanitized PATH that often lacks the
    # project .venv/bin, so a bare `greatminds` resolves to nothing
    # and the hook errors out for the wrong reason (everyone refused,
    # not just non-allowed roles). Using the absolute python with
    # `-m greatminds.cli.main` guarantees the same install that ran
    # setup is invoked, independent of PATH.
    import shlex
    python_path = shlex.quote(sys.executable)
    body = (
        "#!/usr/bin/env bash\n"
        "# Installed by `greatminds setup` (task 0091 item 2).\n"
        "# Refuses commits when $GREATMINDS_ROLE is not in\n"
        "# schema.yaml `git_permissions.commit` allow-list.\n"
        f"exec {python_path} -m greatminds.cli.main "
        "check-git-permission commit\n"
    )
    try:
        hook_path.write_text(body, encoding="utf-8")
        hook_path.chmod(0o755)
    except OSError as exc:
        warn(f"  git pre-commit hook: skipped: write failed ({exc})")
        return f"skipped: {exc}"
    info(f"  git pre-commit hook: written ({hook_path})")
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
    # 0274: PROJECT.md is generated from the template with the
    # schema-driven System variables section interpolated.
    md_status = _ensure_project_md(coord, canon, force, lang)
    info(f"  PROJECT.md: {md_status} (lang={lang})")

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

    # 0185: ensure project-root .gitignore excludes the worktree base.
    # ``.worktrees/`` is git-internal state (one .git linked-worktree
    # per task) and must NOT be staged. Idempotent: only appends if
    # absent.
    root_gi = project_dir / ".gitignore"
    needed_line = ".worktrees/"
    existing = (
        root_gi.read_text(encoding="utf-8") if root_gi.is_file() else ""
    )
    if needed_line not in existing.splitlines():
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        root_gi.write_text(
            existing + suffix +
            "\n# 0185: per-task worktrees\n" + needed_line + "\n",
            encoding="utf-8",
        )
        info("  project .gitignore: appended .worktrees/")

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

    # 0274: PROJECT.env is generated directly (no longer a
    # ``.example`` step). Contents come from
    # ``schema.project_env.system_vars`` — pre-populated KEY= lines
    # with description / acquire-instructions comments per var.
    env_status = _ensure_project_env(coord, canon, force)
    info(f"  PROJECT.env: {env_status}")

    # 0281 (0276 Phase E): seed the per-project stand-profiles dir
    # with the canonical presets (full-deploy / smoke-only, both
    # yaml + md). Idempotent — existing operator-edited copies are
    # NOT overwritten.
    sp_copied, sp_skipped = _seed_stand_profiles(coord, canon)
    info(f"  stand-profiles: {sp_copied} copied, {sp_skipped} exist")

    # .claude/settings.local.json — Stop hook + schema's claude_settings
    # permissions.allow rules (0191). Merge-on-existing preserves any
    # operator-added rules + hook entries.
    status = _ensure_claude_settings_local(project_dir, canon)
    info(f"  .claude/settings.local.json: {status}")

    # Codex per-role homes (task 0158, supersedes 0047) — install
    # shipped profiles into ``<project>/coordination/.codex-home/<role>/
    # config.toml``. codex 0.130+ reads ``$CODEX_HOME/config.toml`` and
    # selects ``[profiles.<role>]`` within it; the previous
    # ``~/.codex/<role>.config.toml`` location is no longer read by
    # codex. start_agent.py sets ``CODEX_HOME`` per role at launch.
    # Per-project, idempotent: existing per-role config.toml is NOT
    # overwritten.
    written, skipped = _setup_codex_homes_per_role(canon, project_dir)
    if written or skipped:
        info(
            f"  codex per-role homes → coordination/.codex-home/: "
            f"{written} written, {skipped} preserved (existing)"
        )

    # 0175: install curated marketplace plugins per claude-host role.
    # The list lives in schema.yaml's ``plugins.claude_marketplace``
    # table (USER-curated names verified against
    # anthropics/claude-plugins-official). Codex side deferred per
    # USER directive — empty lists log deferral, no install calls.
    # Idempotent: per-plugin presence in ``claude plugin list`` skips
    # the install. Per-plugin failure is non-fatal (log + continue).
    (plugins_inst_fresh, plugins_pres_prior, plugins_pres_dedupe,
     plugins_failed, plugins_failed_names) = (
        _install_role_plugins_per_host(canon, verbose=False)
    )
    plugins_pres_total = plugins_pres_prior + plugins_pres_dedupe
    if (plugins_inst_fresh or plugins_pres_total or plugins_failed):
        # 0203 iter-2 (PLANNER §7): break out preserved into prior
        # (pre-campaign) vs dedupe (this-run), closing the «6/8»
        # ambiguity on multi-role campaigns.
        failed_suffix = (
            f": {', '.join(sorted(set(plugins_failed_names)))}"
            if plugins_failed_names else ""
        )
        info(
            f"  marketplace plugins: {plugins_inst_fresh} installed/"
            f"{plugins_pres_prior} pre-existing/"
            f"{plugins_pres_dedupe} dedupe-this-run/"
            f"{plugins_failed} failed"
            f"{failed_suffix} (see schema.yaml plugins:)"
        )

    # Git pre-commit hook (task 0091 item 2) — installs only if a
    # .git/ directory exists in the project root (gracefully no-ops on
    # non-git projects). Idempotent: if a hook already exists at the
    # path, we don't overwrite (preserves user customizations).
    _install_git_pre_commit_hook(project_dir)

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

    # 0280 (0276 Phase D): sanity check ansible-playbook is on PATH.
    # ansible-core is now a hard dependency (pyproject.toml) so a
    # successful pip install should provide it; this check surfaces
    # broken installs early. Warn-only — YAML stand profiles need
    # it; MD profiles still work without.
    _check_ansible_playbook_available()

    ok("\ndone.")
    info("\nNext:")
    info(f"  1. edit {project_dir}/coord.yaml — confirm project_dir + window list")
    info(f"  2. fill {project_dir}/coordination/PROJECT.md tokens")
    info(f"  3. run: cd {project_dir} && greatminds launch --target tmux")


def _check_ansible_playbook_available() -> None:
    """0280: warn (not error) if ``ansible-playbook`` is absent.

    YAML stand-profiles (Phase C / cli/stand_executor.py) require it;
    MD-format profiles continue to work without ansible. We emit a
    visible warning instead of aborting setup so an operator without
    ansible can still bootstrap a fleet for MD-only workflows.
    """
    found = shutil.which("ansible-playbook")
    if not found:
        warn(
            "  ansible-playbook NOT on PATH — YAML stand-profile "
            "execution (Phase C) will fail. ansible-core is declared "
            "as a hard dep; if you just ran `pip install greatminds`, "
            "verify the venv: `pip show ansible-core` should list it. "
            "MD-format profiles still work without ansible."
        )
        return

    # Mostly cosmetic: capture --version output for the setup log so
    # operators see the exact version + interpreter at install time.
    try:
        cp = subprocess.run(
            [found, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            first = (cp.stdout or "").splitlines()[:1]
            info(f"  ansible-playbook: {first[0] if first else found}")
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    warn(
        f"  ansible-playbook present at {found} but --version "
        "failed; YAML profile execution may be flaky."
    )


if __name__ == "__main__":
    setup()
