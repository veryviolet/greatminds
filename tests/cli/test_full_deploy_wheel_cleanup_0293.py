"""Tests for task 0293: canon ``full-deploy.yaml`` must clean
stale wheels from ``dist/`` BEFORE ``uv build --wheel`` so the
post-build install glob (``dist/greatminds-*.whl``) resolves to
exactly one file.

EXPLORER 2026-05-27 finding: ``uv pip install dist/greatminds-*.whl``
refuses to install when the glob matches multiple files (a
previous deploy left its wheel behind, the new build added a
second). The live coord file got a pre-build ``rm -f`` task
hand-patched; this brings the canon template into the same shape
so fresh setups don't ship the broken deploy.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


def _tasks() -> list[dict]:
    path = (find_canon_dir() / "templates" / "stand-profiles"
            / "full-deploy.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data
    return data[0].get("tasks") or []


def _task_index(name_substring: str) -> int:
    tasks = _tasks()
    for i, task in enumerate(tasks):
        if name_substring.lower() in (task.get("name") or "").lower():
            return i
    raise AssertionError(
        f"0293: no task containing {name_substring!r} in "
        "full-deploy.yaml"
    )


def _task_body_text(task: dict) -> str:
    """Flatten the shell/command bodies into a single string for
    substring matching — handles both the ``shell: cmd`` scalar
    form and the ``command: {cmd: …}`` mapping form."""
    parts: list[str] = []
    shell = task.get("ansible.builtin.shell")
    if isinstance(shell, str):
        parts.append(shell)
    cmd_block = task.get("ansible.builtin.command") or {}
    if isinstance(cmd_block, dict):
        cmd = cmd_block.get("cmd")
        if isinstance(cmd, str):
            parts.append(cmd)
    return " ".join(parts)


def test_cleanup_task_present() -> None:
    """The canon playbook must include a pre-build cleanup of
    ``dist/greatminds-*.whl``."""
    tasks = _tasks()
    cleanup = [
        t for t in tasks
        if "remove stale wheels" in (t.get("name") or "").lower()
        or "rm -f dist/greatminds-*.whl" in _task_body_text(t)
    ]
    assert cleanup, (
        "0293: full-deploy.yaml must have a task that removes "
        "stale dist/greatminds-*.whl before the build step"
    )


def test_cleanup_runs_before_build() -> None:
    """Ordering: cleanup must precede ``build greatminds wheel
    locally``. Pin the index relationship — a future refactor
    that swaps the order would silently re-introduce the bug."""
    cleanup_idx = _task_index("remove stale wheels")
    build_idx = _task_index("build greatminds wheel")
    assert cleanup_idx < build_idx, (
        f"0293: cleanup task (idx={cleanup_idx}) must run BEFORE "
        f"build (idx={build_idx})"
    )


def test_cleanup_uses_shell_with_glob() -> None:
    """The cleanup invokes ``rm -f dist/greatminds-*.whl`` via
    ``ansible.builtin.shell`` (not ``command``) so the glob is
    actually expanded by the shell."""
    cleanup_idx = _task_index("remove stale wheels")
    task = _tasks()[cleanup_idx]
    shell_cmd = task.get("ansible.builtin.shell")
    assert isinstance(shell_cmd, str)
    assert "rm -f" in shell_cmd
    assert "dist/greatminds-*.whl" in shell_cmd


def test_cleanup_chdir_at_deploy_path() -> None:
    """The rm runs in the deploy_path (where dist/ lives)."""
    cleanup_idx = _task_index("remove stale wheels")
    task = _tasks()[cleanup_idx]
    args = task.get("args") or {}
    assert args.get("chdir") == "{{ deploy_path }}"
