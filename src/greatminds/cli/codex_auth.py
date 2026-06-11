"""Shared Codex auth/config resolution for paned AND driven launches.

Task 0390 — a paned/interactive Codex role could sit at the Codex
sign-in UI forever even though the host user is already logged in: the
old paned ``start_agent`` path pointed ``CODEX_HOME`` at the per-role
``coordination/.codex-home/<role>`` home, and Codex 0.137 reads auth
from ``$CODEX_HOME/auth.json``. A per-role home holds config ONLY (no
auth.json), so Codex showed the login prompt and ignored the machine
login in ``~/.codex/auth.json``.

The 0375 driven path already solved this: authenticate against the
SINGLE machine Codex home (``auth.json`` lives there) and carry per-role
behavior (model) as ``-c`` overrides — never a per-role ``CODEX_HOME``
for auth. This module is the single home for that resolution so the
paned (``start_agent``) and driven (``coordd``) paths cannot drift on
auth-home selection.

Hard invariants (both paths):
  * ONE machine Codex auth source per host/user owns ``auth.json``.
  * Per-role ``coordination/.codex-home/<role>`` dirs are config /
    profile SOURCES only — never auth, never a login target.
  * auth.json is NEVER copied or symlinked into a per-role home.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def machine_codex_home() -> str:
    """The SINGLE machine Codex home — the one place ``auth.json`` is valid.

    Codex 0.137 stores AND refreshes the ChatGPT auth in
    ``$CODEX_HOME/auth.json`` with single-use refresh tokens, and exposes
    no native split between an auth home and a config/session home. So
    both paned and driven Codex must run against the one machine login.

    Resolution order:
      1. ``GREATMINDS_CODEX_HOME`` — explicit operator override.
      2. An inherited ``CODEX_HOME`` that is NOT a per-role
         ``coordination/.codex-home/<role>`` home (a real machine home
         the process was launched with).
      3. ``~/.codex`` (codex's default).
    """
    override = os.environ.get("GREATMINDS_CODEX_HOME")
    if override:
        return override
    inherited = os.environ.get("CODEX_HOME")
    if inherited and ".codex-home" not in inherited:
        return inherited
    return os.path.expanduser("~/.codex")


def read_role_codex_model(role_home: Path, role_lower: str) -> str | None:
    """Read the role's model from its per-role config SOURCE.

    The per-role ``coordination/.codex-home/<role>`` home is retained as
    role-profile SOURCE MATERIAL ONLY (model selection) — NEVER for auth.
    Reads ``model = "..."`` from the profile layer ``<role>.config.toml``
    (the 0332 split) or the base ``config.toml`` so the launch can inject
    it via a ``-c model=`` argv override and keep the role's model while
    authenticating against the single machine login. Returns ``None`` when
    no model is declared (codex uses its own default)."""
    for name in (f"{role_lower}.config.toml", "config.toml"):
        try:
            text = (role_home / name).read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            return m.group(1)
    return None


def codex_model_config_args(role_home: Path, role_lower: str) -> list[str]:
    """``-c model="<role-model>"`` argv override, or ``[]`` when no model
    is declared. The paned/interactive launch uses this INSTEAD of
    ``--profile <role>`` (which only selects config inside a per-role
    ``CODEX_HOME`` — exactly the per-role-auth path 0375/0390 remove)."""
    model = read_role_codex_model(role_home, role_lower)
    if model:
        return ["-c", f'model="{model}"']
    return []


def machine_codex_auth_present(home: str | Path | None = None) -> bool:
    """True when the effective machine Codex home holds an ``auth.json``."""
    h = Path(home) if home is not None else Path(machine_codex_home())
    return (h / "auth.json").is_file()


def machine_codex_auth_error(home: str, role: str) -> str:
    """Actionable preflight failure message when the machine Codex home
    has no usable ``auth.json``.

    Names the EFFECTIVE machine Codex home and states explicitly that the
    per-role ``coordination/.codex-home/<role>`` homes are config sources,
    NOT login targets — so the operator logs in against the machine home,
    not a role dir."""
    login_hint = (
        "codex login"
        if home == os.path.expanduser("~/.codex")
        else f"CODEX_HOME={home} codex login"
    )
    return (
        f"codex auth missing for role {role}: no auth.json in the effective "
        f"machine Codex home {home}. Paned/driven Codex roles authenticate "
        f"against this ONE machine login; the per-role "
        f"coordination/.codex-home/<role> dirs are config/profile sources "
        f"ONLY and are NOT login targets (no auth.json is copied there). "
        f"Recover by logging in against the machine home:\n    {login_hint}\n"
        f"(or set GREATMINDS_CODEX_HOME to the machine home that owns "
        f"auth.json)."
    )
