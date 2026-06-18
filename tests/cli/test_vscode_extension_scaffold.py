"""Smoke tests for the bundled VS Code extension scaffold."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "vscode-extension"


def test_vscode_extension_package_declares_cockpit_commands() -> None:
    package = json.loads((EXT / "package.json").read_text(encoding="utf-8"))

    assert package["name"] == "greatminds"
    assert package["main"] == "./extension.js"
    commands = {
        item["command"]
        for item in package["contributes"]["commands"]
    }
    assert {
        "greatminds.refresh",
        "greatminds.openDashboard",
        "greatminds.openDrivenLog",
        "greatminds.openCoordd",
        "greatminds.showAgentTools",
        "greatminds.showStandStatus",
    } <= commands


def test_vscode_extension_uses_cli_backend() -> None:
    text = (EXT / "extension.js").read_text(encoding="utf-8")

    assert "execFile(cliPath()" in text
    assert "GREATMINDS_PROJECT_DIR" in text
    assert "agent\", \"tools\", \"--json" in text
    assert "driven-log" in text
    assert "stand\", \"status" in text
    assert ".greatminds" not in text


def test_vscode_extension_assets_exist() -> None:
    assert (EXT / "README.md").is_file()
    assert (EXT / "media" / "icon.svg").is_file()
