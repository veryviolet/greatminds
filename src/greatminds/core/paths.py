"""Canonical path resolution for greatminds projects.

Project layout:

- ``coordination/`` is tracked, user-editable project configuration.
- ``.greatminds/`` is ignored runtime/system state.
- package data is the installed canon source for schema, bootstrap, docs,
  templates, plugins, and profiles.
"""

from __future__ import annotations

import os
from pathlib import Path

from .util import die


CONFIG_DIR_NAME = "coordination"
RUNTIME_DIR_NAME = ".greatminds"

def _under_worktrees(path: Path) -> bool:
    """True if ``path`` has a ``.worktrees`` ancestor segment.

    A ``coordination/`` directory nested under ``.worktrees/<id>/`` is
    never the canonical project store (GitHub #10): it is an orphan that
    a stray write created from inside a per-task worktree. Path
    resolution must walk PAST it to the real project root rather than
    stop at the worktree and read an empty coordination/ with no queues.
    """
    return ".worktrees" in path.parts


def find_project_dir(start: Path | None = None, *, strict: bool = True) -> Path:
    """Locate the project root.

    ``$GREATMINDS_PROJECT_DIR`` wins. Otherwise walk upward from ``start`` and
    look for either the runtime directory or the tracked config directory.
    """
    env_dir = os.environ.get("GREATMINDS_PROJECT_DIR")
    if env_dir:
        root = Path(env_dir).resolve()
        if (root / RUNTIME_DIR_NAME).is_dir() and not _under_worktrees(root):
            return root
        if (root / CONFIG_DIR_NAME).is_dir() and not _under_worktrees(root):
            return root
    cur = (start or Path.cwd()).resolve()
    for p in [cur, *cur.parents]:
        if _under_worktrees(p):
            continue
        runtime = p / RUNTIME_DIR_NAME
        config = p / CONFIG_DIR_NAME
        if runtime.is_dir() or config.is_dir():
            return p
    if not strict:
        base = cur
        if _under_worktrees(cur):
            for anc in cur.parents:
                if anc.name == ".worktrees":
                    base = anc.parent
                    break
        return base
    die(1, f"no greatminds project found from {cur}")
    raise SystemExit


def find_config_dir(start: Path | None = None, *, strict: bool = True) -> Path:
    """Locate tracked project configuration under ``coordination/``."""
    project = find_project_dir(start, strict=strict)
    return project / CONFIG_DIR_NAME


def find_runtime_dir(start: Path | None = None, *, strict: bool = True) -> Path:
    """Locate ignored runtime/system state under ``.greatminds/``."""
    project = find_project_dir(start, strict=strict)
    runtime = project / RUNTIME_DIR_NAME
    if not runtime.is_dir():
        config = project / CONFIG_DIR_NAME
        if config.is_dir():
            return config
    if strict and not runtime.is_dir():
        die(1, f"no {RUNTIME_DIR_NAME}/ runtime directory found under {project}")
        raise SystemExit
    return runtime


def find_coord_dir(start: Path | None = None, *, strict: bool = True) -> Path:
    """Compatibility alias for the runtime directory.

    Internal modules historically call the runtime task store ``coord``. In
    the project layout, that store is ``.greatminds/``.
    """
    return find_runtime_dir(start, strict=strict)


def project_config_dir(project_dir: Path) -> Path:
    return project_dir / CONFIG_DIR_NAME


def project_runtime_dir(project_dir: Path) -> Path:
    runtime = project_dir / RUNTIME_DIR_NAME
    if not runtime.is_dir():
        config = project_dir / CONFIG_DIR_NAME
        if config.is_dir():
            return config
    return runtime


def project_env_file(project_dir: Path) -> Path:
    return project_runtime_dir(project_dir) / "PROJECT.env"


def coord_yaml_path(project_dir: Path) -> Path:
    current = project_config_dir(project_dir) / "coord.yaml"
    root_copy = project_dir / "coord.yaml"
    if not current.is_file() and root_copy.is_file():
        return root_copy
    return current


def stand_profiles_dir(project_dir: Path) -> Path:
    return project_config_dir(project_dir) / "stand-profiles"


def stand_profiles_registry_path(project_dir: Path) -> Path:
    return project_config_dir(project_dir) / "stand-profiles.yaml"


def config_dir_for_runtime(runtime_dir: Path) -> Path:
    return runtime_dir.parent / CONFIG_DIR_NAME


def ensure_layout_dirs(project_dir: Path) -> tuple[Path, Path]:
    """Create and return ``(config_dir, runtime_dir)``."""
    config = project_config_dir(project_dir)
    runtime = project_runtime_dir(project_dir)
    config.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    return config, runtime


def project_schema_path(project_dir: Path) -> Path:
    return project_runtime_dir(project_dir) / "schema.yaml"


def project_bootstrap_path(project_dir: Path) -> Path:
    return project_runtime_dir(project_dir) / "bootstrap.md"


def project_coordinate_doc_path(project_dir: Path) -> Path:
    return project_runtime_dir(project_dir) / "COORDINATE.md"


def find_canon_dir() -> Path:
    """Locate the canon data directory (``schema.yaml``, ``bootstrap.md``,
    ``COORDINATE.md``, ``plugins/``, ``mcp/``, ``codex/profiles/``,
    ``templates/``).

    Resolution order:
      1. ``$GREATMINDS_CANON_DIR`` — explicit override (sandbox runs, dev clones).
      2. ``importlib.resources.files('greatminds.data')`` — the wheel-shipped
         data directory. For a regular wheel install this is a real filesystem
         path under ``site-packages/greatminds/data/``; for the rare zip-import
         case it is still a Traversable that supports ``/`` and ``.is_file()``.
    """
    env = os.environ.get("GREATMINDS_CANON_DIR")
    if env:
        p = Path(env)
        if (p / "schema.yaml").is_file():
            return p
        die(1, f"GREATMINDS_CANON_DIR={env} set but schema.yaml not found there")
        raise SystemExit
    from importlib.resources import files
    return Path(str(files("greatminds.data")))


def caller_role() -> str:
    """Return the caller's role from ``$GREATMINDS_ROLE`` (uppercased, trimmed).

    The env var is the only source of truth — there is no ``--as ROLE`` flag
    anywhere in the protocol, by design: lying about your role is not a
    feature.

    Callers that want to additionally check the role against ``schema.yaml``
    do so themselves after this returns (kept out of core.paths to avoid a
    schema dependency in path resolution).
    """
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper().strip()
    if not role:
        die(1, "caller role unknown: set GREATMINDS_ROLE in your shell")
        raise SystemExit
    return role
