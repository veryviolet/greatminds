"""GitHub issue #13: a systemd-side coordd daemon restart inherited the
bare systemd --user PATH and could no longer exec claude / codex /
ansible-playbook — driven turns and stand deploys broke.

Root cause: ``greatminds daemon restart`` (the choke point ``update`` and
the restart CLI both funnel through) called ``systemctl --user restart``
without re-rendering the on-disk unit. A unit installed before PATH-baking
(pre-1.6.3) — or rendered when a tool was not yet resolvable — carried no
``Environment=PATH=`` and restarted coordd with systemd's bare PATH.

Fix: ``restart_cmd`` now refreshes (re-renders + rewrites) the on-disk
units before the restart so the baked PATH is always current. The install
helpers are idempotent, so a current unit is a no-op.
"""
from __future__ import annotations

from pathlib import Path

from greatminds.cli import daemon as daemon_mod


def test_template_body_bakes_path_environment() -> None:
    """Regression guard: the composed template unit must carry a baked
    ``Environment=PATH=`` line (the per-restart refresh re-applies it)."""
    body = daemon_mod._template_unit_body()
    assert "Environment=PATH=" in body, (
        "issue #13: the daemon unit must bake a deterministic PATH so a "
        "restart can exec claude / codex / ansible"
    )
    # baked PATH must include the standard system dirs at minimum
    path_line = next(l for l in body.splitlines()
                     if l.startswith("Environment=PATH="))
    assert "/usr/bin" in path_line


def _isolate(monkeypatch, tmp_path: Path) -> list[tuple]:
    """Point SYSTEMD_USER_DIR at a tmp dir, record _systemctl calls, and
    isolate the refresh to the template unit (no project resolved → the
    drop-in / app-server branches are skipped)."""
    sysd = tmp_path / "systemd-user"
    sysd.mkdir()
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", sysd)
    calls: list[tuple] = []
    monkeypatch.setattr(daemon_mod, "_systemctl",
                        lambda *a: calls.append(a) or _Done())
    monkeypatch.setattr(daemon_mod, "lookup_project_dir", lambda name: None)
    return calls


class _Done:
    returncode = 0
    stderr = ""
    stdout = ""


def test_refresh_rewrites_stale_unit_lacking_path(monkeypatch, tmp_path) -> None:
    """A stale on-disk unit with NO Environment=PATH= is rewritten to a
    body that has it; refresh returns True and daemon-reloads."""
    calls = _isolate(monkeypatch, tmp_path)
    dest = daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME
    # simulate a pre-PATH (pre-1.6.3) installed unit
    dest.write_text(
        "[Unit]\nDescription=greatminds coordination daemon for project %i\n"
        "After=default.target\n\n[Service]\nType=simple\n"
        "ExecStart=/old/bin/greatminds coordd --project %i\n"
        "Restart=always\nRestartSec=2\n\n[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    assert "Environment=PATH=" not in dest.read_text(encoding="utf-8")

    changed = daemon_mod._refresh_units_before_restart("proj", None)

    assert changed is True
    assert "Environment=PATH=" in dest.read_text(encoding="utf-8"), (
        "issue #13: a stale unit must be re-baked with PATH on restart"
    )
    assert ("daemon-reload",) in calls, "must daemon-reload after a rewrite"


def test_refresh_noop_when_unit_current(monkeypatch, tmp_path) -> None:
    """When the on-disk unit already matches the freshly-rendered body,
    refresh is a no-op: returns False and does NOT daemon-reload."""
    calls = _isolate(monkeypatch, tmp_path)
    dest = daemon_mod.SYSTEMD_USER_DIR / daemon_mod.TEMPLATE_UNIT_NAME
    dest.write_text(daemon_mod._template_unit_body(), encoding="utf-8")

    changed = daemon_mod._refresh_units_before_restart("proj", None)

    assert changed is False
    assert ("daemon-reload",) not in calls, (
        "a current unit must not trigger a gratuitous daemon-reload"
    )
