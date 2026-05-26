"""Tests for task 0202: ``greatminds update`` auto-runs
``daemon install`` when the per-session template unit is missing.

Pre-0202 the update flow removed the legacy ``coordd.service`` and
then jumped to ``daemon restart`` — which fails if the new
``greatminds-daemon@.service`` template unit was never installed
(fresh pre-0008 fleet upgrading). Operators on Lattice had to
manually run ``greatminds daemon install`` to recover. 0202 fills
the gap so the migration is smooth.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from greatminds.cli import update as update_mod
from greatminds.cli import daemon as daemon_mod


def test_ensure_template_unit_skips_when_already_installed(
    monkeypatch, tmp_path: Path,
) -> None:
    """Already-installed template unit → return without invoking
    daemon install (idempotent: re-running update doesn't re-install)."""
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", tmp_path)
    (tmp_path / daemon_mod.TEMPLATE_UNIT_NAME).write_text(
        "[Unit]\nDescription=stub\n", encoding="utf-8"
    )

    install_called: list = []
    def fake_run(cmd, *a, **kw):
        install_called.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(update_mod.subprocess, "run", fake_run)

    update_mod._step_ensure_template_unit_installed()
    assert install_called == [], (
        "0202: already-installed unit should NOT trigger daemon install"
    )


def test_ensure_template_unit_runs_install_when_missing(
    monkeypatch, tmp_path: Path,
) -> None:
    """0202 happy path: template unit missing → run `greatminds
    daemon install`. This is the migration gap from Lattice 1.2.2 →
    1.2.10."""
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", tmp_path)
    # tmp_path is empty → template not present.

    calls: list = []
    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    monkeypatch.setattr(update_mod.subprocess, "run", fake_run)

    update_mod._step_ensure_template_unit_installed()
    # Some command must have been ``... daemon install``.
    install_cmds = [c for c in calls if c[-2:] == ["daemon", "install"]]
    assert install_cmds, (
        f"0202: expected a `daemon install` invocation; got: {calls}"
    )


def test_ensure_template_unit_propagates_install_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    """If `daemon install` returns non-zero (e.g. systemd-user not
    enabled), update aborts with an actionable hint. Pin against
    silent failure that would leave the fleet broken."""
    import click
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", tmp_path)

    def fake_run(cmd, *a, **kw):
        return subprocess.CompletedProcess(list(cmd), 2, "", "no systemd-user")
    monkeypatch.setattr(update_mod.subprocess, "run", fake_run)

    with pytest.raises(click.exceptions.Exit) as exc:
        update_mod._step_ensure_template_unit_installed()
    assert exc.value.exit_code == 2
