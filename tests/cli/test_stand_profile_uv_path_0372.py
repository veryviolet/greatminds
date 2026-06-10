"""Tests for task 0372: canon stand-profile templates must carry the
``environment.PATH`` block that puts ``~/.local/bin`` on PATH so ansible
``command:`` tasks (non-login, non-interactive shells that never source
``~/.profile``) can find ``uv`` on freshly seeded fleets.

Regression from the 1.6.x host-agnostic (add_host) template rewrite, which
dropped the PATH block the pre-1.6 profiles carried. The live fleet was
patched operationally in ``coordination/stand-profiles/full-deploy.yaml``;
this restores it in the SHIPPED templates so future setup / migrate /
reseed don't ship the broken profile.

Two angles are covered:
  1. The shipped templates carry the PATH block on the deploy/smoke play.
  2. ``reseed_stale_stand_profiles`` propagates the fix to fleets seeded
     from the 1.6.x add_host-but-no-PATH templates (which previously
     sniffed as "current" via the add_host shortcut and never got fixed).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from greatminds.cli import setup as setup_mod
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


# ---------- reseed propagates the fix to 1.6.x-seeded fleets ----------


def test_prefix_fixtures_are_registered_stale_hashes() -> None:
    """The committed pre-fix (1.6.x add_host, no PATH) fixtures are real
    shipped templates; their hashes must be in the stale-hash allowlist so
    reseed recognises them as reseedable."""
    fd = (FIXTURES / "full-deploy.no-path.yaml").read_text(encoding="utf-8")
    so = (FIXTURES / "smoke-only.no-path.yaml").read_text(encoding="utf-8")
    assert setup_mod._sha256(fd) in \
        setup_mod._STALE_SHIPPED_PROFILE_HASHES["full-deploy.yaml"]
    assert setup_mod._sha256(so) in \
        setup_mod._STALE_SHIPPED_PROFILE_HASHES["smoke-only.yaml"]


def test_prefix_fixtures_have_add_host_but_no_path() -> None:
    """Sanity: the fixtures genuinely predate the fix — they already carry
    the add_host topology (so the old add_host shortcut would have called
    them 'current') yet lack the uv PATH block."""
    for name in ("full-deploy.no-path.yaml", "smoke-only.no-path.yaml"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert setup_mod._profile_uses_add_host(text), name
        assert _LOCAL_BIN not in text, name


def _coord_with_profile(tmp_path: Path, name: str, content: str) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / "stand-profiles").mkdir(parents=True)
    (coord / "stand-profiles" / name).write_text(content, encoding="utf-8")
    return coord


def test_reseed_refreshes_add_host_no_path_profile(tmp_path: Path) -> None:
    """A fleet seeded from the 1.6.x add_host-but-no-PATH template is
    reseeded to the PATH-restored template (not left as 'current')."""
    for name in ("full-deploy.yaml", "smoke-only.yaml"):
        stale = (FIXTURES / f"{name.removesuffix('.yaml')}.no-path.yaml").read_text("utf-8")
        coord = _coord_with_profile(tmp_path / name, name, stale)
        canon = find_canon_dir()

        result = setup_mod.reseed_stale_stand_profiles(coord, canon)

        assert result["reseeded"] == [name], result
        refreshed = (coord / "stand-profiles" / name).read_text("utf-8")
        assert refreshed == _template_text(name)
        assert _LOCAL_BIN in refreshed
        backup = coord / "stand-profiles" / ".backups" / name
        assert backup.is_file() and backup.read_text("utf-8") == stale


def test_reseed_of_path_fixed_template_is_current(tmp_path: Path) -> None:
    """Once a profile is byte-identical to the PATH-fixed template, reseed
    classifies it current — idempotent, no spurious re-backup."""
    name = "full-deploy.yaml"
    coord = _coord_with_profile(tmp_path, name, _template_text(name))

    result = setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert result["current"] == [name]
    assert result["reseeded"] == []
    assert not (coord / "stand-profiles" / ".backups").exists()


def test_reseed_leaves_operator_add_host_no_path_variant_alone(tmp_path: Path) -> None:
    """An operator's OWN add_host variant (unknown hash) that lacks PATH is
    still left in place — reseed only refreshes pristine shipped copies, it
    must not clobber operator edits even when they miss the PATH fix."""
    variant = (
        "---\n"
        "- hosts: localhost\n"
        "  tasks:\n"
        "    - ansible.builtin.add_host: {name: x, groups: stand_nodes}\n"
        "- hosts: stand_nodes\n"
        "  tasks: [{name: noop, ansible.builtin.command: /bin/true}]\n"
        "# operator's own tweak, no PATH block\n"
    )
    coord = _coord_with_profile(tmp_path, "full-deploy.yaml", variant)

    result = setup_mod.reseed_stale_stand_profiles(coord, find_canon_dir())

    assert result["current"] == ["full-deploy.yaml"]
    assert result["reseeded"] == []
    assert (coord / "stand-profiles" / "full-deploy.yaml").read_text("utf-8") == variant
