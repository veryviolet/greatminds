#!/usr/bin/env python3
"""greatminds stand — stand_request stream wrapper around task.

Two subcommands:

  request   create a new stand_request task in coordination/stand_requests/.
            validates that each id in --evidence-for actually exists in
            an active queue (a task that will benefit from the run).
            On success: claim by STAND-KEEPER picks it up automatically.

  result    append stand_result block to a stand_request already in
            stand_wip/, then mv to stand_done/. STAND-KEEPER only.
            Validates result / stand_status / profile enums.

Caller role from ``$GREATMINDS_ROLE`` (no ``--as`` override).

Implementation note: this module calls into ``greatminds.cli.task`` via
direct function imports (``create_task``, ``move_task``, ``append_block``)
— no subprocess between modules of the same package.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir
from greatminds.cli.task import (
    _split_multivalue,
    append_block,
    create_task,
    move_task,
)


# Queues where a product task can still be "active" (i.e. wanting evidence).
# Active queues that a stand's --evidence-for argument may reference.
# 0149: ``review_sessions`` joined the allow-list. The product pipeline
# queues are the original set (a stand records evidence for in-flight
# work); review-session tasks (e.g. ``0007-explorer-...``) are also
# legitimate evidence targets — STAND-KEEPER's run of a smoke/deploy
# stand is evidence that EXPLORER's scenario found nothing fresh, and
# the pre-0149 validator rejected them as ``not in any active queue``.
EVIDENCE_FOR_ACTIVE_QUEUES = (
    "feature_inbox",
    "feature_plan",
    "feature_dev",
    "feature_ui_dev",
    "feature_docs",
    "feature_test",
    "feature_docs_review",
    "feature_review",
    "feature_blocked",
    "review_sessions",
)


def task_exists_in_active(coord: Path, task_id: str) -> bool:
    """``True`` if a task with this id (or id-prefix) sits in any active queue."""
    for q in EVIDENCE_FOR_ACTIVE_QUEUES:
        for ext in (".yaml", ".md"):
            if (coord / q / f"{task_id}{ext}").is_file():
                return True
        qd = coord / q
        if qd.is_dir():
            for f in qd.iterdir():
                if f.is_file() and (
                    f.stem == task_id or f.stem.startswith(task_id + "-")
                ):
                    return True
    return False


_REQUEST_TYPES = ["deploy", "restart", "rebuild", "smoke",
                  "remote_sync", "gpu_check", "teardown"]
_PROFILES = ["full-deploy", "vite-dev"]


@click.group(help="stand_request stream — request a stand op, record result")
def stand() -> None:
    pass


@stand.command(name="request")
@click.option("--request-type", "request_type", required=True,
              type=click.Choice(_REQUEST_TYPES))
@click.option("--profile", required=True, type=click.Choice(_PROFILES))
@click.option("--title", required=True)
@click.option("--hosts", multiple=True, callback=_split_multivalue,
              help="list of hosts; repeat the flag or comma-separate values")
@click.option("--evidence-for", "evidence_for", multiple=True,
              callback=_split_multivalue,
              help="task ids that will use this stand's evidence; "
                   "repeat or comma-separate")
@click.option("--description", default=None, help="literal | @file | -")
@click.option("--priority", default=None,
              type=click.Choice(["low", "normal", "high"]))
@click.option("--reason", default=None, help="journal reason")
def stand_request(request_type, profile, title, hosts, evidence_for,
                  description, priority, reason) -> None:
    coord = find_coord_dir()

    if evidence_for:
        missing = [tid for tid in evidence_for
                   if not task_exists_in_active(coord, tid)]
        if missing:
            raise GreatMindsError(
                f"evidence-for ids not in any active queue: {missing}",
                exit_code=2,
            )

    target_path = create_task(
        stream="stand",
        title=title,
        request_type=request_type,
        profile=profile,
        hosts=hosts,
        evidence_for=evidence_for,
        description=description,
        priority=priority,
        reason=reason,
    )
    click.echo(f"created {target_path}")


@stand.command(name="result")
@click.argument("task_id", metavar="ID")
@click.option("--result", required=True,
              type=click.Choice(["ok", "partial", "fail"]))
@click.option("--status", required=True,
              type=click.Choice(["READY", "DEGRADED", "DOWN", "BLOCKED"]))
@click.option("--commit", required=True)
@click.option("--profile", required=True, type=click.Choice(_PROFILES))
@click.option("--notes", default=None, help="literal | @file | -")
@click.option("--reason", default=None, help="journal reason for mv")
def stand_result(task_id, result, status, commit, profile,
                 notes, reason) -> None:
    coord = find_coord_dir()

    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        raise GreatMindsError(
            "only STAND-KEEPER may produce stand_result", exit_code=3
        )

    wip = coord / "stand_wip"
    in_wip = (
        (wip / f"{task_id}.yaml").is_file()
        or (wip / f"{task_id}.md").is_file()
        or any(
            f.stem == task_id or f.stem.startswith(task_id + "-")
            for f in wip.glob("*.yaml")
        )
        or any(
            f.stem == task_id or f.stem.startswith(task_id + "-")
            for f in wip.glob("*.md")
        )
    )
    if not in_wip:
        raise GreatMindsError(f"task {task_id} not in stand_wip/")

    append_block(
        task_id=task_id,
        kind="stand_result",
        fields={
            "result": result,
            "stand_status": status,
            "commit": commit,
            "profile": profile,
        },
        body=notes,
    )
    move_task(task_id=task_id, to_queue="stand_done", reason=reason)
    click.echo(f"recorded stand_result and moved {task_id} → stand_done")


if __name__ == "__main__":
    stand()
