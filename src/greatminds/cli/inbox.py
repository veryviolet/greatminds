#!/usr/bin/env python3
"""greatminds inbox — structured inter-role messaging.

Replaces ad-hoc writes to ``coordination/inbox/<role>/``. Each message is
a YAML file with required fields (``to_role``, ``from_role``, ``kind``,
``task_ref``, ``sent_at``) and a constrained ``body`` for free text.

Kinds:
  wake   please tick now (default reason: "wake request")
  ask    question expecting a reply via reply.yaml under same dir
  info   FYI, no action expected

Subcommands:
  send   write a new message
  list   list pending messages for a role (default: caller)
  show   print one message
  ack    mark a message processed (renames to ``processed-<orig>``)

Caller role from ``$GREATMINDS_ROLE`` (no ``--as`` override). The script
touches the caller's heartbeat and appends a journal entry on every
successful send.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import click
import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import caller_role as _bare_caller_role
from greatminds.core.paths import find_canon_dir, find_coord_dir
from greatminds.core.util import now_iso

KINDS = {"wake", "ask", "info"}


_schema_cache: dict | None = None


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache
    try:
        _schema_cache = yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GreatMindsError(f"schema.yaml: {exc}")
    return _schema_cache


def known_roles() -> set:
    return set((_schema().get("roles") or {}).keys())


def caller_role() -> str:
    """Resolved caller role, validated against ``schema.roles``."""
    role = _bare_caller_role()
    if role not in known_roles():
        raise GreatMindsError(f"unknown role: {role}")
    return role


def touch_heartbeat(coord: Path, role: str) -> None:
    try:
        p = coord / f"heartbeat.{role.lower()}"
        p.touch()
        os.utime(p, None)
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


@click.group(help="inter-role inbox messaging (wake/ask/info)")
def inbox() -> None:
    pass


@inbox.command(name="send")
@click.argument("to")
@click.option("--kind", required=True, type=click.Choice(sorted(KINDS)))
@click.option("--task", default=None, help="task id ref")
@click.option("--body", default=None, help="literal | @file | - (stdin)")
def inbox_send(to, kind, task, body) -> None:
    coord = find_coord_dir()
    from_role = caller_role()
    to_role = to.upper()
    if to_role not in known_roles():
        raise GreatMindsError(f"unknown destination role: {to_role}")

    body_text = read_body(body) if body else ""
    if len(body_text) > 50_000:
        raise GreatMindsError("body too large (>50KB)", exit_code=2)

    msg = {
        "to_role": to_role,
        "from_role": from_role,
        "kind": kind,
        "task_ref": task or "",
        "sent_at": now_iso(),
        "answered_at": None,
        "body": body_text,
    }

    fname = (
        f"{kind}-{int(time.time())}-{task[:40]}"
        if task else
        f"{kind}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    )
    fname += ".yaml"

    target = coord / "inbox" / to_role.lower() / fname
    atomic_write_yaml(target, msg)
    journal_append(coord, {
        "t": now_iso(),
        "actor": from_role,
        "task": task or "",
        "from": "inbox",
        "to": f"inbox/{to_role.lower()}",
        "reason": f"{kind}: {(body_text[:80] or '').strip()}",
        "intent_id": "",
    })
    touch_heartbeat(coord, from_role)
    click.echo(f"sent {target}")


@inbox.command(name="list")
@click.argument("role", required=False)
def inbox_list(role) -> None:
    coord = find_coord_dir()
    role_l = (role or caller_role()).lower()
    # Heartbeat refresh on read-only list — keeps role liveness fresh
    # during long idle stretches.
    try:
        touch_heartbeat(coord, role_l.upper())
    except OSError:
        pass
    box = coord / "inbox" / role_l
    if not box.is_dir():
        click.echo(f"(no inbox dir for {role_l})")
        return
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
        click.echo(f"  {age:5}s  {size:6}B  {f.name}")
    if not pending:
        click.echo("(empty)")


@inbox.command(name="show")
@click.argument("path")
def inbox_show(path) -> None:
    p = Path(path)
    if not p.is_file():
        coord = find_coord_dir()
        role = caller_role().lower()
        cand = coord / "inbox" / role / path
        if cand.is_file():
            p = cand
        else:
            raise GreatMindsError(f"{path} not found")
    click.echo(p.read_text(encoding="utf-8"))


@inbox.command(name="ack")
@click.argument("path")
def inbox_ack(path) -> None:
    coord = find_coord_dir()
    role = caller_role().lower()
    p = Path(path)
    if not p.is_file():
        cand = coord / "inbox" / role / path
        if cand.is_file():
            p = cand
        else:
            raise GreatMindsError(f"{path} not found")
    if p.parent.name != role:
        raise GreatMindsError(
            f"can't ack message in {p.parent.name}'s inbox as {role}",
            exit_code=3,
        )
    new = p.with_name(f"processed-{p.name}")
    try:
        os.rename(p, new)
    except OSError as exc:
        raise GreatMindsError(f"ack rename failed: {exc}", exit_code=4)
    touch_heartbeat(coord, role.upper())
    click.echo(f"acked {new.name}")


if __name__ == "__main__":
    inbox()
