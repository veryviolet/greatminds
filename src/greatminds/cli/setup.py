"""Initialize a project with ACP manifests, role bindings and runtime queues."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import click

from greatminds.core.paths import (
    find_canon_dir,
)
from greatminds.cli._colors import err, warn


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
            "See `.greatminds/PROJECT.env` for the entry + setup "
            "instructions."
        )
        out.append("")
    return "\n".join(out).rstrip()


def _ensure_project_env(runtime: Path, canon: Path, force: bool) -> str:
    """Write/refresh ``.greatminds/PROJECT.env`` from schema.

    Behavior:
    - File missing → write from template + schema. Returns "written".
    - File present + force=True → backup to ``.bak`` then overwrite.
      Returns "overwritten".
    - File present + force=False → leave alone (preserves user's
      filled-in values). Returns "exists".
    """
    target = runtime / "PROJECT.env"
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
    """Write/refresh ``coordination/PROJECT.md`` from template
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


def _seed_bootstrap(coord: Path, canon: Path) -> bool:
    """Seed the single static system-prompt ``.greatminds/bootstrap.md``
    from canon (``greatminds.data/bootstrap.md``), overwriting on each
    setup so it tracks canon. Role-independent: every agent reads its own
    contract from ``schema.roles.<GREATMINDS_ROLE>``. The driven driver
    and start-agent pass this file to claude's
    ``--append-system-prompt-file`` / codex ``baseInstructions``."""
    src = canon / "bootstrap.md"
    if not src.is_file():
        return False
    coord.mkdir(parents=True, exist_ok=True)
    (coord / "bootstrap.md").write_text(
        src.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def _remove_root_canon_copy(project_dir: Path, coord: Path, name: str) -> str:
    """Remove root-level canon docs after the runtime copy exists.

    If the root copy matches the runtime copy, delete it. If it differs,
    preserve it under ``.greatminds/.backups/`` and still remove the root copy
    so project roots stay clean.
    """
    root_file = project_dir / name
    if not root_file.is_file():
        return "absent"
    coord_file = coord / name
    coord.mkdir(parents=True, exist_ok=True)
    try:
        root_text = root_file.read_text(encoding="utf-8")
    except OSError as exc:
        return f"left in root: {exc}"
    if not coord_file.is_file():
        try:
            coord_file.write_text(root_text, encoding="utf-8")
            root_file.unlink()
        except OSError as exc:
            return f"left in root: {exc}"
        return "moved to runtime"
    try:
        coord_text = coord_file.read_text(encoding="utf-8")
    except OSError as exc:
        return f"left in root: {exc}"
    if root_text != coord_text:
        backups = coord / ".backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"{name}.root-legacy.bak"
        i = 1
        while backup.exists():
            backup = backups / f"{name}.root-legacy.{i}.bak"
            i += 1
        backup.write_text(root_text, encoding="utf-8")
    try:
        root_file.unlink()
    except OSError as exc:
        return f"left in root: {exc}"
    return "removed" if root_text == coord_text else f"moved to {backup}"


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


def _seed_stand_profile_registry(coord: Path, canon: Path) -> str:
    src = canon / "templates" / "stand-profiles.yaml"
    dst = coord / "stand-profiles.yaml"
    if dst.is_file():
        return "exists"
    if not src.is_file():
        return "MISSING in canon"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return "written"


# 0367 (follow-up to 0366/#16/#17): sha256 of every greatminds-SHIPPED
# stand-profile version that predates the add_host / stand_nodes host
# topology (the "host-agnostic deploy" rewrite). These legacy templates
# target ``hosts: stand`` / ``hosts: "${STAND_HOST}"`` with neither an
# add_host bootstrap play nor a static inventory, so the host-agnostic
# executor matches zero hosts and deploys nothing (the vacuous-deploy
# trap). A ``coordination/stand-profiles/<name>`` whose content hashes to
# one of these is a PRISTINE seeded copy of a stale template — the
# operator never edited it — and is therefore SAFE to refresh in place to
# the current packaged template. Anything NOT in this set is treated as
# operator-customized and left untouched. Conservative by construction:
# reseed only ever overwrites bytes greatminds itself once shipped.
_STALE_SHIPPED_PROFILE_HASHES: dict[str, frozenset[str]] = {
    "full-deploy.yaml": frozenset({
        "5311122bc5ecb87e21c8e78ace2697c6d3f752f2416cb602b99f86f2164decc3",
        "53786e4c3435632d01824d091f02b5f3d3761bc3d75ca9ce1a14c47810ad2868",
        "6dcef9fe54c3c8d39c8cbb162ad89cc58bf90f17eeb5895f5b9d5dbed64bb5a7",
        "97c86774284e15038c6a4062097dc4e6900209adabe1005f5f0bc86a7306e9c1",
        "a9b38887161732aef1fedfffbff3e0c0564358f7f801f4f617b009f1cd25dbce",
        "aefc89661d2e6b01fb74b1ff4a3c5460ebcc5a63cc4bfa2d7082659e1afadaa8",
        "bf44b2f97f73e3484de36618a8df936775fa992c88c1e7b961014a59305b80e3",
        "e0e4db62dad30d3f604f8d31281c371727020e21f5f08b540c813734e3408761",
        "e84f5c470b1de176515a081fa2cb32cff6309834cad720a1b5278d9c928167f1",
        # 1.6.x add_host rewrite that DROPPED the uv PATH environment block
        # (task 0372 regression). Pristine on disk it sniffs as "current"
        # (it has add_host), so it must be listed here for the stale-hash
        # check to reseed it to the PATH-restored template.
        "bb94e88c9c18dc3c6d0c5ffb7d3f4e9c5a9d9412a859369793a9169e78b04f33",
        # PATH-restored template that still rsyncs `.git` (no --exclude=.git).
        # A per-task worktree's .git is a gitdir FILE; a stale destination
        # .git DIRECTORY makes rsync abort with exit 23 → stand DOWN
        # (task 0382). Pristine on disk it sniffs as "current" (add_host +
        # uv PATH), so list it here to reseed to the .git-excluding template.
        "30469cc65b0cb58e95e590d62746baf4ae18b6dd2ea7aa22ff038c8b089ceb83",
        # 0382 template after adding --exclude=.git but before 0394's
        # pre-sync remote cleanup. Excluding .git stops future copies but
        # leaves a stale destination `.git` pointer in place forever; reseed
        # pristine copies to the template that removes deploy_path/.git first.
        "4ca039ad80125011cb331249c3fb9baab086b26c630751f60457575bd91355c0",
        # 2.0.1 template before local-loopback hosts were registered with
        # ansible_connection=local and before local rsync avoided the
        # localhost SSH known_hosts trap.
        "551f9f76a83fb53f9318ab0f5684ad740012236cb64cb5a10cdbc56c69f669ea",
    }),
    "smoke-only.yaml": frozenset({
        "4342d1565f86de3d81569174f315e32f52bc95858a325b0585096637f42f1123",
        "45f85a0f1d1cabe4cc4257c3a96d55a3dc4234a15f0bd7dd321d6b0364435f0f",
        "82efeb03acb5462964d6ee8f1da38a27483a42fcbc08fe4a95545bd0cf06f267",
        # 1.6.x add_host rewrite without the uv PATH block (task 0372).
        "50e719ab502fed73b2fc0dd9ac4bad3603202877c09cd5bba1ef4e6c5bf09f86",
        # 2.0.1 template before local-loopback hosts were registered with
        # ansible_connection=local.
        "69adde1c838155dc0455790d59eef7556be78ac54892959541a78a5c0f9c3ff0",
    }),
    "vite-dev.yaml": frozenset({
        "077e70f2905520baf92020e725db34ebc4e9b5723ad22f7cb22f07af68b9eb70",
        "13db5f9415c9bb237b5a73b07a06c6a6d5023c2957072540ea03abfdf4cfc568",
        "5077f9ef7fb7a3354d8573835f25f015da69b12368a679eb564ddaa098a4265c",
        "f7baec9084a195cbc526ff7a3a09ee98a248e88159a78869e2e67b4df7525d88",
        # 2.0.1 template before local-loopback hosts were registered with
        # ansible_connection=local and local rsync avoided SSH.
        "a94e77a9c4ae4a766bbc5e98e6960f2369af46ca018372f9149a205037bff64a",
    }),
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _profile_uses_add_host(text: str) -> bool:
    """A stand profile carries the current (post-rewrite) host topology
    when it registers the stand node(s) via ``add_host`` into the
    ``stand_nodes`` group. Both markers must be present to count as
    migrated (a profile that merely mentions one in a comment does not)."""
    return "add_host" in text and "stand_nodes" in text


def reseed_stale_stand_profiles(coord: Path, canon: Path) -> dict[str, list[str]]:
    """0367: refresh pristine-but-stale seeded stand profiles to the
    current add_host / STAND_HOST topology, without clobbering
    operator-customized profiles.

    For each packaged profile name present in
    ``coord/stand-profiles/``, classify the on-disk copy:

    * ``current``     — already byte-identical to the packaged template,
                        or carries the add_host topology under a hash that
                        matches no shipped version (operator's own migrated
                        variant). Left alone.
    * ``reseeded``    — content hashes to a known stale SHIPPED version
                        (pristine seeded copy) that is not the current
                        template — including a shipped add_host variant
                        that dropped a later template fix. The old bytes
                        are backed up to ``stand-profiles/.backups/<name>``
                        and the file is refreshed from the current template.
    * ``customized``  — looks stale (no add_host) but its content matches
                        no shipped version: an operator edit. Left in
                        place and reported so a human can migrate it.
    * ``missing_template`` — name we know but the canon template is gone
                        (partial/dev build). Left alone.

    Idempotent: a second run sees ``current`` and does nothing. Returns
    a dict of name-lists keyed by classification for the caller to log.
    """
    result: dict[str, list[str]] = {
        "reseeded": [], "current": [], "customized": [], "missing_template": [],
    }
    target_dir = coord / "stand-profiles"
    src_dir = canon / "templates" / "stand-profiles"
    if not target_dir.is_dir():
        return result
    backup_dir = target_dir / ".backups"
    for name, stale_hashes in _STALE_SHIPPED_PROFILE_HASHES.items():
        dst = target_dir / name
        if not dst.is_file():
            continue  # this profile was never seeded here
        src = src_dir / name
        if not src.is_file():
            result["missing_template"].append(name)
            continue
        try:
            on_disk = dst.read_text(encoding="utf-8")
            template = src.read_text(encoding="utf-8")
        except OSError as exc:
            warn(f"    could not read profile {name}: {exc}")
            continue
        # Order matters: a PRISTINE shipped profile is reseeded even when it
        # already carries the add_host topology (e.g. the 1.6.x rewrite that
        # dropped the uv PATH block — task 0372). Only an UNKNOWN-hash
        # add_host profile (an operator's own migrated variant) counts as
        # current via the add_host shortcut, so we never clobber operator
        # edits while still propagating template fixes to seeded copies.
        if on_disk == template:
            result["current"].append(name)
        elif _sha256(on_disk) in stale_hashes:
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / name).write_text(on_disk, encoding="utf-8")
            dst.write_text(template, encoding="utf-8")
            result["reseeded"].append(name)
        elif _profile_uses_add_host(on_disk):
            result["current"].append(name)
        else:
            result["customized"].append(name)
    return result


# 0310: reverted 0309's model-write feature. ``greatminds setup``
# no longer pins a claude model into settings.local.json — model
# selection belongs to the machine-level ``~/.claude/settings.json``
# or the interactive ``/model`` command. Hardcoding it in project
# settings created unwanted coupling and blocked per-machine choice.
# The ``DEFAULT_CLAUDE_MODEL`` constant + the
# ``_load_claude_settings_model_from_canon`` helper from 0309 are
# removed.


# Queue directories created on setup. MUST stay in sync with the
# task-holding queues in schema.queues (kind active/parking/terminal;
# the state-kind `.stand` is NOT a created dir). feature_live was added
# with LIVE-DEVELOPER in 1.5.0 but this list wasn't updated, so fresh
# setups silently lacked the queue — test_setup_queues_match_schema
# now pins this against the schema to prevent recurrence.
QUEUES = [
    "feature_inbox", "feature_plan", "feature_dev", "feature_ui_dev",
    "feature_docs", "feature_test", "feature_docs_review",
    "feature_live", "feature_review", "feature_blocked",
    "verified", "archive", "user_feedback", "review_sessions",
]

ROLES_LOWER = [
    "architect-planner", "architect-reviewer", "developer", "ui-developer",
    "technical-writer", "tester", "reader", "explorer",
    "user", "maintainer",
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


@click.command(short_help="initialize an ACP project")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--execution-config", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Explicit ACP manifests and role bindings; otherwise create an empty contract.")
def setup(project_dir: Path | None, execution_config: Path | None = None) -> None:
    from greatminds.runtime.bootstrap import bootstrap
    import json
    project = (project_dir or Path.cwd()).resolve()
    result = bootstrap(project, execution_config)
    result['next_step'] = 'Configure agents and bindings in coordination/execution.yaml, then run greatminds coordd.'
    click.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    setup()
