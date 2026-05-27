"""Tests for task 0296: ``schema.stand.resource.profiles_allowed``
must include ``liveness-prose`` so the lease CLI accepts the new
md-only profile.

0295 added the canon template but skipped the enum extension; SK
would otherwise refuse ``stand lease --profile liveness-prose``
with exit 2 before even reaching the loader.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import stand as stand_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema enum ----------


def test_schema_profiles_allowed_includes_liveness_prose() -> None:
    """0296: schema.stand.resource.profiles_allowed must list
    liveness-prose alongside the legacy three (full-deploy,
    vite-dev, smoke-only)."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    allowed = ((doc.get("stand") or {}).get("resource") or {}
               ).get("profiles_allowed") or []
    assert "liveness-prose" in allowed, (
        f"0296: profiles_allowed must include 'liveness-prose' "
        f"(got {allowed!r})"
    )


def test_schema_profiles_allowed_keeps_legacy_three() -> None:
    """Regression net: adding ``liveness-prose`` must not displace
    the existing entries."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    allowed = ((doc.get("stand") or {}).get("resource") or {}
               ).get("profiles_allowed") or []
    for legacy in ("full-deploy", "vite-dev", "smoke-only"):
        assert legacy in allowed


def test_allowed_profiles_helper_returns_liveness_prose() -> None:
    """The internal ``_allowed_profiles`` helper used by
    ``stand lease`` must surface ``liveness-prose`` so the CLI
    enum check passes."""
    allowed = stand_mod._allowed_profiles()
    assert "liveness-prose" in allowed


# ---------- canon template still ships ----------


def test_liveness_prose_canon_template_present() -> None:
    """Schema points at the profile name; the canon template must
    exist so ``greatminds setup`` seeds the file for SK to load."""
    p = (find_canon_dir() / "templates" / "stand-profiles"
         / "liveness-prose.md")
    assert p.is_file(), (
        "0296: canon liveness-prose.md must ship alongside the "
        "schema enum extension (otherwise lease accepts the name "
        "but load_profile fails to find the file)"
    )


# ---------- lease CLI accepts the new profile ----------


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8",
    )
    (project / ".worktrees" / "0296").mkdir(parents=True)
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.setenv("GREATMINDS_ROLE", "TESTER")
    monkeypatch.chdir(project)
    return project


def test_stand_lease_cli_accepts_liveness_prose(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end: ``greatminds stand lease --profile liveness-prose``
    no longer rejects with exit 2. Pre-0296 the enum check fired
    first; post-0296 the lease proceeds and writes state.yaml."""
    project = _project(tmp_path, monkeypatch)
    wt = project / ".worktrees" / "0296"

    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0296-probe",
        "--worktree", str(wt),
        "--profile", "liveness-prose",
    ])
    assert result.exit_code == 0, (
        f"0296: lease must accept liveness-prose profile. "
        f"stdout={result.output!r} stderr={result.exception!r}"
    )
    assert "lease_id" in result.output


def test_stand_lease_cli_still_rejects_rogue_profile(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression net: adding ``liveness-prose`` must not relax the
    enum — a rogue name still fails with the profiles_allowed
    diagnostic."""
    project = _project(tmp_path, monkeypatch)
    wt = project / ".worktrees" / "0296"

    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, [
        "lease",
        "--task", "0296-probe",
        "--worktree", str(wt),
        "--profile", "rogue-profile-name",
    ])
    assert result.exit_code != 0
    out = result.output + (str(result.exception)
                            if result.exception else "")
    assert "rogue-profile-name" in out
    assert "profiles_allowed" in out
