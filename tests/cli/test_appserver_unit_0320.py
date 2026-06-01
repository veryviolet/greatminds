"""Tests for task 0320 (0311 Phase 3a): codex app-server as a
systemd-user template unit (one per fleet), installed + enabled by
``greatminds daemon install`` when the fleet has driven codex roles.

Driven codex roles (Phase 3) run their turns through the codex
app-server protocol (thread/start + turn/start, Phase 3b/0321), not
PTY keystrokes. The app-server must be a managed daemon like coordd:
survive logout (``WantedBy=default.target`` + ``enable``), auto-restart
(``Restart=on-failure``). It is gated on driven codex roles so
claude-only / pre-Phase-3 fleets never get the extra unit.

codex 0.133.0 ships the ``app-server`` (+ ``remote-control``)
experimental subcommands, so Phase 3 is unblocked; the unit runs
``codex app-server --listen unix://<sock>`` in the foreground (the
right shape for systemd Type=simple). Live validation (server up,
survives restart, threads resume) is TESTER's GATE — stand_required.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import daemon as daemon_mod
from greatminds.core.paths import find_canon_dir


# ---------- canon template shape ----------


def test_canon_appserver_unit_exists_and_has_required_directives() -> None:
    """0320: the shipped app-server template unit must mirror the
    daemon unit's supervision directives (the 0307 lesson) and run the
    app-server in the foreground over a per-project UNIX socket."""
    src = (find_canon_dir() / "systemd"
           / daemon_mod.APPSERVER_TEMPLATE_UNIT_NAME)
    assert src.is_file(), "0320: canon app-server unit missing"
    body = src.read_text(encoding="utf-8")
    assert "Restart=on-failure" in body
    assert "WantedBy=default.target" in body
    assert "Type=simple" in body
    assert "__CODEX_BIN__" in body, (
        "0320: ExecStart must use the __CODEX_BIN__ placeholder "
        "(substituted at install time, like __GREATMINDS_BIN__)"
    )
    # 0320-iter2: ExecStart names node explicitly + sets PATH so the
    # relative `#!/usr/bin/env node` codex shebang resolves under
    # systemd --user (the GATE-failure fix).
    assert "__NODE_BIN__" in body, (
        "0320-iter2: ExecStart must invoke node explicitly via the "
        "__NODE_BIN__ placeholder (codex shebang is env-node, relative)"
    )
    assert "Environment=PATH=__NODE_DIR__" in body
    assert "__NODE_BIN__ __CODEX_BIN__ app-server --listen unix://" in body


# ---------- unit body rendering ----------


def test_appserver_unit_body_substitutes_node_and_codex(monkeypatch) -> None:
    """0320-iter2: ExecStart = ``<node-abs> <codex-abs> app-server …``;
    Environment=PATH leads with node's dir. All placeholders resolved."""
    monkeypatch.setattr(daemon_mod, "_resolved_codex_exec",
                         lambda: "/abs/codex")
    monkeypatch.setattr(daemon_mod, "_resolved_node_exec",
                         lambda: "/nvm/bin/node")
    body = daemon_mod._appserver_unit_body()
    assert body is not None
    assert "/nvm/bin/node /abs/codex app-server --listen unix://" in body
    assert "Environment=PATH=/nvm/bin:" in body
    for ph in ("__CODEX_BIN__", "__NODE_BIN__", "__NODE_DIR__"):
        assert ph not in body, f"unsubstituted placeholder {ph}"


def test_appserver_unit_body_none_when_codex_absent(monkeypatch) -> None:
    monkeypatch.setattr(daemon_mod, "_resolved_codex_exec", lambda: None)
    monkeypatch.setattr(daemon_mod, "_resolved_node_exec",
                         lambda: "/nvm/bin/node")
    assert daemon_mod._appserver_unit_body() is None


def test_appserver_unit_body_none_when_node_absent(monkeypatch) -> None:
    """0320-iter2: no node interpreter → no runnable ExecStart → skip."""
    monkeypatch.setattr(daemon_mod, "_resolved_codex_exec",
                         lambda: "/abs/codex")
    monkeypatch.setattr(daemon_mod, "_resolved_node_exec", lambda: None)
    assert daemon_mod._appserver_unit_body() is None


def test_install_appserver_unit_writes_and_is_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_resolved_codex_exec",
                         lambda: "/usr/bin/codex")
    monkeypatch.setattr(daemon_mod, "_resolved_node_exec",
                         lambda: "/usr/bin/node")
    first = daemon_mod.install_appserver_unit()
    assert first is True
    dest = tmp_path / daemon_mod.APPSERVER_TEMPLATE_UNIT_NAME
    assert dest.is_file()
    # Second call with identical body → no rewrite.
    assert daemon_mod.install_appserver_unit() is False


def test_install_appserver_unit_none_when_codex_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", tmp_path)
    monkeypatch.setattr(daemon_mod, "_resolved_codex_exec", lambda: None)
    monkeypatch.setattr(daemon_mod, "_resolved_node_exec",
                         lambda: "/usr/bin/node")
    assert daemon_mod.install_appserver_unit() is None
    assert not (tmp_path / daemon_mod.APPSERVER_TEMPLATE_UNIT_NAME).exists()


# ---------- socket-path convention (shared with 0321 driver) ----------


