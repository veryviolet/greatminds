"""Shared click colour palette + small output helpers.

Palette (per the project's CLI style spec):

  base info       cyan
  headers         cyan + bold
  success         bright_green
  error           bright_red
  warning         yellow

All four helpers respect the standard ``NO_COLOR`` env var (click does
this for us via ``--no-color`` and TTY detection; ``click.secho``
auto-strips ANSI when output is piped).
"""

from __future__ import annotations

import sys

import click


def info(msg: str = "", **kwargs) -> None:
    """Plain info — cyan."""
    click.secho(msg, fg="cyan", **kwargs)


def header(msg: str, **kwargs) -> None:
    """Section header — cyan + bold."""
    click.secho(msg, fg="cyan", bold=True, **kwargs)


def ok(msg: str, **kwargs) -> None:
    """Success line — bright green."""
    click.secho(msg, fg="bright_green", **kwargs)


def warn(msg: str, **kwargs) -> None:
    """Warning — yellow."""
    click.secho(msg, fg="yellow", err=True, **kwargs)


def err(msg: str, **kwargs) -> None:
    """Error to stderr — bright red."""
    click.secho(msg, fg="bright_red", err=True, **kwargs)


def fail(msg: str, exit_code: int = 1) -> None:
    """Print error + exit. Mirrors ``greatminds.core.util.die`` but uses
    the click colour scheme."""
    err(msg)
    sys.exit(exit_code)
