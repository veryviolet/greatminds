#!/usr/bin/env python3
"""Hook helper: decide whether the agent should keep ticking instead of stopping.

Invoked from claude `Stop` hook and cursor `stop`/`subagentStop` hook.
Receives JSON on stdin (the hook payload; we mostly ignore it). Looks at:

  - the role's inbox (anything pending?),
  - the role's claims_from queues (any tasks waiting?).

If there is work to do, prints a JSON blob telling the host to continue the
loop. Otherwise prints "{}" (allow stop).

Output format depends on the host:
  - claude:  {"decision": "block", "reason": "<text>"}
  - cursor:  {"followup_message": "<text>"}

Pass `--host claude` (default) or `--host cursor` to choose.

Usage:
    stop_decide <ROLE> [--host claude|cursor]
              [--project-dir <dir>] [--canon-dir <dir>]

Role is the same key used by render-role.

Exit code is always 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


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


def role_meta(schema: dict, role: str) -> dict:
    meta = (schema.get("roles") or {}).get(role)
    return meta if isinstance(meta, dict) else {}


def inbox_pending(coord: Path, role: str) -> list[str]:
    """Return list of UNPROCESSED inbox messages (wake-* .md).

    Excludes:
    - `.gitkeep` (the dir-tracker stub)
    - `processed-*` files (these are acked breadcrumbs renamed by
      `bin/inbox ack`; they MUST NOT count as pending or long-running
      agents enter an infinite false-positive stop-block loop).
    Only `.md` is checked because notify_from_journal writes wake-*.md;
    inter-role `.yaml` asks are slower-channel and don't block stop.
    """
    inbox = coord / "inbox" / role.lower()
    if not inbox.is_dir():
        return []
    out: list[str] = []
    for f in sorted(inbox.iterdir()):
        if not f.is_file():
            continue
        if f.suffix != ".md":
            continue
        if f.name == ".gitkeep":
            continue
        if f.name.startswith("processed-"):
            continue
        out.append(f.name)
    return out


def queue_pending(coord: Path, role: str, schema: dict) -> list[tuple[str, str]]:
    """(queue, task) pairs the role could claim."""
    out: list[tuple[str, str]] = []
    meta = role_meta(schema, role)
    claims = meta.get("claims_from") or []
    if not isinstance(claims, list):
        return out
    for q in claims:
        d = coord / q
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "_TEMPLATE.md":
                continue
            out.append((q, f.name))
    return out


def render_reason(role: str, inbox_msgs: list[str], queue_tasks: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    if inbox_msgs:
        parts.append(f"inbox/{role.lower()}/ has {len(inbox_msgs)} message(s)")
    if queue_tasks:
        sample = ", ".join(f"{q}/{t}" for q, t in queue_tasks[:3])
        more = "" if len(queue_tasks) <= 3 else f" (+{len(queue_tasks) - 3} more)"
        parts.append(f"claimable: {sample}{more}")
    return "; ".join(parts) or "no work"


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-stop-decide`` in pyproject.toml."""
    from greatminds.core.paths import find_canon_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role")
    parser.add_argument("--host", choices=["claude", "cursor"], default="claude")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--canon-dir",
        type=Path,
        default=None,
        help="canon data directory (default: packaged greatminds.data)",
    )
    args = parser.parse_args(argv)
    if args.canon_dir is None:
        args.canon_dir = find_canon_dir()

    # Drain stdin so hosts that pipe JSON don't get EPIPE.
    try:
        sys.stdin.read()
    except Exception:
        pass

    coord = resolve_coord(args.project_dir)
    schema = load_schema(args.canon_dir)

    # Only inbox messages count as "wake right now" signal. claims_from
    # queues may hold long-lived files (e.g. an open review_sessions/<id>
    # entry that the planner inspects every tick without moving) — using
    # those as "block stop" triggers caused an infinite loop where the
    # agent kept being blocked from idling because of a static file.
    inbox_msgs = inbox_pending(coord, args.role)

    if not inbox_msgs:
        print("{}")
        return 0

    msg = f"продолжай тик: inbox/{args.role.lower()}/ has {len(inbox_msgs)} new wake message(s)"
    if args.host == "claude":
        print(json.dumps({
            "decision": "block",
            "reason": msg,
            "systemMessage": msg,
        }))
    else:
        print(json.dumps({"followup_message": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
