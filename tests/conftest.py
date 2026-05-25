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

import os

import pytest


@pytest.fixture(autouse=True)
def _suppress_claude_plugin_install(monkeypatch):
    """0175: skip real ``claude plugin install`` calls from setup_main."""
    monkeypatch.setenv("GREATMINDS_SKIP_PLUGIN_INSTALL", "1")
