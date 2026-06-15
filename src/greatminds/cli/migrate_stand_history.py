#!/usr/bin/env python3
"""greatminds migrate-stand-history — one-shot move of legacy
``coordination/stand_done/*`` evidence files into the new archive
path ``coordination/archive/stand-history/``.

0247 (1.3.0 BREAKING) removed the ``stand_requests`` / ``stand_wip``
/ ``stand_done`` queue model in favour of the lease-based singleton
stand resource (``.stand/state.yaml``). The runtime stopped creating
these dirs in fresh setups, but every existing fleet still carries
their historical contents — typically dozens of ``stand_done/<id>.{yaml,md}``
evidence files from past deploys. This command moves those files,
preserving filename + content, so the legacy directory can finally
be deleted with no data loss.

Idempotent: missing source dir → "nothing to do"; collision in the
target dir → skipped (existing file wins, source kept for manual
review). ``--dry-run`` reports the planned moves without touching the
filesystem.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir


LEGACY_SOURCE_DIR = "stand_done"
ARCHIVE_SUBDIR = ("archive", "stand-history")


def _iter_history_files(src: Path) -> list[Path]:
    """All non-hidden regular files in src/, sorted for stable output."""
    return sorted(
        p for p in src.iterdir()
        if p.is_file() and not p.name.startswith(".")
    )


def _plan_moves(coord: Path) -> tuple[Path, Path, list[Path]]:
    """Return (source_dir, dest_dir, files_to_move).

    Caller checks ``files_to_move`` emptiness to decide whether the
    op is a no-op.
    """
    src = coord / LEGACY_SOURCE_DIR
    dest = coord
    for part in ARCHIVE_SUBDIR:
        dest = dest / part
    files = _iter_history_files(src) if src.is_dir() else []
    return src, dest, files


@click.command(name="migrate-stand-history",
               help="Archive coordination/stand_done/* under "
                    "coordination/archive/stand-history/.")
@click.option("--coord", "coord_str", default=None,
              help="path to coordination/ (default: resolved via env / cwd)")
@click.option("--dry-run", is_flag=True, default=False,
              help="report what would move; touch nothing")
def migrate_stand_history(coord_str: str | None, dry_run: bool) -> None:
    coord = Path(coord_str) if coord_str else find_coord_dir()
    if not coord.is_dir():
        raise GreatMindsError(f"coordination dir not found: {coord}",
                              exit_code=2)

    src, dest, files = _plan_moves(coord)
    if not files:
        if not src.is_dir():
            click.echo(f"no legacy {LEGACY_SOURCE_DIR}/ at {coord}; "
                       "nothing to migrate")
        else:
            click.echo(f"{src} is empty; nothing to migrate")
        return

    if dry_run:
        click.echo(f"DRY-RUN: would move {len(files)} file(s) "
                   f"{src} → {dest}/")
        for f in files:
            click.echo(f"  {f.name}")
        return

    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    skipped: list[str] = []
    for f in files:
        target = dest / f.name
        if target.exists():
            skipped.append(f.name)
            continue
        shutil.move(str(f), str(target))
        moved += 1

    click.echo(f"moved {moved} file(s) → {dest}/")
    if skipped:
        click.echo(f"skipped {len(skipped)} (target already exists): "
                   f"{', '.join(skipped[:6])}"
                   + (" …" if len(skipped) > 6 else ""))
