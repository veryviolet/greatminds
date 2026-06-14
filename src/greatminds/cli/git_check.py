"""greatminds check-git-permission — check git verb permissions by role.

Reads ``schema.yaml`` ``git_permissions`` and refuses if
``$GREATMINDS_ROLE`` is not in the allowed list for the given verb. This is an
explicit diagnostic command; ``greatminds setup`` does not install git hooks.

Usage::

    greatminds check-git-permission commit
    greatminds check-git-permission push

Exit codes:
    0  allowed (or GREATMINDS_ROLE is in git_permissions.<verb>)
    2  $GREATMINDS_ROLE unset or not allowed for this verb
    3  schema lookup failed (missing section or unreadable file)

Task 0091 item 2.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


def _load_git_permissions() -> dict:
    try:
        data = yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"schema.yaml: {exc}", exit_code=3)
    gp = data.get("git_permissions")
    if not isinstance(gp, dict):
        raise GreatMindsError(
            "schema.yaml is missing `git_permissions:` section (task 0091 item 2)",
            exit_code=3,
        )
    return gp


@click.command(
    name="check-git-permission",
    short_help="gate git commit/push by GREATMINDS_ROLE per schema.git_permissions",
    help=__doc__,
)
@click.argument("verb", type=click.Choice(["commit", "push"]))
def check_git_permission(verb: str) -> None:
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    gp = _load_git_permissions()
    allowed = gp.get(verb) or []
    if not isinstance(allowed, list):
        raise GreatMindsError(
            f"schema.git_permissions.{verb} must be a list; got {type(allowed).__name__}",
            exit_code=3,
        )
    if not role:
        click.echo(
            f"refusing git {verb}: $GREATMINDS_ROLE is unset. "
            f"Commits and pushes must be performed from an agent context "
            f"(allowed roles for `{verb}`: {allowed}).",
            err=True,
        )
        raise click.exceptions.Exit(2)
    if role not in allowed:
        click.echo(
            f"refusing git {verb}: role {role!r} is not in "
            f"schema.git_permissions.{verb} = {allowed}. "
            f"Only the listed role(s) may perform this verb.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    # Allowed — exit 0 silently so the hook lets git proceed.


if __name__ == "__main__":
    check_git_permission()
