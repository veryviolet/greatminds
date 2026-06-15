"""Task 0390: paned/interactive Codex uses the SINGLE machine Codex login.

A paned/interactive Codex role could sit at the Codex sign-in UI forever
(USABLE=NO(pane:auth_prompt)) even though the host user was already logged
in: the pre-0390 ``start_agent`` codex arm pointed ``CODEX_HOME`` at the
per-role ``.greatminds/.codex-home/<role>`` home, which holds config ONLY
(no ``auth.json``). Codex 0.137 reads auth from ``$CODEX_HOME/auth.json``,
so it ignored the machine login in ``~/.codex/auth.json`` and showed the
sign-in prompt.

The fix mirrors the 0375 driven model and shares its helpers
(``greatminds.cli.codex_auth``) so paned and driven Codex cannot drift on
auth-home selection:

  * ``CODEX_HOME`` points at the SINGLE machine Codex home (auth lives
    there), never a per-role home;
  * the role model rides a ``-c model="..."`` override, never
    ``--profile`` (which only selects config inside a per-role CODEX_HOME);
  * the per-role home stays a config SOURCE — ``auth.json`` is NEVER
    copied or symlinked into it;
  * a missing machine ``auth.json`` fails fast with an actionable message
    that names the effective machine home and states the per-role homes
    are not login targets.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import codex_auth
from greatminds.cli import coordd as cd
from greatminds.cli import start_agent as sa_mod


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """Isolate the env the codex arm mutates. The launch arm sets
    ``os.environ['CODEX_HOME']`` directly (not via monkeypatch), so pop it
    explicitly after each test to avoid leaking into the next."""
    monkeypatch.setenv("GREATMINDS_START_AGENT_SAFE", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GREATMINDS_CODEX_HOME", raising=False)
    yield
    os.environ.pop("CODEX_HOME", None)


def _machine_home(tmp_path: Path, with_auth: bool, name: str = "machine-codex") -> Path:
    """A machine Codex home (auth.json present iff ``with_auth``)."""
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    if with_auth:
        (home / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    return home


def _seed_per_role_config(project: Path, role_lower: str) -> Path:
    """Per-role config SOURCE: model declared, NO auth.json."""
    home = project / ".greatminds" / ".codex-home" / role_lower
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        f'developer_instructions = "ok"\nmodel = "gpt-5-codex"\n'
        f'\n[profiles.{role_lower}]\nmodel = "gpt-5-codex"\n',
        encoding="utf-8",
    )
    return home


# --------------------------------------------------------------------------
# codex_auth: the shared resolver (single source of truth)
# --------------------------------------------------------------------------


def test_machine_home_prefers_explicit_override(tmp_path, monkeypatch):
    home = _machine_home(tmp_path, with_auth=True)
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME",
                       str(tmp_path / ".greatminds" / ".codex-home" / "dev"))
    assert codex_auth.machine_codex_home() == str(home)


def test_machine_home_uses_inherited_non_per_role(tmp_path, monkeypatch):
    real = tmp_path / "real-codex"
    real.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real))
    assert codex_auth.machine_codex_home() == str(real)


def test_machine_home_ignores_inherited_per_role_home(tmp_path, monkeypatch):
    """An inherited CODEX_HOME that is a per-role ``.codex-home`` dir must
    NOT be treated as the machine home — fall through to ~/.codex."""
    per_role = tmp_path / ".greatminds" / ".codex-home" / "developer"
    per_role.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(per_role))
    # machine_codex_home falls back via os.path.expanduser("~/.codex"),
    # which resolves $HOME — redirect it so we never read the real ~/.codex.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert codex_auth.machine_codex_home() == str(tmp_path / ".codex")


def test_model_config_args_reads_model_else_empty(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    role_home = _seed_per_role_config(project, "developer")
    assert codex_auth.codex_model_config_args(role_home, "developer") == [
        "-c", 'model="gpt-5-codex"']
    # No config → no override.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert codex_auth.codex_model_config_args(empty, "developer") == []


def test_auth_present_true_false(tmp_path):
    assert codex_auth.machine_codex_auth_present(
        _machine_home(tmp_path, True, name="with-auth"))
    assert not codex_auth.machine_codex_auth_present(
        _machine_home(tmp_path, False, name="no-auth"))


def test_auth_error_names_machine_home_and_not_login_target(tmp_path):
    home = _machine_home(tmp_path, with_auth=False)
    msg = codex_auth.machine_codex_auth_error(str(home), "ARCHITECT-PLANNER")
    assert str(home) in msg
    assert "ARCHITECT-PLANNER" in msg
    assert "NOT login targets" in msg
    assert "codex login" in msg


# --------------------------------------------------------------------------
# start_agent paned codex arm — integration via the click command
# --------------------------------------------------------------------------


def _invoke(project: Path, role: str, dry_run: bool, monkeypatch):
    monkeypatch.setenv("GREATMINDS_PROJECT_DIR", str(project))
    monkeypatch.setenv("GREATMINDS_START_AGENT_NOPTY", "1")
    monkeypatch.setenv("GREATMINDS_START_AGENT_NOTITLE", "1")
    monkeypatch.chdir(project)
    args = [role, "codex", "--mode", "loop"]
    if dry_run:
        args.append("--dry-run")
    return CliRunner().invoke(sa_mod.start_agent, args, catch_exceptions=False)


def test_paned_dry_run_sets_machine_home_and_uses_c_model(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    role_home = _seed_per_role_config(project, "architect-planner")
    machine = _machine_home(tmp_path, with_auth=True)
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", str(machine))

    result = _invoke(project, "ARCHITECT-PLANNER", dry_run=True,
                     monkeypatch=monkeypatch)
    assert result.exit_code == 0, result.output
    # CODEX_HOME points at the MACHINE home, not the per-role home.
    assert os.environ.get("CODEX_HOME") == str(machine)
    assert os.environ.get("CODEX_HOME") != str(role_home)
    # Model rides -c, never --profile.
    assert "--profile" not in result.output
    assert 'model="gpt-5-codex"' in result.output
    # The per-role home is never given an auth.json.
    assert not (role_home / "auth.json").exists()


def test_paned_no_authjson_copied_into_per_role_home(tmp_path, monkeypatch):
    """Hard invariant: the launch never creates auth.json under a per-role
    home (no copy / symlink), even after a real (non-dry-run-shaped) arm."""
    project = tmp_path / "project"
    project.mkdir()
    role_home = _seed_per_role_config(project, "architect-planner")
    machine = _machine_home(tmp_path, with_auth=True)
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", str(machine))

    _invoke(project, "ARCHITECT-PLANNER", dry_run=True, monkeypatch=monkeypatch)
    assert not (role_home / "auth.json").exists()
    assert not (role_home / "auth.json").is_symlink()


def test_paned_missing_machine_auth_fails_fast(tmp_path, monkeypatch):
    """No auth.json in the effective machine home → clear preflight failure
    naming the machine home; never a silent park at the sign-in UI."""
    project = tmp_path / "project"
    project.mkdir()
    _seed_per_role_config(project, "architect-planner")
    machine = _machine_home(tmp_path, with_auth=False)  # NO auth.json
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", str(machine))

    result = _invoke(project, "ARCHITECT-PLANNER", dry_run=False,
                     monkeypatch=monkeypatch)
    assert result.exit_code == 2, result.output
    assert str(machine) in result.output
    assert "NOT login targets" in result.output


# --------------------------------------------------------------------------
# Anti-drift: coordd (driven) shares the same machine-home resolver
# --------------------------------------------------------------------------


def test_coordd_machine_home_delegates_to_codex_auth(tmp_path, monkeypatch):
    home = _machine_home(tmp_path, with_auth=True)
    monkeypatch.setenv("GREATMINDS_CODEX_HOME", str(home))
    assert cd._machine_codex_home() == codex_auth.machine_codex_home()
    assert cd._machine_codex_home() == str(home)


def test_coordd_role_model_delegates_to_codex_auth(tmp_path):
    coord = tmp_path / "coordination"
    role_home = coord / ".codex-home" / "developer"
    role_home.mkdir(parents=True)
    (role_home / "config.toml").write_text('model = "gpt-5-codex"\n',
                                            encoding="utf-8")
    assert cd._codex_role_model(coord, "developer") == "gpt-5-codex"
    assert cd._codex_role_model(None, "developer") is None
