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
