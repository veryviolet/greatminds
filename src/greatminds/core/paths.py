"""Canonical path resolution for the coordination protocol.

Every CLI module imports from here instead of forking its own copy of
``find_coord_dir`` / ``find_canon_dir`` / ``caller_role``. The originals were
duplicated across 6+ scripts in ``/opt/coordination/bin/*`` with subtle
differences (some accept a ``start`` arg, some fall back to ``cwd/coordination``
without dying, some validate the role against ``schema.yaml``).

Resolution rules (in order, first match wins):

- **coord** (project-local task store, e.g. ``/opt/guardora/lattice/coordination/``):
    1. ``$COORD_PROJECT_DIR / "coordination"`` if that directory exists.
    2. Walk up from ``start`` (or ``Path.cwd()``) looking for ``coordination/``.

- **canon** (package data — schema, command_START, role docs, plugins, mcp,
  templates):
    1. ``$COORD_CANON_DIR`` if set and contains ``schema.yaml``.
    2. ``importlib.resources.files('greatminds.data')`` — wheel-shipped data.

- **role** (caller identity):
    1. ``$COORD_ROLE`` (uppercased, trimmed). Empty → die.
"""

from __future__ import annotations

import os
from pathlib import Path

from .util import die


def find_coord_dir(start: Path | None = None, *, strict: bool = True) -> Path:
    """Locate the project-local ``coordination/`` directory.

    Walks from ``start`` (default ``Path.cwd()``) upward looking for a
    ``coordination/`` child. The ``$COORD_PROJECT_DIR`` env var overrides the
    walk: if set, ``$COORD_PROJECT_DIR/coordination`` is returned unconditionally
    when it exists.

    Args:
        start: starting point for the walk; ``None`` means ``Path.cwd()``.
        strict: when ``True`` (the default) the function ``die``s if no
            ``coordination/`` is found. When ``False`` it returns
            ``start/coordination`` as a best-effort fallback (used by
            ``pty-launch`` which runs before the project tree may exist).
    """
    env_dir = os.environ.get("COORD_PROJECT_DIR")
    if env_dir:
        p = Path(env_dir) / "coordination"
        if p.is_dir():
            return p
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        c = p / "coordination"
        if c.is_dir():
            return c
    if not strict:
        return cur / "coordination"
    die(1, f"no coordination/ directory found from {cur}")
    raise SystemExit  # unreachable, helps type-checkers


def find_canon_dir() -> Path:
    """Locate the canon data directory (``schema.yaml``, ``command_START.yaml``,
    ``roles/``, ``plugins/``, ``mcp/``, ``codex/profiles/``, ``templates/``).

    Resolution order:
      1. ``$COORD_CANON_DIR`` — explicit override (sandbox runs, dev clones).
      2. ``importlib.resources.files('greatminds.data')`` — the wheel-shipped
         data directory. For a regular wheel install this is a real filesystem
         path under ``site-packages/greatminds/data/``; for the rare zip-import
         case it is still a Traversable that supports ``/`` and ``.is_file()``.
    """
    env = os.environ.get("COORD_CANON_DIR")
    if env:
        p = Path(env)
        if (p / "schema.yaml").is_file():
            return p
        die(1, f"COORD_CANON_DIR={env} set but schema.yaml not found there")
        raise SystemExit
    from importlib.resources import files
    return Path(str(files("greatminds.data")))


def caller_role() -> str:
    """Return the caller's role from ``$COORD_ROLE`` (uppercased, trimmed).

    The env var is the only source of truth — there is no ``--as ROLE`` flag
    anywhere in the protocol, by design: lying about your role is not a
    feature.

    Callers that want to additionally check the role against ``schema.yaml``
    do so themselves after this returns (kept out of core.paths to avoid a
    schema dependency in path resolution).
    """
    role = (os.environ.get("COORD_ROLE") or "").upper().strip()
    if not role:
        die(1, "caller role unknown: set COORD_ROLE in your shell")
        raise SystemExit
    return role
