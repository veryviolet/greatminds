"""Tests for task 0292: canon ``full-deploy.yaml`` must use
``uv pip install`` (not ``.venv-coord/bin/pip``) since ``uv venv``
doesn't install pip into the venv by default.

EXPLORER 2026-05-27 finding: Phase H lease on 0284 failed at the
install step with rc=2 (pip binary missing in the uv-created
venv). The live coord file was hand-patched; this task brings the
canon template into the same shape so fresh setups don't ship the
broken install command."""
from __future__ import annotations

import re

import yaml

from greatminds.core.paths import find_canon_dir


def _full_deploy_playbook() -> dict:
    path = (find_canon_dir() / "templates" / "stand-profiles"
            / "full-deploy.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data
    return data[-1]  # deploy play (add_host bootstrap play precedes it)


def _install_task(play: dict) -> dict:
    """Find the install-wheel step in the play's task list."""
    for task in play.get("tasks") or []:
        name = (task.get("name") or "").lower()
        if "install greatminds into venv" in name:
            return task
    raise AssertionError(
        "0292: full-deploy.yaml missing the 'install greatminds "
        "into venv' task"
    )


def _cmd_text(task: dict) -> str:
    """Return the task's ``cmd`` as a single string (collapsing the
    YAML block-scalar style ``>`` form into one line for substring
    matching)."""
    cmd_block = task.get("ansible.builtin.command") \
        or task.get("command") or {}
    cmd = cmd_block.get("cmd") or ""
    return re.sub(r"\s+", " ", cmd).strip()


def test_install_step_uses_uv_pip_not_dot_venv_pip() -> None:
    """0292: the install command must invoke ``uv pip install``
    (uv's built-in pip surface) — NOT the missing
    ``.venv-coord/bin/pip`` binary."""
    play = _full_deploy_playbook()
    task = _install_task(play)
    cmd = _cmd_text(task)
    assert "uv pip install" in cmd, (
        f"0292: install command must use 'uv pip install' "
        f"(got: {cmd!r})"
    )
    assert ".venv-coord/bin/pip " not in cmd, (
        f"0292: must not invoke .venv-coord/bin/pip directly "
        f"(uv venv lacks pip): {cmd!r}"
    )


def test_install_step_pins_python_interpreter() -> None:
    """``uv pip install`` must be pinned to the venv's interpreter
    via ``--python .venv-coord/bin/python`` so the wheel lands in
    the right env."""
    play = _full_deploy_playbook()
    task = _install_task(play)
    cmd = _cmd_text(task)
    assert "--python .venv-coord/bin/python" in cmd, (
        f"0292: install command must --python-pin the venv's "
        f"interpreter (got: {cmd!r})"
    )


def test_install_step_keeps_force_reinstall() -> None:
    """Regression net: the ``--force-reinstall`` flag must survive
    the rewrite — without it, repeat deploys may skip the new
    wheel build."""
    play = _full_deploy_playbook()
    task = _install_task(play)
    cmd = _cmd_text(task)
    assert "--force-reinstall" in cmd


def test_install_step_chdir_unchanged() -> None:
    """The ansible chdir must still point at deploy_path; the rewrite
    only changed the cmd."""
    play = _full_deploy_playbook()
    task = _install_task(play)
    cmd_block = task.get("ansible.builtin.command") \
        or task.get("command") or {}
    assert cmd_block.get("chdir") == "{{ deploy_path }}"
