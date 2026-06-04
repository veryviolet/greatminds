"""0278 (0276 Phase B): stand-profile loader.

Pure parser for ``coordination/stand-profiles/<name>.{yaml,md}``
files. No runtime side effects, no SK integration — those land in
Phase C+ (the loader is consumed by SK's deploy flow there).

Lookup precedence (per ``schema.stand_profile``):

  1. ``<coord>/stand-profiles/<name>.yaml`` — preferred when both
     exist. Parsed via ``yaml.safe_load``; required fields (``name``,
     ``hosts``, ``tasks``) validated against the schema list.
  2. ``<coord>/stand-profiles/<name>.md`` — free-prose fallback. An
     optional ``---``-delimited YAML frontmatter at the top carries
     metadata (e.g. ``deploy_prerequisites_only: true``); the rest of
     the file is kept verbatim for SK to inject into its prompt.
  3. Neither file present → :class:`GreatMindsError` with a message
     naming the two paths that were looked at.

The ``deploy_prerequisites_only`` flag is extracted uniformly across
both dialects so callers don't need to branch on format:

  - YAML: read from ``vars.deploy_prerequisites_only`` (default False).
  - MD:   read from the optional frontmatter mapping
    (default False).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


STAND_PROFILES_DIRNAME = "stand-profiles"
PREREQ_ONLY_KEY = "deploy_prerequisites_only"

# Default YAML required-field list — overridden at load-time from the
# schema's ``stand_profile.yaml_required_fields`` if the schema is
# reachable. The constants are the safety net for partial installs.
_DEFAULT_YAML_REQUIRED = ("name", "hosts", "tasks")


@dataclass
class ProfileSpec:
    """Parsed view of a stand-profile file.

    Exactly one of ``yaml_data`` / ``md_content`` is populated, matching
    ``format``. ``deploy_prerequisites_only`` is extracted across both
    dialects so call-site logic stays uniform.
    """

    name: str
    format: Literal["yaml", "md"]
    path: Path
    yaml_data: dict[str, Any] | None = None
    md_content: str | None = None
    deploy_prerequisites_only: bool = False
    # Diagnostic: when the MD frontmatter parsed, the raw frontmatter
    # dict is kept here so callers can read additional ad-hoc metadata
    # without re-parsing.
    md_frontmatter: dict[str, Any] | None = field(default=None)


# ---------------------------------------------------------------------------
# Schema lookup (best-effort)
# ---------------------------------------------------------------------------


def _yaml_required_fields_from_schema() -> tuple[str, ...]:
    """Read ``schema.stand_profile.yaml_required_fields`` if reachable.

    Falls back to ``_DEFAULT_YAML_REQUIRED`` when the canon dir is not
    findable or the schema lacks the section — keeps the loader usable
    in test fixtures that build a minimal coord dir without a full
    canon install.
    """
    try:
        doc = yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError, GreatMindsError):
        return _DEFAULT_YAML_REQUIRED
    sp = doc.get("stand_profile") or {}
    fields_list = sp.get("yaml_required_fields") or []
    if not isinstance(fields_list, list) or not fields_list:
        return _DEFAULT_YAML_REQUIRED
    return tuple(str(x) for x in fields_list)


# ---------------------------------------------------------------------------
# MD frontmatter parser
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def _parse_md_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split ``---``-delimited YAML frontmatter from an MD profile.

    Returns ``(frontmatter_dict_or_none, body_text)``. When the file
    has no frontmatter, the full text is returned as body. Frontmatter
    that fails to parse as a mapping is treated as absent (callers see
    None) — we don't want a malformed frontmatter block to make the
    file unloadable.
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return None, text
    try:
        front = yaml.safe_load(m.group("front")) or {}
    except yaml.YAMLError:
        return None, text
    if not isinstance(front, dict):
        return None, text
    return front, m.group("body")


# ---------------------------------------------------------------------------
# YAML / MD loaders
# ---------------------------------------------------------------------------


def _load_yaml_profile(name: str, path: Path) -> ProfileSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GreatMindsError(
            f"stand-profile {name!r}: failed to read {path}: {exc}",
            exit_code=2,
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GreatMindsError(
            f"stand-profile {name!r} ({path.name}): invalid YAML: {exc}",
            exit_code=2,
        )
    # 0281: ansible-playbook syntax has the document as a LIST of
    # plays. Phase B's original loader only accepted a top-level
    # mapping (single-play short-hand). Accept both: list-of-plays
    # uses the first play for required-field validation; ``yaml_data``
    # keeps the full original shape so ``ansible-playbook`` can
    # consume it directly.
    if isinstance(data, list):
        if not data or not isinstance(data[0], dict):
            raise GreatMindsError(
                f"stand-profile {name!r} ({path.name}): list-of-plays "
                f"form must contain at least one mapping entry",
                exit_code=2,
            )
        first_play = data[0]
    elif isinstance(data, dict):
        first_play = data
    else:
        raise GreatMindsError(
            f"stand-profile {name!r} ({path.name}): top-level must be "
            f"a mapping or list-of-mappings (got {type(data).__name__})",
            exit_code=2,
        )

    required = _yaml_required_fields_from_schema()
    missing = [f for f in required if f not in first_play]
    if missing:
        raise GreatMindsError(
            f"stand-profile {name!r} ({path.name}): missing required "
            f"field(s) {missing!r}; schema requires {list(required)!r}",
            exit_code=2,
        )

    vars_block = first_play.get("vars") or {}
    if not isinstance(vars_block, dict):
        vars_block = {}
    prereq = bool(vars_block.get(PREREQ_ONLY_KEY, False))

    return ProfileSpec(
        name=name,
        format="yaml",
        path=path,
        yaml_data=data,
        deploy_prerequisites_only=prereq,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def profile_paths(coord_dir: Path, profile_name: str) -> tuple[Path, Path]:
    """Return ``(yaml_path, md_path)`` — the two candidate locations.

    Helper for callers (tests, error messages) that want to surface
    the looked-at paths without re-computing them.
    """
    base = coord_dir / STAND_PROFILES_DIRNAME / profile_name
    return base.with_suffix(".yaml"), base.with_suffix(".md")


def load_profile(coord_dir: Path, profile_name: str) -> ProfileSpec:
    """Load ``<coord_dir>/stand-profiles/<profile_name>.{yaml,md}``.

    YAML wins when both formats are present. Raises
    :class:`GreatMindsError` with both candidate paths in the message
    when neither file exists.
    """
    if not profile_name or not isinstance(profile_name, str):
        raise GreatMindsError(
            "load_profile: profile_name must be a non-empty string",
            exit_code=2,
        )
    yaml_path, md_path = profile_paths(coord_dir, profile_name)
    if yaml_path.is_file():
        return _load_yaml_profile(profile_name, yaml_path)
    if md_path.is_file():
        # 1.6.0: MD (prose) profiles are removed — the deploy must be a
        # declarative ansible playbook run deterministically by coordd,
        # not LLM-executed prose. Convert the profile to YAML.
        raise GreatMindsError(
            f"stand-profile {profile_name!r}: MD/prose profiles were "
            f"removed in 1.6.0 ({md_path} exists). Convert it to a YAML "
            f"ansible playbook at {yaml_path} — the deploy is run by "
            f"coordd, not an LLM.",
            exit_code=2,
        )
    raise GreatMindsError(
        f"no profile {profile_name!r} found at {yaml_path}",
        exit_code=2,
    )