def test_appserver_socket_path_uses_xdg_runtime_dir(monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    p = daemon_mod.appserver_socket_path("my-fleet")
    assert str(p) == "/run/user/4242/greatminds-appserver-my-fleet.sock"


def test_appserver_socket_path_falls_back_without_xdg(monkeypatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    p = daemon_mod.appserver_socket_path("f")
    assert p.name == "greatminds-appserver-f.sock"
    assert str(p).startswith("/run/user/") or str(p).startswith("/tmp")


# ---------- the driven-codex-roles gate ----------


def _write_project(tmp_path: Path, *, windows: list[dict],
                   lifecycles: dict[str, str]) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "coord.yaml").write_text(
        yaml.safe_dump({"session": "f", "windows": windows}),
        encoding="utf-8")
    (project / "schema.yaml").write_text(
        yaml.safe_dump({"roles": {r: {"lifecycle": lc}
                                  for r, lc in lifecycles.items()}}),
        encoding="utf-8")
    return project


def test_gate_true_for_driven_codex_role(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path,
        windows=[{"name": "rev", "role": "ARCHITECT-REVIEWER",
                  "tool": "codex", "mode": "driven"}],
        lifecycles={"ARCHITECT-REVIEWER": "driven"})
    assert daemon_mod.has_driven_codex_roles(project) is True


def test_gate_false_for_codex_role_not_driven(tmp_path: Path) -> None:
    """A codex window whose schema lifecycle != driven (still loop, the
    pre-Phase-3 state) must NOT trigger the app-server unit."""
    project = _write_project(
        tmp_path,
        windows=[{"name": "rev", "role": "ARCHITECT-REVIEWER",
                  "tool": "codex", "mode": "loop"}],
        lifecycles={"ARCHITECT-REVIEWER": "self-loop"})
    assert daemon_mod.has_driven_codex_roles(project) is False


def test_gate_false_for_driven_claude_only(tmp_path: Path) -> None:
    """A fleet with only driven CLAUDE workers (Phase 2e) needs no
    app-server — the gate is about codex specifically."""
    project = _write_project(
        tmp_path,
        windows=[{"name": "dev", "role": "DEVELOPER",
                  "tool": "claude", "mode": "driven"}],
        lifecycles={"DEVELOPER": "driven"})
    assert daemon_mod.has_driven_codex_roles(project) is False


def test_gate_false_when_no_windows(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "coord.yaml").write_text(
        yaml.safe_dump({"session": "f"}), encoding="utf-8")
    assert daemon_mod.has_driven_codex_roles(project) is False


# ---------- daemon install integration ----------


def _stub_install(monkeypatch, *, driven_codex: bool,
                  wrote_app: bool | None = True) -> list:
    """Stub the side-effecting seams so ``daemon install`` is traceable.
    Returns the recorded ``_systemctl`` calls."""
    monkeypatch.setattr(daemon_mod, "detect_legacy_coordd", lambda: False)
    monkeypatch.setattr(daemon_mod, "install_template_unit", lambda: True)
    monkeypatch.setattr(daemon_mod, "register_project",
                        lambda *a, **kw: None)
    monkeypatch.setattr(daemon_mod, "has_driven_codex_roles",
                        lambda *_a, **_k: driven_codex)
    monkeypatch.setattr(daemon_mod, "install_appserver_unit",
                        lambda: wrote_app)
    calls: list = []

    def fake_systemctl(*args):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=("systemctl", *args), returncode=0, stdout="", stderr="")
    monkeypatch.setattr(daemon_mod, "_systemctl", fake_systemctl)
    return calls


def _project_cwd(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "coord.yaml").write_text(
        yaml.safe_dump({"session": "my-fleet"}), encoding="utf-8")
    monkeypatch.chdir(project)


def test_install_enables_appserver_when_driven_codex(
    tmp_path: Path, monkeypatch,
) -> None:
    """0320: with driven codex roles, ``daemon install`` enables BOTH
    greatminds-daemon@my-fleet AND greatminds-appserver@my-fleet."""
    _project_cwd(tmp_path, monkeypatch)
    calls = _stub_install(monkeypatch, driven_codex=True)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 0, result.output

    enable_units = [c[1] for c in calls if c and c[0] == "enable"]
    assert any(u == "greatminds-daemon@my-fleet.service"
               for u in enable_units), enable_units
    assert any(u == "greatminds-appserver@my-fleet.service"
               for u in enable_units), (
        f"0320: app-server instance must be enabled; got {enable_units}"
    )


def test_install_no_appserver_when_not_driven_codex(
    tmp_path: Path, monkeypatch,
) -> None:
    """No driven codex roles → no app-server unit touched (only coordd)."""
    _project_cwd(tmp_path, monkeypatch)
    calls = _stub_install(monkeypatch, driven_codex=False)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 0, result.output

    enable_units = [c[1] for c in calls if c and c[0] == "enable"]
    assert not any("appserver" in u for u in enable_units), (
        f"0320: app-server must NOT be enabled without driven codex; "
        f"got {enable_units}"
    )


def test_install_warns_when_driven_codex_but_codex_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    """Driven codex roles but no codex binary (install_appserver_unit
    returns None) → warn, exit 0, no app-server enable."""
    _project_cwd(tmp_path, monkeypatch)
    calls = _stub_install(monkeypatch, driven_codex=True, wrote_app=None)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 0, result.output
    enable_units = [c[1] for c in calls if c and c[0] == "enable"]
    assert not any("appserver" in u for u in enable_units)
    assert "codex" in (result.output or "").lower()
