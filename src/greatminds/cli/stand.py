#!/usr/bin/env python3
"""bin/stand — stand_request stream wrapper around bin/task.

Two subcommands:

  request   create a new stand_request in coordination/stand_requests/.
            validates that each id in --evidence-for actually exists in
            an active queue (a task that will benefit from the run).
            on success: claim by STAND-KEEPER picks it up automatically.

  result    append stand_result block to a stand_request already in
            stand_wip/, then mv to stand_done/. STAND-KEEPER only.
            validates result/stand_status/profile enums.

Caller role from $COORD_ROLE (no --as override).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from greatminds.core.paths import find_coord_dir
from greatminds.core.util import die


def run_task(*argv: str) -> int:
    """Forward env. Pass-through stdout/stderr.

    Uses ``python -m greatminds.cli.task`` so we don't depend on the
    ``greatminds-task`` entry-point being on PATH.
    """
    return subprocess.call([sys.executable, "-m", "greatminds.cli.task", *argv])


# Queues where a product task can still be "active" (i.e. wanting evidence)
ACTIVE_PRODUCT_QUEUES = (
    "feature_inbox",
    "feature_plan",
    "feature_dev",
    "feature_ui_dev",
    "feature_docs",
    "feature_test",
    "feature_docs_review",
    "feature_review",
    "feature_blocked",
)


def task_exists_in_active(coord: Path, task_id: str) -> bool:
    for q in ACTIVE_PRODUCT_QUEUES:
        for ext in (".yaml", ".md"):
            if (coord / q / f"{task_id}{ext}").is_file():
                return True
        # also accept prefix match (e.g. "0237" → 0237-xxx.yaml)
        qd = coord / q
        if qd.is_dir():
            for f in qd.iterdir():
                if f.is_file() and f.stem.startswith(task_id + "-"):
                    return True
                if f.is_file() and f.stem == task_id:
                    return True
    return False


def cmd_request(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    # validate evidence-for ids
    if args.evidence_for:
        missing = [tid for tid in args.evidence_for
                   if not task_exists_in_active(coord, tid)]
        if missing:
            die(2, f"evidence-for ids not in any active queue: {missing}")

    # forward to bin/task new
    argv = [
        "new",
        "--stream", "stand",
        "--request-type", args.request_type,
        "--title", args.title,
        "--profile", args.profile,
    ]
    if args.priority:
        argv += ["--priority", args.priority]
    if args.hosts:
        argv += ["--hosts", *args.hosts]
    if args.evidence_for:
        argv += ["--evidence-for", *args.evidence_for]
    if args.description:
        argv += ["--description", args.description]
    if args.reason:
        argv += ["--reason", args.reason]
    return run_task(*argv)


def cmd_result(args: argparse.Namespace) -> int:
    coord = find_coord_dir()
    # caller must be STAND-KEEPER (enforced by bin/task too, double check here)
    role = (os.environ.get("COORD_ROLE") or "").upper()
    if role != "STAND-KEEPER":
        die(3, "only STAND-KEEPER may produce stand_result")

    # task must be in stand_wip currently
    if not (coord / "stand_wip" / f"{args.id}.yaml").is_file() \
       and not (coord / "stand_wip" / f"{args.id}.md").is_file():
        # try prefix
        if not any(f.stem.startswith(args.id + "-") or f.stem == args.id
                   for f in (coord / "stand_wip").glob("*.yaml")) \
           and not any(f.stem.startswith(args.id + "-") or f.stem == args.id
                       for f in (coord / "stand_wip").glob("*.md")):
            die(1, f"task {args.id} not in stand_wip/")

    # 1. append-block stand_result
    block_argv = [
        "append-block", "stand_result",
        "--id", args.id,
        "--field", f"result={args.result}",
        "--field", f"stand_status={args.status}",
        "--field", f"commit={args.commit}",
        "--field", f"profile={args.profile}",
    ]
    if args.notes:
        block_argv += ["--body", args.notes]
    rc = run_task(*block_argv)
    if rc != 0:
        return rc

    # 2. mv to stand_done
    mv_argv = ["mv", args.id, "stand_done"]
    if args.reason:
        mv_argv += ["--reason", args.reason]
    return run_task(*mv_argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stand", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("request", help="create stand_request")
    pr.add_argument("--request-type", required=True,
                    choices=["deploy", "restart", "rebuild", "smoke",
                             "remote_sync", "gpu_check", "teardown"])
    pr.add_argument("--profile", required=True,
                    choices=["full-deploy", "vite-dev"])
    pr.add_argument("--title", required=True)
    pr.add_argument("--hosts", nargs="*")
    pr.add_argument("--evidence-for", nargs="*",
                    help="task ids that will use this stand's evidence")
    pr.add_argument("--description", help="literal | @file | -")
    pr.add_argument("--priority", choices=["low", "normal", "high"])
    pr.add_argument("--reason", help="journal reason")
    pr.set_defaults(func=cmd_request)

    ps = sub.add_parser("result", help="record stand_result + mv to stand_done")
    ps.add_argument("id")
    ps.add_argument("--result", required=True, choices=["ok", "partial", "fail"])
    ps.add_argument("--status", required=True,
                    choices=["READY", "DEGRADED", "DOWN", "BLOCKED"])
    ps.add_argument("--commit", required=True)
    ps.add_argument("--profile", required=True, choices=["full-deploy", "vite-dev"])
    ps.add_argument("--notes", help="literal | @file | -")
    ps.add_argument("--reason", help="journal reason for mv")
    ps.set_defaults(func=cmd_result)

    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-stand`` in pyproject.toml."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
