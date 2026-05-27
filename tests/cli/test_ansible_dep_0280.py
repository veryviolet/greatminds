"""Tests for task 0280 (0276 Phase D): ansible-core dependency.

Phase C added YAML stand-profile execution via ``ansible-playbook``
subprocess (cli/stand_executor.py); Phase D pins ansible-core as a
hard dependency in pyproject.toml + adds a setup-time sanity check
so broken installs surface immediately instead of mid-deploy.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from greatminds.cli import setup as setup_mod


def _pyproject_text() -> str:
    # Walk up from this test file to the worktree root (where
    # pyproject.toml lives) so the test runs identically in the
    # main checkout and per-task worktrees.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("pyproject.toml not found from test file")


# ---------- pyproject pin ----------


def test_pyproject_has_ansible_core_dependency() -> None:
    """0280: ``ansible-core`` must appear in ``[project] dependencies``
    so a fresh ``pip install greatminds`` provides it transparently."""
    text = _pyproject_text()
    # Match ``ansible-core`` inside the dependencies array. We tolerate
    # version specifiers, quotes, and surrounding whitespace.
    m = re.search(
        r'"ansible-core(?P<spec>[^"\n]*)"',
        text,
    )
    assert m is not None, (
        "0280: pyproject.toml [project] dependencies must include "
        "'ansible-core' so YAML stand-profile execution works out "
        "of the box"
    )


def test_pyproject_ansible_version_pins_to_2_16_plus() -> None:
    """Pin against accidental loosening: at least ``>=2.16`` so
    ansible features the executor relies on (modern collection
    resolution, ``--tags`` shape) are guaranteed."""
    text = _pyproject_text()
    m = re.search(r'"ansible-core(?P<spec>[^"]*)"', text)
    assert m is not None
    spec = m.group("spec")
    assert ">=2.16" in spec, (
        f"0280: ansible-core lower bound must be >=2.16 "
        f"(got spec {spec!r})"
    )


# ---------- setup-time sanity check ----------


def test_setup_check_runs_and_logs_version_when_present(
    monkeypatch, capsys,
) -> None:
    """``_check_ansible_playbook_available`` runs ``--version`` and
    logs the first output line so the setup log records the exact
    ansible build the operator just got."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda name: "/fake/bin/ansible-playbook"
        if name == "ansible-playbook" else None,
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="ansible [core 2.17.4]\n  config file = None\n",
            stderr="",
        )
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    setup_mod._check_ansible_playbook_available()
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "ansible-playbook" in out
    assert "2.17.4" in out


def test_setup_check_warns_when_ansible_missing(
    monkeypatch, capsys,
) -> None:
    """No ``ansible-playbook`` on PATH → setup emits a warning
    pointing at the YAML-profile failure mode + ``pip show
    ansible-core`` diagnostic. Setup itself does NOT raise — MD
    profiles still work."""
    monkeypatch.setattr(setup_mod.shutil, "which", lambda _name: None)
    setup_mod._check_ansible_playbook_available()
    err = capsys.readouterr().err
    assert "ansible-playbook" in err
    assert "ansible-core" in err
    assert "MD" in err or "md" in err  # mention the fall-back path


def test_setup_check_warns_when_version_subprocess_fails(
    monkeypatch, capsys,
) -> None:
    """Binary present but ``--version`` non-zero / hangs → warn that
    the YAML profile path may be flaky. The binary itself still
    resolves so we don't claim it's missing."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda _name: "/fake/bin/ansible-playbook",
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="oops",
        )
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    setup_mod._check_ansible_playbook_available()
    err = capsys.readouterr().err
    assert "ansible-playbook" in err
    assert "flaky" in err.lower() or "failed" in err.lower()


def test_setup_check_warns_on_subprocess_timeout(
    monkeypatch, capsys,
) -> None:
    """Defensive: a hung ``--version`` (TimeoutExpired) must NOT
    propagate; the check warns and returns."""
    monkeypatch.setattr(
        setup_mod.shutil, "which",
        lambda _name: "/fake/bin/ansible-playbook",
    )

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    # Should not raise — must warn instead.
    setup_mod._check_ansible_playbook_available()
    err = capsys.readouterr().err
    assert "ansible-playbook" in err
