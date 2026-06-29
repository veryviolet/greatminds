"""Project-owned stand profile registry.

The registry is the machine-readable catalog for a project's deploy profiles.
Schema owns the vocabulary; ``coordination/stand-profiles.yaml`` owns the
project-specific profile list and maps profile names to YAML playbook files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import (
    RUNTIME_DIR_NAME,
    config_dir_for_runtime,
    find_canon_dir,
)


REGISTRY_FILENAME = "stand-profiles.yaml"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PROFILE_APPROVAL_TOKEN = "USER_APPROVED"


def _config_dir(coord_or_config_dir: Path) -> Path:
    if coord_or_config_dir.name == RUNTIME_DIR_NAME:
        return config_dir_for_runtime(coord_or_config_dir)
    return coord_or_config_dir


@dataclass(frozen=True)
class ProfileEntry:
    name: str
    file: str
    purpose: str
    used_for: tuple[str, ...]
    default_for: tuple[str, ...]
    restore_profile: str | None = None
    environment: str = "stand"
    requires_explicit_user_approval: bool = False
    allowed_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileRegistry:
    path: Path
    source: str
    profiles: dict[str, ProfileEntry] = field(default_factory=dict)

    def require(self, name: str) -> ProfileEntry:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise GreatMindsError(
                f"stand profile {name!r} is not registered in "
                f"{self.path}; registered profiles: {known}",
                exit_code=2,
            )


def _schema_doc() -> dict[str, Any]:
    try:
        return yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError, GreatMindsError):
        return {}


def registry_vocab() -> dict[str, set[str]]:
    doc = _schema_doc()
    cfg = doc.get("stand_profile_registry") or {}
    roles = set((doc.get("roles") or {}).keys())
    return {
        "used_for": set((cfg.get("used_for_values") or {}).keys()),
        "default_for": set((cfg.get("default_for_values") or {}).keys()),
        "environments": set(cfg.get("environment_values") or ["stand"]),
        "roles": roles,
    }


def registry_path(coord_dir: Path, *, worktree: str | Path | None = None
                  ) -> tuple[Path, str]:
    if worktree:
        wt_path = Path(worktree) / "coordination" / REGISTRY_FILENAME
        if wt_path.is_file():
            return wt_path, "lease-worktree"
    return _config_dir(coord_dir) / REGISTRY_FILENAME, "main"


def _safe_profile_name(name: str) -> bool:
    return bool(PROFILE_NAME_RE.match(name))


def _safe_file_name(file_name: str) -> bool:
    p = Path(file_name)
    return (
        bool(file_name)
        and p.name == file_name
        and p.suffix == ".yaml"
        and _safe_profile_name(p.stem)
    )


def load_registry(coord_dir: Path, *,
                  worktree: str | Path | None = None) -> ProfileRegistry:
    path, source = registry_path(coord_dir, worktree=worktree)
    if not path.is_file():
        raise GreatMindsError(
            f"stand profile registry missing at {path}; run "
            "`greatminds setup` or create coordination/stand-profiles.yaml",
            exit_code=2,
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GreatMindsError(
            f"stand profile registry {path}: invalid YAML: {exc}",
            exit_code=2,
        )
    if not isinstance(data, dict):
        raise GreatMindsError(
            f"stand profile registry {path}: top-level must be a mapping",
            exit_code=2,
        )
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise GreatMindsError(
            f"stand profile registry {path}: profiles must be a non-empty mapping",
            exit_code=2,
        )

    vocab = registry_vocab()
    entries: dict[str, ProfileEntry] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(name, str) or not _safe_profile_name(name):
            raise GreatMindsError(
                f"stand profile registry {path}: invalid profile name {name!r}",
                exit_code=2,
            )
        if not isinstance(raw, dict):
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} must be a mapping",
                exit_code=2,
            )
        file_name = str(raw.get("file") or "").strip()
        if not _safe_file_name(file_name):
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} file must be "
                "a local .yaml file name",
                exit_code=2,
            )
        purpose = str(raw.get("purpose") or "").strip()
        if not purpose:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} needs purpose",
                exit_code=2,
            )
        used_for = raw.get("used_for") or []
        default_for = raw.get("default_for") or []
        if not isinstance(used_for, list) or not all(isinstance(x, str) for x in used_for):
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} used_for must be a list",
                exit_code=2,
            )
        if not used_for:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} used_for must not be empty",
                exit_code=2,
            )
        if not isinstance(default_for, list) or not all(isinstance(x, str) for x in default_for):
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} default_for must be a list",
                exit_code=2,
            )
        unknown_used = sorted(set(used_for) - vocab["used_for"])
        unknown_default = sorted(set(default_for) - vocab["default_for"])
        if unknown_used:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} has unknown "
                f"used_for token(s): {unknown_used}",
                exit_code=2,
            )
        if unknown_default:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} has unknown "
                f"default_for token(s): {unknown_default}",
                exit_code=2,
            )
        environment = str(raw.get("environment") or "stand").strip()
        if environment not in vocab["environments"]:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} has unknown "
                f"environment {environment!r}",
                exit_code=2,
            )
        allowed_roles = raw.get("allowed_roles") or []
        if not isinstance(allowed_roles, list) or not all(isinstance(x, str) for x in allowed_roles):
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} allowed_roles must be a list",
                exit_code=2,
            )
        unknown_roles = sorted(set(allowed_roles) - vocab["roles"])
        if unknown_roles:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {name!r} has unknown "
                f"allowed_roles token(s): {unknown_roles}",
                exit_code=2,
            )

        entries[name] = ProfileEntry(
            name=name,
            file=file_name,
            purpose=purpose,
            used_for=tuple(used_for),
            default_for=tuple(default_for),
            restore_profile=(
                str(raw.get("restore_profile")).strip()
                if raw.get("restore_profile") else None
            ),
            environment=environment,
            requires_explicit_user_approval=bool(
                raw.get("requires_explicit_user_approval", False)
            ),
            allowed_roles=tuple(allowed_roles),
        )
    default_owner: dict[str, str] = {}
    for entry in entries.values():
        for token in entry.default_for:
            owner = default_owner.get(token)
            if owner is not None:
                raise GreatMindsError(
                    f"stand profile registry {path}: default_for token "
                    f"{token!r} is claimed by both {owner!r} and "
                    f"{entry.name!r}",
                    exit_code=2,
                )
            default_owner[token] = entry.name
    for entry in entries.values():
        if entry.restore_profile and entry.restore_profile not in entries:
            raise GreatMindsError(
                f"stand profile registry {path}: profile {entry.name!r} "
                f"restore_profile {entry.restore_profile!r} is not registered",
                exit_code=2,
            )
    return ProfileRegistry(path=path, source=source, profiles=entries)


def profile_for_default(coord_dir: Path, token: str, *,
                        worktree: str | Path | None = None
                        ) -> tuple[ProfileRegistry, ProfileEntry] | None:
    """Return the single profile claiming ``default_for: <token>``.

    ``load_registry`` already rejects duplicate default owners, so this helper
    is a small typed lookup for lifecycle code that needs profile intent rather
    than a hard-coded profile name.
    """
    registry = load_registry(coord_dir, worktree=worktree)
    for entry in registry.profiles.values():
        if token in entry.default_for:
            return registry, entry
    return None


def profile_file_path(coord_dir: Path, entry: ProfileEntry) -> Path:
    return _config_dir(coord_dir) / "stand-profiles" / entry.file


def validate_profile_lease_policy(entry: ProfileEntry, *,
                                  holder_role: str,
                                  profile_approval: str | None) -> None:
    """Enforce high-risk profile policy before a lease enters state.yaml."""
    if entry.allowed_roles and holder_role not in entry.allowed_roles:
        allowed = ", ".join(entry.allowed_roles)
        raise GreatMindsError(
            f"profile {entry.name!r} may only be leased by: {allowed}",
            exit_code=2,
        )
    if (entry.requires_explicit_user_approval
            and profile_approval != PROFILE_APPROVAL_TOKEN):
        raise GreatMindsError(
            f"profile {entry.name!r} requires explicit user approval; pass "
            f"`--profile-approval {PROFILE_APPROVAL_TOKEN}` only after the "
            "user has approved this lease",
            exit_code=2,
        )


def doctor_registry(coord_dir: Path, *,
                    worktree: str | Path | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        registry = load_registry(coord_dir, worktree=worktree)
    except GreatMindsError as exc:
        return ([str(exc)], warnings)

    from greatminds.cli.stand_profile import load_profile

    for entry in registry.profiles.values():
        try:
            spec = load_profile(
                coord_dir, entry.name, worktree=worktree, file_name=entry.file
            )
        except GreatMindsError as exc:
            errors.append(f"{entry.name}: {exc}")
            continue
        if spec.format != "yaml":
            errors.append(f"{entry.name}: profile file must be YAML")
    profiles_dir = _config_dir(coord_dir) / "stand-profiles"
    if profiles_dir.is_dir():
        registered_files = {entry.file for entry in registry.profiles.values()}
        for path in sorted(profiles_dir.glob("*.yaml")):
            if path.name not in registered_files:
                warnings.append(
                    f"{path.name}: YAML file exists but is not registered in "
                    f"{REGISTRY_FILENAME}"
                )
    return errors, warnings
