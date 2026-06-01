"""0288: ``greatminds role contract <ROLE>`` — render a role's
machine-readable contract from ``schema.roles.<ROLE>``.

The schema declares per-role ``responsibilities``,
``forbidden_actions``, and ``event_triggers`` (post-0288). This CLI
renders them as a compact summary an LLM-driven agent can ingest
into its prompt context at tick start.

Output format:

  ROLE: <NAME>
  Category: <category>
  Claims from: [<queue>, …]

  Responsibilities:
    - …
  Forbidden actions:
    - …
  Event triggers:
    on_<event>:
      1. step
      2. step
      …
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


def load_role_contract(canon_dir: Path, role: str
                        ) -> dict[str, Any]:
    """Return the schema's full role entry. Raises GreatMindsError
    if the role isn't declared."""
    try:
        doc = yaml.safe_load(
            (canon_dir / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(
            f"could not load schema.yaml: {exc}", exit_code=2,
        )
    roles = doc.get("roles") or {}
    entry = roles.get(role)
    if not isinstance(entry, dict):
        available = ", ".join(sorted(roles)) or "(none)"
        raise GreatMindsError(
            f"role {role!r} not in schema.roles. Available: {available}",
            exit_code=2,
        )
    return entry


def render_contract(role: str, entry: dict[str, Any]) -> str:
    """Plain-text render of one role's contract. Stable shape so
    tests can pin the output."""
    out: list[str] = []
    out.append(f"ROLE: {role}")
    cat = entry.get("category")
    if cat:
        out.append(f"Category: {cat}")
    # 0312: surface the lifecycle classification (interactive /
    # self-loop / driven) so the agent + operators see how the role
    # is woken.
    lifecycle = entry.get("lifecycle")
    if lifecycle:
        out.append(f"Lifecycle: {lifecycle}")
    claims = entry.get("claims_from") or []
    if claims:
        out.append(f"Claims from: {list(claims)}")
    else:
        out.append("Claims from: (event-driven; no claim queue)")
    out.append("")

    resp = entry.get("responsibilities") or []
    out.append("Responsibilities:")
    if resp:
        for r in resp:
            out.append(f"  - {r}")
    else:
        out.append("  (none declared)")
    out.append("")

    forb = entry.get("forbidden_actions") or []
    out.append("Forbidden actions:")
    if forb:
        for f in forb:
            out.append(f"  - {f}")
    else:
        out.append("  (none declared)")
    out.append("")

    triggers = entry.get("event_triggers") or {}
    out.append("Event triggers:")
    if triggers:
        for event, steps in triggers.items():
            out.append(f"  {event}:")
            if not steps:
                out.append("    (no steps)")
                continue
            for i, step in enumerate(steps, start=1):
                out.append(f"    {i}. {step}")
    else:
        out.append("  (none — interactive role)")
    return "\n".join(out)


@click.group(name="role",
             help="machine-readable role contracts (0288).")
def role() -> None:
    pass


@role.command(name="contract",
              help="render schema.roles.<ROLE> contract")
@click.argument("role_name")
@click.option("--canon-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None,
              help="canon dir override (default: packaged greatminds.data)")
def role_contract(role_name: str, canon_dir: Path | None) -> None:
    canon = canon_dir or find_canon_dir()
    entry = load_role_contract(canon, role_name)
    click.echo(render_contract(role_name, entry))


@role.command(name="list",
              help="list all declared roles")
@click.option("--canon-dir",
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None)
def role_list(canon_dir: Path | None) -> None:
    canon = canon_dir or find_canon_dir()
    try:
        doc = yaml.safe_load(
            (canon / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(
            f"could not load schema.yaml: {exc}", exit_code=2,
        )
    for name in sorted((doc.get("roles") or {}).keys()):
        click.echo(name)
