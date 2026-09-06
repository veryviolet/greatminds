"""Suite-wide pytest configuration.

0175: integration tests for ``greatminds setup`` exercise ``setup_main``
end-to-end. Without a guard, that path would shell out to the real
``claude plugin install`` for every curated plugin and mutate the test
host's plugin registry. The env-var below tells setup.py to skip that
helper outright. Unit tests that exercise the helper itself (in
``tests/cli/test_plugin_install_marketplace_0175.py``) monkeypatch
``subprocess.run`` directly and unset the var locally.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _suppress_claude_plugin_install(monkeypatch):
    """0175: skip real ``claude plugin install`` calls from setup_main."""
    monkeypatch.setenv("GREATMINDS_SKIP_PLUGIN_INSTALL", "1")


@pytest.fixture(autouse=True)
def _isolate_daemon_user_state(tmp_path, monkeypatch):
    """Exercise real daemon file writers without touching the operator's host.

    Keep this suite-wide: setup, update, restart, and install all reach these
    helpers. Individual tests can override the paths and set synthetic auth
    values after this fixture; inherited credentials must never enter fixtures.
    """
    from greatminds.cli import daemon

    config = tmp_path / "user-config"
    registry = config / "greatminds"
    monkeypatch.setattr(daemon, "REGISTRY_DIR", registry)
    monkeypatch.setattr(daemon, "REGISTRY_PATH", registry / "projects.json")
    monkeypatch.setattr(daemon, "AGENT_ENV_DIR", registry / "agent-env")
    monkeypatch.setattr(daemon, "SYSTEMD_USER_DIR", config / "systemd" / "user")
    for name in daemon.AGENT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in ("GREATMINDS_RUN_ID", "GREATMINDS_RUN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
