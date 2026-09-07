"""Shipped profiles retain executable PATH, loopback and wheel-install behavior."""
from __future__ import annotations

from pathlib import Path

import yaml

from greatminds.core.paths import find_canon_dir


FIXTURES = Path(__file__).parent / "fixtures_0372"
_LOCAL_BIN = "/.local/bin"


def _template_text(name: str) -> str:
    path = find_canon_dir() / "templates" / "stand-profiles" / name
    return path.read_text(encoding="utf-8")


def _deploy_play(name: str) -> dict:
    """The last play in a profile is the one that runs on the stand
    node(s) (Play 1 only registers add_host on localhost)."""
    data = yaml.safe_load(_template_text(name))
    assert isinstance(data, list) and data
    return data[-1]


def _register_play(name: str) -> dict:
    data = yaml.safe_load(_template_text(name))
    assert isinstance(data, list) and data
    return data[0]


# ---------- shipped templates carry the PATH block ----------


def test_full_deploy_play_has_uv_path_environment() -> None:
    play = _deploy_play("full-deploy.yaml")
    env = play.get("environment") or {}
    path = env.get("PATH")
    assert isinstance(path, str), "full-deploy deploy play must set environment.PATH"
    assert _LOCAL_BIN in path, (
        "0372: full-deploy environment.PATH must prepend ~/.local/bin so "
        "ansible command tasks find uv on freshly seeded fleets"
    )


def test_smoke_only_play_has_uv_path_environment() -> None:
    play = _deploy_play("smoke-only.yaml")
    env = play.get("environment") or {}
    path = env.get("PATH")
    assert isinstance(path, str), "smoke-only play must set environment.PATH"
    assert _LOCAL_BIN in path, (
        "0372: smoke-only environment.PATH must prepend ~/.local/bin"
    )


def test_full_deploy_path_targets_stand_user_home() -> None:
    """The PATH uses the STAND_USER PROJECT.env var (with a default) so the
    home dir resolves per-fleet rather than being hardcoded to one host."""
    path = (_deploy_play("full-deploy.yaml").get("environment") or {}).get("PATH", "")
    assert "STAND_USER" in path, (
        "0372: PATH must derive ~/.local/bin from STAND_USER (PROJECT.env), "
        "not a single hardcoded user"
    )


def test_path_block_present_in_raw_template_text() -> None:
    """Belt-and-suspenders text check: the marker survives any YAML
    round-tripping and is greppable for downstream tooling."""
    for name in ("full-deploy.yaml", "smoke-only.yaml"):
        assert _LOCAL_BIN in _template_text(name), name


# ---------- loopback hosts must not require SSH ----------


def test_loopback_hosts_use_local_ansible_connection() -> None:
    """A user-path toy stand commonly sets STAND_HOST=localhost.
    The shipped profiles must not make ansible SSH to localhost, because
    fresh hosts can fail on known_hosts before any product probe runs."""
    for name in ("full-deploy.yaml", "smoke-only.yaml", "vite-dev.yaml"):
        tasks = _register_play(name).get("tasks") or []
        add_host = next(t for t in tasks if "ansible.builtin.add_host" in t)
        args = add_host["ansible.builtin.add_host"]
        expr = args.get("ansible_connection", "")
        assert "local" in expr
        assert "localhost" in expr
        assert "127.0.0.1" in expr
        assert "::1" in expr


def test_full_deploy_has_local_rsync_without_ssh_target() -> None:
    """When ansible_connection=local, rsync must copy to deploy_path as a
    local filesystem path, not to ``localhost:deploy_path`` over SSH."""
    tasks = _deploy_play("full-deploy.yaml").get("tasks") or []
    local_sync = next(
        t for t in tasks if t.get("name") == "sync worktree to local deploy_path"
    )
    cmd = local_sync["ansible.builtin.command"]["cmd"]
    assert "{{ inventory_hostname }}:" not in cmd
    assert "{{ deploy_path }}/" in cmd
    assert local_sync.get("when") == "(ansible_connection | default('ssh')) == 'local'"


def test_full_deploy_remote_rsync_still_uses_ssh_target() -> None:
    tasks = _deploy_play("full-deploy.yaml").get("tasks") or []
    remote_sync = next(
        t for t in tasks if t.get("name") == "sync worktree to deploy_path over ssh"
    )
    cmd = remote_sync["ansible.builtin.command"]["cmd"]
    assert "{{ inventory_hostname }}:{{ deploy_path }}/" in cmd
    assert remote_sync.get("when") == "(ansible_connection | default('ssh')) != 'local'"


# ---------- install task must expand the wheel glob via a shell ----------


def _tasks_with_wheel_glob(name: str) -> list[dict]:
    """All tasks in the deploy play whose module args mention the
    dist/greatminds-*.whl glob (raw text scan over each task dict)."""
    matches = []
    for task in _deploy_play(name).get("tasks") or []:
        if "greatminds-*.whl" in yaml.safe_dump(task):
            matches.append(task)
    return matches


def test_wheel_install_uses_shell_not_command() -> None:
    """The dist/greatminds-*.whl glob is only expanded by a shell.
    ``ansible.builtin.command`` runs no shell, so uv would receive the
    literal unexpanded path and the install would fail on a freshly built
    dist/. Every task that passes the glob must therefore use
    ``ansible.builtin.shell`` (mirroring remove-stale-wheels)."""
    globbed = _tasks_with_wheel_glob("full-deploy.yaml")
    assert globbed, "0372: expected a wheel-install task referencing greatminds-*.whl"
    for task in globbed:
        assert "ansible.builtin.shell" in task, (
            "0372: a task passing the dist/greatminds-*.whl glob must use "
            "ansible.builtin.shell so the glob expands; ansible.builtin.command "
            f"leaves it literal and uv install fails. Offending task: {task.get('name')}"
        )
        assert "ansible.builtin.command" not in task, (
            f"0372: {task.get('name')} must not use command for a glob arg"
        )
