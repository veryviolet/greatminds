"""Tests for task 0313 (0311 Phase 1c): MAINTAINER recovery
commands in ``claude_settings.auto_mode.allow``.

MAINTAINER is self-loop (USER-absent) and runs recovery — agent
restart, daemon control, dead-pid SIGTERM. The classifier's
auto-mode ceiling blocked these without explicit allow entries,
so MAINTAINER's autonomous recovery stalled waiting for a USER
who isn't there. 0313 adds the recovery patterns to the
schema-driven auto_mode.allow list that setup bakes into
``.claude/settings.local.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from greatminds.cli import setup as setup_mod
from greatminds.core.paths import find_canon_dir


RECOVERY_PATTERNS = (
    "Bash(greatminds restart:*)",
    "Bash(greatminds daemon:*)",
    "Bash(greatminds start-agent:*)",
    "Bash(kill:*)",
    "Bash(systemctl --user:*)",
)


def _schema_auto_mode() -> list[str]:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return (((doc.get("claude_settings") or {})
             .get("auto_mode") or {})
            .get("allow") or [])


# ---------- schema source-of-truth ----------


def test_schema_auto_mode_has_recovery_patterns() -> None:
    """0313: every recovery pattern MAINTAINER needs must be in
    schema.claude_settings.auto_mode.allow."""
    allow = _schema_auto_mode()
    for pat in RECOVERY_PATTERNS:
        assert pat in allow, (
            f"0313: auto_mode.allow missing recovery pattern "
            f"{pat!r}; MAINTAINER's autonomous recovery will be "
            "blocked by the classifier"
        )


def test_schema_auto_mode_keeps_existing_entries() -> None:
    """Regression net: the 0267/0273 push + ops entries must
    survive the 0313 addition."""
    allow = _schema_auto_mode()
    for legacy in ("$defaults", "Bash(git push origin main:*)",
                    "Bash(ssh:*)", "Bash(rsync:*)"):
        assert legacy in allow




# ---------- setup bakes them into settings.local.json ----------
