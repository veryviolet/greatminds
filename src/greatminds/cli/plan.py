#!/usr/bin/env python3
"""bin/plan — one-shot planning wrapper for ARCHITECT-PLANNER.

Replaces the manual triage→mv→plan-block→route chain with a single
command. Internally calls bin/task only, so all schema validation,
role/scope gating, fcntl locks, intent, journal and heartbeat
side-effects are preserved exactly. Same pattern as bin/stand.

Takes a task sitting in feature_inbox/ (or user_feedback/) all the way
to its per-scope implementer queue:

  1. append-block triage   (in the current intake queue)
  2. mv  → feature_plan
  3. append-block plan      (all required plan fields + body)
  4. mv  → feature_dev | feature_ui_dev | feature_docs   (by --scope)

Usage:
  bin/plan <task-id> \
      --scope backend|ui|docs \
      --assignee-role DEVELOPER \
      --base-commit <sha> \
      --plan-kind full|bugfix \
      --mode A|B|C \
      --stand-required true|false [--stand-reason "..."] \
      --body "<plan text>"   (or --body-file PATH | --body-file -) \
      [--triage "<triage note>"] \
      [--stop-at plan]       # don't route; leave in feature_plan

The chain stops at the first failing step and prints exactly where it
stopped and what to do — never a silent partial failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from greatminds.core.paths import find_coord_dir
from greatminds.core.util import die

SCOPE_QUEUE = {
    "backend": "feature_dev",
    "ui":      "feature_ui_dev",
    "docs":    "feature_docs",
}


def run_task(*argv: str) -> int:
    """Invoke the ``greatminds-task`` CLI in a child process.

    Uses ``python -m greatminds.cli.task`` so we don't depend on
    ``greatminds-task`` being on PATH (works regardless of how the package
    was installed: pipx, system pip, project venv, …).
    """
    return subprocess.call([sys.executable, "-m", "greatminds.cli.task", *argv])


def find_task_queue(coord: Path, task_id: str) -> str | None:
    for q in coord.iterdir():
        if not q.is_dir() or q.name.startswith("."):
            continue
        if (q / f"{task_id}.yaml").is_file():
            return q.name
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-plan`` in pyproject.toml."""
    ap = argparse.ArgumentParser(prog="plan", description=__doc__.splitlines()[0])
    ap.add_argument("id")
    ap.add_argument("--scope", required=True, choices=sorted(SCOPE_QUEUE))
    ap.add_argument("--assignee-role", required=True)
    ap.add_argument("--base-commit", required=True)
    ap.add_argument("--plan-kind", required=True, choices=["full", "bugfix"])
    ap.add_argument("--mode", required=True, choices=["A", "B", "C"])
    ap.add_argument("--stand-required", required=True, choices=["true", "false"])
    ap.add_argument("--stand-reason", default="")
    ap.add_argument("--body", help="plan text (literal)")
    ap.add_argument("--body-file", help="plan text: file path, or - for stdin")
    ap.add_argument("--triage", help="optional triage note")
    ap.add_argument("--stop-at", choices=["plan"],
                    help="stop after plan block; do not route to per-scope queue")
    ap.add_argument("--audit-only", action="store_true",
                    help="independent READER audit (scope must be docs): no "
                         "WRITER step — route straight to feature_docs_review. "
                         "READER audits reality-vs-docs, records findings; "
                         "PLANNER later spawns a separate write task.")
    args = ap.parse_args(argv)

    role = (os.environ.get("COORD_ROLE") or "").upper()
    if role != "ARCHITECT-PLANNER":
        die(3, f"only ARCHITECT-PLANNER may use bin/plan (COORD_ROLE={role!r})")

    if args.audit_only and args.scope != "docs":
        die(1, "--audit-only requires --scope docs (it is the READER audit path)")

    if args.stand_required == "true" and not args.stand_reason.strip():
        die(1, "--stand-required true requires --stand-reason")
    if not args.body and not args.body_file:
        die(1, "provide --body or --body-file")

    coord = find_coord_dir()
    where = find_task_queue(coord, args.id)
    if where is None:
        die(1, f"task {args.id} not found in any queue")
    if where not in ("feature_inbox", "user_feedback"):
        die(1, f"task {args.id} is in {where}; bin/plan only takes tasks "
               f"from feature_inbox or user_feedback")

    # 1. triage
    triage_body = args.triage or f"triaged for planning (scope:{args.scope})"
    if run_task("append-block", "triage", "--id", args.id, "--body", triage_body) != 0:
        die(2, f"step 1/4 triage failed — task still in {where}, nothing moved")

    # 2. mv → feature_plan
    if run_task("mv", args.id, "feature_plan", "--reason", "triaged; planning") != 0:
        die(2, f"step 2/4 mv {where}→feature_plan failed — triage block written, "
               f"task still in {where}")

    # 3. plan block
    plan_argv = [
        "append-block", "plan", "--id", args.id,
        "--field", f"base_commit={args.base_commit}",
        "--field", f"assignee_role={args.assignee_role}",
        "--field", f"stand_required={args.stand_required}",
        "--field", f"plan_kind={args.plan_kind}",
        "--field", f"mode={args.mode}",
        "--field", "ready_for_implementation=true",
    ]
    if args.stand_reason.strip():
        plan_argv += ["--field", f"stand_reason={args.stand_reason}"]
    if args.audit_only:
        plan_argv += ["--field", "audit_only=true"]
    if args.body_file:
        plan_argv += ["--body", "-" if args.body_file == "-" else f"@{args.body_file}"]
    else:
        plan_argv += ["--body", args.body]
    if run_task(*plan_argv) != 0:
        die(2, f"step 3/4 plan block failed — task is in feature_plan; fix the "
               f"field error and rerun just: bin/task append-block plan --id "
               f"{args.id} ... then bin/task mv {args.id} "
               f"{SCOPE_QUEUE[args.scope]}")

    if args.stop_at == "plan":
        print(f"plan: {args.id} planned, left in feature_plan (--stop-at plan)")
        return 0

    # 4. route. audit-only docs tasks go straight to READER's queue
    # (feature_docs_review), bypassing the WRITER step entirely.
    target = "feature_docs_review" if args.audit_only else SCOPE_QUEUE[args.scope]
    reason = ("route audit-only → READER" if args.audit_only
              else f"route scope:{args.scope}")
    if run_task("mv", args.id, target, "--reason", reason) != 0:
        die(2, f"step 4/4 mv feature_plan→{target} failed — plan is valid and "
               f"saved; rerun just: bin/task mv {args.id} {target}")

    print(f"plan: {args.id} → {target} "
          f"({'audit-only ' if args.audit_only else ''}scope:{args.scope} "
          f"assignee:{args.assignee_role})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
