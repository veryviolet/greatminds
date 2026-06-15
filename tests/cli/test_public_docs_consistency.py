"""Public docs surfaces must describe the current CLI/auth contracts."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import click
from click.testing import CliRunner

from greatminds.cli.main import cli


ROOT = Path(__file__).resolve().parents[2]
HELP_HISTORICAL_MARKERS = re.compile(
    r"\b(legacy|deprecated|obsolete|historical|retired)\b|"
    r"\bpre-[0-9]|\bpost-[0-9]|"
    r"\b0\.1\.x\b|\b1\.3\.0\b|\b1\.5\.0\b|\b1\.6\.0\b|"
    r"\b0(1[0-9][0-9]|2[0-9][0-9]|3[0-9][0-9])\b"
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _help(*args: str) -> str:
    result = CliRunner().invoke(cli, [*args, "--help"], catch_exceptions=False)
    assert result.exit_code == 0
    return result.output


def _command_paths(command: click.Command, prefix: tuple[str, ...] = ()):
    yield prefix
    if isinstance(command, click.Group):
        for name, subcommand in sorted(command.commands.items()):
            yield from _command_paths(subcommand, (*prefix, name))


def test_public_cli_help_has_no_retired_entrypoints_or_stand_request() -> None:
    surfaces = {
        " ".join(path) if path else "root": _help(*path)
        for path in _command_paths(cli)
    }
    banned = [
        "greatminds-start-agent",
        "greatminds-pty-launch",
        "greatminds-*",
        "greatminds stand request",
        "stand_request stream",
        "stand result",
        "~/.codex/<role-lower>.config.toml",
        "--profile <role-lower>",
        "CODEX_HOME=<project>/coordination/.codex-home",
    ]
    offenders: list[str] = []
    for name, text in surfaces.items():
        for needle in banned:
            if needle in text:
                offenders.append(f"{name}: {needle}")
        for line in text.splitlines():
            if HELP_HISTORICAL_MARKERS.search(line):
                offenders.append(f"{name}: {line.rstrip()}")
            if CYRILLIC.search(line):
                offenders.append(f"{name}: {line.rstrip()}")
    assert not offenders, "public help text must describe only current CLI:\n" + "\n".join(offenders)


def test_package_summary_matches_current_codex_auth_contract() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    desc = data["project"]["description"]
    assert "per-role CODEX_HOME" not in desc
    assert "driven roles" in desc
    assert data["project"]["urls"]["Documentation"] == (
        "https://veryviolet.github.io/greatminds/"
    )
