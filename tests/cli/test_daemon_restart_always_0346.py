"""Tests for task 0346: the coordd systemd unit must use Restart=always
so a killed coordd is resurrected.

EXPLORER killed greatminds-daemon@greatminds-dev with SIGTERM; systemd
reported exit status=0/SUCCESS and did NOT restart it (Restart=on-failure
only restarts on FAILURE, but coordd's loop catches the signal and exits
cleanly). The fleet then had no wake/drive daemon until a manual restart.
Restart=always resurrects coordd after any external kill or crash, while
a deliberate `systemctl --user stop` is still honoured (systemd never
auto-restarts a commanded stop).
"""
from __future__ import annotations

from greatminds.cli import daemon as daemon_mod
from greatminds.core.paths import find_canon_dir


def test_composed_unit_uses_restart_always() -> None:
    body = daemon_mod._template_unit_body()
    assert "Restart=always" in body
    assert "Restart=on-failure" not in body
    # RestartSec retained so it doesn't hot-loop
    assert "RestartSec=" in body


def test_shipped_canon_unit_uses_restart_always() -> None:
    unit = (find_canon_dir() / "systemd"
            / daemon_mod.TEMPLATE_UNIT_NAME).read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "Restart=on-failure" not in unit


def test_inline_fallback_uses_restart_always(monkeypatch) -> None:
    """When the canon file is missing, the inline fallback body must also
    carry Restart=always (not the old on-failure)."""
    # point find_canon_dir (as imported into daemon) at a dir with no
    # systemd/ template → forces the inline fallback branch
    import tempfile
    from pathlib import Path
    empty = Path(tempfile.mkdtemp())
    monkeypatch.setattr(daemon_mod, "find_canon_dir", lambda: empty)
    body = daemon_mod._template_unit_body()
    assert "Restart=always" in body
    assert "Restart=on-failure" not in body
    assert "coordd --project %i" in body
