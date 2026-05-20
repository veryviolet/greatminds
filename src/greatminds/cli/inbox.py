#!/usr/bin/env python3
"""bin/inbox — structured inter-role messaging.

Replaces ad-hoc writes to coordination/inbox/<role>/. Each message is a
YAML file with required fields (to_role, from_role, kind, task_ref,
sent_at) and a constrained `body` for free text.

kinds:
  wake   — please tick now (default reason: 'wake request')
  ask    — question expecting a reply via reply.yaml under same dir
  info   — FYI, no action expected

Subcommands:
  send   write a new message
  list   list pending messages for a role (default: caller)
  show   print one message
  ack    mark a message processed (renames to processed-<orig>)

Caller role from $COORD_ROLE (no --as override). The script touches
the caller's heartbeat and appends a journal entry on every successful
send.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import yaml

from greatminds.core.paths import find_canon_dir, find_coord_dir
from greatminds.core.paths import caller_role as _bare_caller_role
from greatminds.core.util import die, now_iso  # noqa: F401

KINDS = {"wake", "ask", "info"}


_schema = None


def schema() -> dict:
    global _schema
    if _schema is not None:
        return _schema
    try:
        _schema = yaml.safe_load((find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        die(1, f"schema.yaml: {exc}")
    return _schema


def known_roles() -> set:
    return set((schema().get("roles") or {}).keys())


def caller_role() -> str:
    """``core.paths.caller_role()`` + schema-validation against ``roles``."""
    role = _bare_caller_role()
    if role not in known_roles():
        die(1, f"unknown role: {role}")
    return role


def touch_heartbeat(coord: Path, role: str) -> None:
    try:
        (coord / f"heartbeat.{role.lower()}").touch()
        os.utime(coord / f"heartbeat.{role.lower()}", None)
    except OSError:
        pass


def journal_append(coord: Path, entry: dict) -> None:
    try:
        with (coord / "journal.ndjson").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_body(spec: str) -> str:
    if spec == "-":
        return sys.stdin.read()
    if spec.startswith("@"):
        return Path(spec[1:]).read_text(encoding="utf-8")
    return spec


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_send(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    from_role = caller_role()
    to_role = args.to.upper()
    if to_role not in known_roles():
        die(1, f"unknown destination role: {to_role}")
    if args.kind not in KINDS:
        die(1, f"--kind must be one of {sorted(KINDS)}")

    body = read_body(args.body) if args.body else ""
    if len(body) > 50_000:
        die(2, "body too large (>50KB)")

    msg = {
        "to_role": to_role,
        "from_role": from_role,
        "kind": args.kind,
        "task_ref": args.task or "",
        "sent_at": now_iso(),
        "answered_at": None,
        "body": body,
    }

    fname = f"{args.kind}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    if args.task:
        fname = f"{args.kind}-{int(time.time())}-{args.task[:40]}"
    fname += ".yaml"

    target = coord / "inbox" / to_role.lower() / fname
    atomic_write_yaml(target, msg)
    journal_append(coord, {
        "t": now_iso(),
        "actor": from_role,
        "task": args.task or "",
        "from": "inbox",
        "to": f"inbox/{to_role.lower()}",
        "reason": f"{args.kind}: {(body[:80] or '').strip()}",
        "intent_id": "",
    })
    touch_heartbeat(coord, from_role)
    print(f"sent {target}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    role = (args.role or caller_role()).lower()
    box = coord / "inbox" / role
    if not box.is_dir():
        print(f"(no inbox dir for {role})")
        return 0
    pending = sorted(
        f for f in box.iterdir()
        if f.suffix in (".yaml", ".md")
        and not f.name.startswith("processed-")
        and not f.name.startswith(".")
        and f.name != ".gitkeep"
    )
    for f in pending:
        size = f.stat().st_size
        age = int(time.time() - f.stat().st_mtime)
        print(f"  {age:5}s  {size:6}B  {f.name}")
    if not pending:
        print(f"(empty)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    p = Path(args.path)
    if not p.is_file():
        coord = find_coord_dir()
        # try ./inbox/<caller>/<name>
        role = caller_role().lower()
        cand = coord / "inbox" / role / args.path
        if cand.is_file():
            p = cand
        else:
            die(1, f"{args.path} not found")
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    role = caller_role().lower()
    p = Path(args.path)
    if not p.is_file():
        cand = coord / "inbox" / role / args.path
        if cand.is_file():
            p = cand
        else:
            die(1, f"{args.path} not found")
    if p.parent.name != role:
        die(3, f"can't ack message in {p.parent.name}'s inbox as {role}")
    new = p.with_name(f"processed-{p.name}")
    try:
        os.rename(p, new)
    except OSError as exc:
        die(4, f"ack rename failed: {exc}")
    touch_heartbeat(coord, role.upper())
    print(f"acked {new.name}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="inbox", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("send", help="send a message")
    ps.add_argument("to", help="destination role")
    ps.add_argument("--kind", required=True, choices=sorted(KINDS))
    ps.add_argument("--task", help="task id ref")
    ps.add_argument("--body", help="literal | @file | - (stdin)")
    ps.set_defaults(func=cmd_send)

    pl = sub.add_parser("list", help="list pending messages")
    pl.add_argument("role", nargs="?", help="role (default: caller)")
    pl.set_defaults(func=cmd_list)

    psh = sub.add_parser("show", help="print one message")
    psh.add_argument("path")
    psh.set_defaults(func=cmd_show)

    pa = sub.add_parser("ack", help="mark message processed")
    pa.add_argument("path")
    pa.set_defaults(func=cmd_ack)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-inbox`` in pyproject.toml."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
