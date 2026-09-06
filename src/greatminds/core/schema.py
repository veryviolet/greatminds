"""Effective contract snapshots shared by CLI, daemon, and agent context.

The installed canon (or explicit GREATMINDS_CANON_DIR) is authoritative.
Project schema files are generated mirrors, never implicit policy overrides.
Snapshots retain their exact content even if the underlying file changes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
from typing import Any

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir, project_schema_path


@lru_cache(maxsize=16)
def _parse(text: str) -> dict[str, Any]:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError("schema root must be a mapping")
    return doc


@dataclass(frozen=True)
class SchemaSnapshot:
    source: Path
    text: str
    sha256: str

    @property
    def document(self) -> dict[str, Any]:
        # Callers may manipulate their view without changing another run's
        # contract or poisoning the shared parse cache.
        return deepcopy(_parse(self.text))

    @property
    def version(self) -> Any:
        return _parse(self.text).get("version")


def load_schema_snapshot(canon_dir: Path | None = None) -> SchemaSnapshot:
    path = ((canon_dir or find_canon_dir()) / "schema.yaml").resolve()
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        _parse(text)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"cannot load schema {path}: {exc}", exit_code=2) from exc
    return SchemaSnapshot(path, text, hashlib.sha256(raw).hexdigest())


def inspect_schema_copy(snapshot: SchemaSnapshot, project_dir: Path) -> dict:
    """Compare the generated project mirror, without rewriting user files."""
    path = project_schema_path(project_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"path": str(path), "status": "missing", "sha256": None}
    except OSError as exc:
        return {"path": str(path), "status": "unreadable", "sha256": None,
                "error": str(exc)}
    digest = hashlib.sha256(raw).hexdigest()
    return {"path": str(path), "sha256": digest,
            "status": "current" if digest == snapshot.sha256 else "drifted"}
