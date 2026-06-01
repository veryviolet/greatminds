"""Tests for task 0307: ``greatminds daemon install`` must invoke
``systemctl --user enable`` so the per-project daemon survives KDE
logout / shutdown.

Pre-0307 the install path wrote the template unit + registered the
project but never ran ``enable``. ``is-enabled`` stayed
``disabled; preset: enabled`` → systemd-user tore the unit down
with default.target on logout. MAINTAINER traced the symptom
(coordd dead ~2.5 days, 2890+ dead-pid asks accumulated).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import daemon as daemon_mod


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "coord.yaml").write_text(
        yaml.safe_dump({"session": "my-fleet"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    return project


def _stub_helpers(monkeypatch, *,
                   legacy: bool = False,
                   wrote_unit: bool = True,
                   enable_rc: int = 0) -> list:
    """Replace daemon's side-effecting helpers with traceable stubs.

    Returns a list of recorded ``_systemctl`` invocations for the
    test to assert against.
    """
    monkeypatch.setattr(daemon_mod, "detect_legacy_coordd",
                         lambda: legacy)
    monkeypatch.setattr(daemon_mod, "install_template_unit",
                         lambda: wrote_unit)
    monkeypatch.setattr(daemon_mod, "register_project",
                         lambda *a, **kw: None)

    calls: list = []

    def fake_systemctl(*args):
        calls.append(list(args))
        # ``enable`` returns the test-specified rc; everything else
        # (daemon-reload, is-active, etc.) succeeds by default.
        rc = enable_rc if "enable" in args else 0
        return subprocess.CompletedProcess(
            args=("systemctl", *args), returncode=rc,
            stdout="", stderr=(
                "fake-enable-stderr" if rc != 0 else ""),
        )
    monkeypatch.setattr(daemon_mod, "_systemctl", fake_systemctl)
    return calls


# ---------- daemon install enables the unit ----------


def test_install_invokes_systemctl_enable_with_correct_unit(
    tmp_path: Path, monkeypatch,
) -> None:
    """0307: ``greatminds daemon install`` MUST call
    ``systemctl --user enable greatminds-daemon@my-fleet.service``."""
    _project(tmp_path, monkeypatch)
    calls = _stub_helpers(monkeypatch)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 0, result.output

    enable_calls = [c for c in calls if c and c[0] == "enable"]
    assert len(enable_calls) == 1, (
        f"0307: expected exactly one `systemctl enable` call; "
        f"got {calls!r}"
    )
    assert enable_calls[0][1].endswith("@my-fleet.service")


def test_install_reports_enable_failure_as_warning(
    tmp_path: Path, monkeypatch,
) -> None:
    """If ``systemctl enable`` returns nonzero, install must NOT
    fail (the unit is still written + project registered); it must
    surface the failure as a warning so the operator sees it."""
    _project(tmp_path, monkeypatch)
    _stub_helpers(monkeypatch, enable_rc=1)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 0
    out = (result.output or "") + (
        str(result.exception) if result.exception else ""
    )
    assert "may not restart after logout" in out \
        or "failed" in out.lower()


def test_install_legacy_coordd_blocks_before_enable(
    tmp_path: Path, monkeypatch,
) -> None:
    """Regression net: when the legacy ``coordd.service`` is
    detected, install aborts BEFORE the new enable step. Otherwise
    the operator would silently switch over without the migrate
    step."""
    _project(tmp_path, monkeypatch)
    calls = _stub_helpers(monkeypatch, legacy=True)

    result = CliRunner().invoke(daemon_mod.daemon, ["install"])
    assert result.exit_code == 2
    assert not any(c and c[0] == "enable" for c in calls), (
        "0307: legacy-detected branch must NOT reach enable"
    )


def test_install_passes_resolved_name_from_coord_yaml(
    tmp_path: Path, monkeypatch,
) -> None:
    """The instance unit name comes from coord.yaml's ``session``
    key when ``--name`` isn't passed. Pin the resolution path so a
    future refactor doesn't accidentally enable the wrong unit."""
    _project(tmp_path, monkeypatch)
    calls = _stub_helpers(monkeypatch)
    CliRunner().invoke(daemon_mod.daemon, ["install"])
    enable_call = next(c for c in calls if c[0] == "enable")
    assert "my-fleet" in enable_call[1]


def test_install_overrides_name_via_flag(
    tmp_path: Path, monkeypatch,
) -> None:
    """``--name`` overrides coord.yaml's session for the enable
    target (operators with multi-name aliases)."""
    _project(tmp_path, monkeypatch)
    calls = _stub_helpers(monkeypatch)
    CliRunner().invoke(daemon_mod.daemon,
                        ["install", "--name", "alt-fleet"])
    enable_call = next(c for c in calls if c[0] == "enable")
    assert "alt-fleet" in enable_call[1]
    assert "my-fleet" not in enable_call[1]


# ---------- daemon repair: one-shot enable for pre-0307 fleets ----------


def test_repair_invokes_enable_for_existing_install(
    tmp_path: Path, monkeypatch,
) -> None:
    """0307 ``daemon repair`` is the one-shot fix for fleets that
    installed before this task landed. Must run
    ``systemctl --user enable <instance>`` and exit 0."""
    _project(tmp_path, monkeypatch)
    calls = _stub_helpers(monkeypatch)

    result = CliRunner().invoke(daemon_mod.daemon, ["repair"])
    assert result.exit_code == 0, result.output
    enable_calls = [c for c in calls if c and c[0] == "enable"]
    assert len(enable_calls) == 1
    assert "my-fleet" in enable_calls[0][1]


def test_repair_propagates_nonzero_systemctl_exit(
    tmp_path: Path, monkeypatch,
) -> None:
    """If the underlying ``systemctl enable`` fails, ``daemon
    repair`` exits nonzero so the operator sees the failure (as
    opposed to ``install`` which logs-and-continues because other
    work happened first)."""
    _project(tmp_path, monkeypatch)
    _stub_helpers(monkeypatch, enable_rc=2)

    result = CliRunner().invoke(daemon_mod.daemon, ["repair"])
    assert result.exit_code != 0
    out = (result.output or "") + (
        str(result.exception) if result.exception else "")
    assert "systemctl" in out.lower() or "enable" in out.lower()


def test_repair_rejects_missing_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """coord.yaml without a ``session`` key AND no ``--name`` →
    exit 2 with the missing-name error (same diagnostic install
    surfaces)."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "coord.yaml").write_text(
        yaml.safe_dump({"other": "x"}), encoding="utf-8")
    monkeypatch.chdir(project)
    _stub_helpers(monkeypatch)

    result = CliRunner().invoke(daemon_mod.daemon, ["repair"])
    assert result.exit_code == 2
