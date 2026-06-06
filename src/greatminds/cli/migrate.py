"""``greatminds migrate`` — bring a project's coordination config up to the
current greatminds version.

``greatminds update`` bumps the PACKAGE; this brings the PROJECT's on-disk
config to the new model so the two don't drift:

  1. canon refresh — re-run ``setup`` (overwrites schema.yaml / COORDINATE.md
     / bootstrap.md, creates any missing queues e.g. feature_live, refreshes
     coordination/.gitignore). Never touches PROJECT.md or coord.yaml.
  2. coord.yaml migration — an old all-paned coord.yaml (every role gets a
     tmux window) is migrated to the current driven model (workers run as
     paneless coordd subprocesses; only planner/maintainer/dashboard/live are
     paned). The old file is backed up; session, project_dir, the
     per-project ``worktrees`` override, and any custom role-less windows are
     preserved.
  3. legacy artifact removal — files deleted in newer versions
     (command_START.yaml, the per-role ``<ROLE>.md`` docs, the bot_* queues)
     are removed so they stop confusing agents.

Standalone (``greatminds migrate``) for fleets already on the new package
but with stale config (the version-already-current case ``update`` skips),
and called automatically by ``greatminds update``.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
import yaml

from greatminds.cli._colors import err, info, ok, warn
from greatminds.core.paths import find_canon_dir


# Per-role canon docs + stream artifacts deleted in 1.5.0 (role contract
# moved into schema.roles; bot stream removed). Removed by migration.
_LEGACY_ROOT_FILES = [
    "command_START.yaml",
    "BOT_STREAM_DIVERGENCE.md",
    "ARCHITECT-PLANNER.md", "ARCHITECT-REVIEWER.md", "DEVELOPER.md",
    "UI-DEVELOPER.md", "TECHNICAL-WRITER.md", "TESTER.md", "READER.md",
    "EXPLORER.md", "STAND-KEEPER.md", "MAINTAINER.md", "USER.md",
    "BOT-DEVELOPER.md", "BOT-USER.md",
]
_LEGACY_BOT_QUEUES = [
    "bot_inbox", "bot_wip", "bot_done", "bot_verified", "bot_archive",
]


def _canonical_windows(session: str, project_dir: str) -> list[dict]:
    """The current window roster from the canon coord.yaml.template."""
    tmpl = find_canon_dir() / "coord.yaml.template"
    body = (tmpl.read_text(encoding="utf-8")
            .replace("__SESSION__", session)
            .replace("__PROJECT_DIR__", project_dir))
    doc = yaml.safe_load(body) or {}
    return list(doc.get("windows") or [])


def migrate_coord_yaml(project_dir: Path) -> tuple[str, str]:
    """Migrate an old all-paned coord.yaml to the current driven model.

    Returns ``(status, detail)`` where status is one of ``no-file`` /
    ``already-current`` / ``migrated``. Idempotent: a coord.yaml that
    already has any ``mode: driven`` window is treated as current.
    """
    coord_yaml = project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        return ("no-file", "no coord.yaml (setup generates a fresh one)")
    try:
        old = yaml.safe_load(coord_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return ("error", f"could not parse coord.yaml: {exc}")
    old_windows = old.get("windows") or []
    if any(isinstance(w, dict) and (w.get("mode") or "").lower() == "driven"
           for w in old_windows):
        return ("already-current", "coord.yaml already uses the driven model")

    session = str(old.get("session") or project_dir.name)
    proj = str(old.get("project_dir") or project_dir)
    canonical = _canonical_windows(session, proj)
    canonical_names = {(w.get("name") or "") for w in canonical}
    # Preserve custom role-less windows (e.g. ``ops``) not in the canon set.
    preserved = [
        w for w in old_windows
        if isinstance(w, dict) and not (w.get("role") or "").strip()
        and (w.get("name") or "") not in canonical_names
    ]

    new_doc: dict = {"session": session, "project_dir": proj}
    # Preserve the per-project worktrees override (default_branch etc.).
    if isinstance(old.get("worktrees"), dict):
        new_doc["worktrees"] = old["worktrees"]
    new_doc["windows"] = canonical + preserved

    backup = project_dir / "coord.yaml.premigrate.bak"
    backup.write_text(coord_yaml.read_text(encoding="utf-8"), encoding="utf-8")
    coord_yaml.write_text(
        yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    detail = (f"{len(old_windows)} paned → {len(canonical)} canon "
              f"+ {len(preserved)} preserved; backup {backup.name}")
    return ("migrated", detail)


# Roles removed in a newer version — their coord.yaml windows must be
# stripped on migration, even from an already-driven coord.yaml (which
# migrate_coord_yaml leaves untouched). 1.6.0 retired STAND-KEEPER (coordd
# now deploys the stand itself).
RETIRED_ROLES = {"STAND-KEEPER"}


def strip_retired_role_windows(project_dir: Path) -> list[str]:
    """Remove coord.yaml windows whose role was retired (e.g.
    STAND-KEEPER in 1.6.0). Runs on ANY coord.yaml — including the
    already-driven fleets that ``migrate_coord_yaml`` skips — so an
    ``update`` of an existing 1.5.x fleet drops the stand-keeper pane
    instead of launching an agent for a role the schema no longer has.
    Returns the list of removed role names."""
    coord_yaml = project_dir / "coord.yaml"
    if not coord_yaml.is_file():
        return []
    try:
        doc = yaml.safe_load(coord_yaml.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    windows = doc.get("windows") or []
    kept, removed = [], []
    for w in windows:
        role = (w.get("role") or "").strip().upper() if isinstance(w, dict) \
            else ""
        if role in RETIRED_ROLES:
            removed.append(role)
        else:
            kept.append(w)
    if not removed:
        return []
    doc["windows"] = kept
    coord_yaml.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    return removed


def remove_legacy_artifacts(project_dir: Path) -> list[str]:
    """Remove files/queues deleted in newer versions. Returns removed paths.

    Conservative: only exact known canon filenames + EMPTY bot_* queues
    (a non-empty bot queue is left alone + reported, never silently
    dropping work). Removals are git-recoverable in tracked projects.
    """
    removed: list[str] = []
    for name in _LEGACY_ROOT_FILES:
        p = project_dir / name
        if p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except OSError as exc:
                warn(f"    could not remove {name}: {exc}")
    coord = project_dir / "coordination"
    for q in _LEGACY_BOT_QUEUES:
        qd = coord / q
        if qd.is_dir():
            contents = [f for f in qd.iterdir()
                        if f.name not in (".gitkeep",)]
            if contents:
                warn(f"    bot queue {q} not empty ({len(contents)} files) — "
                     "left in place, inspect before removing")
                continue
            try:
                import shutil
                shutil.rmtree(qd)
                removed.append(f"coordination/{q}/")
            except OSError as exc:
                warn(f"    could not remove {q}: {exc}")
    return removed


def run_migration(project_dir: Path, run_setup: bool = True) -> None:
    """The full project migration (used by ``migrate`` + ``update``)."""
    if run_setup:
        info("==> canon refresh (greatminds setup)...")
        gm = __import__("shutil").which("greatminds")
        cmd = ([gm] if gm else [sys.executable, "-m", "greatminds.cli.main"]) \
            + ["setup", "--project-dir", str(project_dir)]
        cp = subprocess.run(cmd, capture_output=True, text=True)
        if cp.returncode != 0:
            err("setup failed during migration:")
            click.echo(cp.stderr or cp.stdout, nl=False, err=True)
            raise click.exceptions.Exit(cp.returncode)
        ok("    ✓ canon refreshed (schema / COORDINATE / bootstrap / queues / gitignore)")

    info("==> migrating coord.yaml to the current window model...")
    status, detail = migrate_coord_yaml(project_dir)
    {"migrated": ok, "already-current": info, "no-file": info,
     "error": err}.get(status, info)(f"    {status}: {detail}")

    # 1.6.0: drop windows for retired roles (STAND-KEEPER) even from an
    # already-driven coord.yaml — the deploy moved into coordd.
    retired = strip_retired_role_windows(project_dir)
    if retired:
        ok(f"    ✓ removed retired-role window(s): {', '.join(retired)}")

    info("==> removing legacy artifacts (deleted in newer versions)...")
    removed = remove_legacy_artifacts(project_dir)
    if removed:
        ok(f"    ✓ removed {len(removed)}: {', '.join(removed)}")
    else:
        info("    none")

    # 0367: existing fleets keep stale seeded stand profiles because setup
    # never overwrites them; the pre-add_host topology matches zero hosts
    # (vacuous deploy). Refresh pristine seeded copies to the current
    # add_host/STAND_HOST template, leaving operator-customized profiles
    # alone. setup does NOT do this (it stays strictly additive) — the
    # reseed only runs on the deliberate migrate/update path.
    info("==> migrating stale seeded stand profiles to add_host topology...")
    from greatminds.cli.setup import reseed_stale_stand_profiles
    sp = reseed_stale_stand_profiles(project_dir / "coordination", find_canon_dir())
    if sp["reseeded"]:
        ok(f"    ✓ reseeded {len(sp['reseeded'])} stale profile(s): "
           f"{', '.join(sp['reseeded'])} "
           "(old bytes backed up under stand-profiles/.backups/)")
    if sp["customized"]:
        warn("    customized stale profile(s) left in place — migrate by "
             f"hand: {', '.join(sp['customized'])}")
    if sp["missing_template"]:
        warn("    template missing for: "
             f"{', '.join(sp['missing_template'])} (partial build?)")
    if not any((sp["reseeded"], sp["customized"], sp["missing_template"])):
        info("    none stale (profiles already current or operator-owned)")


@click.command(name="migrate",
               short_help="migrate project config to the current greatminds version")
@click.option("--project-dir", default=None,
              help="project root (default: cwd).")
@click.option("--no-setup", is_flag=True, default=False,
              help="skip the canon refresh (setup) step.")
def migrate(project_dir: str | None, no_setup: bool) -> None:
    """Bring a project's coord.yaml / canon / queues up to the installed
    greatminds version (coord.yaml driven-model migration, canon refresh,
    legacy-artifact removal). Idempotent; backs up coord.yaml."""
    pd = Path(project_dir).resolve() if project_dir else Path.cwd()
    run_migration(pd, run_setup=not no_setup)
    ok("==> migration complete")
