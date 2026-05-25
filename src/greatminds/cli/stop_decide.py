"""Hook helper: decide whether the agent should keep ticking instead of stopping.

Invoked from claude's ``Stop`` hook and cursor's ``stop``/``subagentStop``
hooks. Receives JSON on stdin (mostly ignored). Looks at:

  - the role's inbox (anything pending?),
  - (optional) the role's ``claims_from`` queues.

If there is wake work, prints a JSON blob telling the host to continue
the loop. Otherwise prints ``{}`` (allow stop).

Output format depends on the host:

  claude   ``{"decision": "block", "reason": "<text>", "systemMessage": "<text>"}``
  cursor   ``{"followup_message": "<text>"}``

Exit code is always 0; the JSON on stdout is the signal the host reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir


def resolve_coord(project_dir: Path) -> Path:
    if project_dir.name == "coordination" and (project_dir / "journal.ndjson").is_file():
        return project_dir
    return project_dir / "coordination"


def load_schema(canon_dir: Path) -> dict:
    p = canon_dir / "schema.yaml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def inbox_pending(coord: Path, role: str) -> list[str]:
    """Unprocessed inbox messages for ``role``.

    Surfaces both ``.md`` (daemon journal-notify wakes) and ``.yaml``
    (role-to-role ``greatminds inbox send`` asks/infos). 0143 fix:
    iter-1 only matched ``.md``, so direct role-to-role asks and infos
    written as ``.yaml`` never reached the claude stop-hook — the
    claude-host PLANNER stayed idle after EXPLORER's
    ``greatminds inbox send ARCHITECT-PLANNER --kind info``. Loop-mode
    hosts (codex/cursor) were unaffected because their tick uses an
    FS-watch over the whole inbox dir.

    Excludes ``.gitkeep`` (dir-tracker stub) and ``processed-*`` files
    (acked breadcrumbs renamed by ``greatminds inbox ack``).
    """
    inbox = coord / "inbox" / role.lower()
    if not inbox.is_dir():
        return []
    out: list[str] = []
    for f in sorted(inbox.iterdir()):
        if not f.is_file():
            continue
        if f.suffix not in (".md", ".yaml"):
            continue
        if f.name == ".gitkeep" or f.name.startswith("processed-"):
            continue
        out.append(f.name)
    return out


@click.command(name="stop-decide",
               short_help="Stop-hook helper: emit block/allow JSON",
               help=__doc__)
@click.argument("role")
@click.option("--host", type=click.Choice(["claude", "cursor"]), default="claude",
              help="output schema (claude vs cursor)")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
def stop_decide(role: str, host: str, project_dir: Path | None,
                canon_dir: Path | None) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    # Drain stdin so hosts that pipe JSON payload don't get EPIPE.
    try:
        sys.stdin.read()
    except Exception:
        pass

    coord = resolve_coord(project_dir)
    inbox_msgs = inbox_pending(coord, role)
    # schema is loaded only for future-proofing of claim-queue checks;
    # the live behaviour only blocks on inbox messages.
    _ = load_schema(canon_dir)

    if not inbox_msgs:
        click.echo("{}")
        return

    msg = f"continue your tick: inbox/{role.lower()}/ has {len(inbox_msgs)} pending inbox message(s)"
    if host == "claude":
        click.echo(json.dumps({
            "decision": "block",
            "reason": msg,
            "systemMessage": msg,
        }))
    else:
        click.echo(json.dumps({"followup_message": msg}))


if __name__ == "__main__":
    stop_decide()
