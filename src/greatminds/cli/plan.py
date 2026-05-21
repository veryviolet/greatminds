"""plan — ARCHITECT-PLANNER convenience: triage + plan + route in one go.

Atomic 4-step chain:

  1. ``append-block triage`` on the inbox task
  2. ``mv`` → ``feature_plan``
  3. ``append-block plan`` (with all required fields)
  4. ``mv`` → per-scope queue (or ``feature_docs_review`` for ``--audit-only``)

The chain stops at the first failing step and prints exactly where it
stopped and what to do — never a silent partial failure.

Only ``ARCHITECT-PLANNER`` may run this command (the GREATMINDS_ROLE env
var is enforced; refusing other roles up-front avoids ambiguity).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from greatminds.core.paths import find_coord_dir
from greatminds.cli._colors import err, info, ok


SCOPE_QUEUE = {
    "backend": "feature_dev",
    "ui":      "feature_ui_dev",
    "docs":    "feature_docs",
}


def run_task(*argv: str) -> int:
    """Invoke ``greatminds task`` in a child process.

    Uses ``python -m greatminds.cli.task`` so the call works regardless
    of how greatminds was installed (pipx, system pip, project venv,
    sourced env).
    """
    return subprocess.call([sys.executable, "-m", "greatminds.cli.task", *argv])


def find_task_queue(coord: Path, task_id: str) -> str | None:
    for q in coord.iterdir():
        if not q.is_dir() or q.name.startswith("."):
            continue
        if (q / f"{task_id}.yaml").is_file():
            return q.name
    return None


def _die(code: int, msg: str) -> None:
    err(msg)
    raise click.exceptions.Exit(code)


@click.command(short_help="ARCHITECT-PLANNER: triage + plan + route in 4 atomic steps",
               help=__doc__)
@click.argument("id")
@click.option("--scope", required=True, type=click.Choice(sorted(SCOPE_QUEUE)))
@click.option("--assignee-role", required=True)
@click.option("--base-commit", required=True)
@click.option("--plan-kind", required=True, type=click.Choice(["full", "bugfix"]))
@click.option("--mode", required=True, type=click.Choice(["A", "B", "C"]))
@click.option("--stand-required", required=True, type=click.Choice(["true", "false"]))
@click.option("--stand-reason", default="")
@click.option("--body", default=None, help="plan text (literal)")
@click.option("--body-file", default=None, help="plan text: file path, or - for stdin")
@click.option("--triage", default=None, help="optional triage note")
@click.option("--stop-at", type=click.Choice(["plan"]),
              help="stop after plan block; do not route to per-scope queue")
@click.option("--audit-only", is_flag=True,
              help="READER audit (scope must be docs): no WRITER step — "
                   "route straight to feature_docs_review.")
def plan(id: str, scope: str, assignee_role: str, base_commit: str,
         plan_kind: str, mode: str, stand_required: str, stand_reason: str,
         body: str | None, body_file: str | None, triage: str | None,
         stop_at: str | None, audit_only: bool) -> None:
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "ARCHITECT-PLANNER":
        _die(3, f"only ARCHITECT-PLANNER may use greatminds plan (GREATMINDS_ROLE={role!r})")

    if audit_only and scope != "docs":
        _die(1, "--audit-only requires --scope docs (it is the READER audit path)")

    if stand_required == "true" and not stand_reason.strip():
        _die(1, "--stand-required true requires --stand-reason")
    if not body and not body_file:
        _die(1, "provide --body or --body-file")

    coord = find_coord_dir()
    where = find_task_queue(coord, id)
    if where is None:
        _die(1, f"task {id} not found in any queue")
    if where not in ("feature_inbox", "user_feedback"):
        _die(1, f"task {id} is in {where}; plan only takes tasks "
                "from feature_inbox or user_feedback")

    # 1. triage
    triage_body = triage or f"triaged for planning (scope:{scope})"
    if run_task("append-block", "triage", "--id", id, "--body", triage_body) != 0:
        _die(2, f"step 1/4 triage failed — task still in {where}, nothing moved")

    # 2. mv → feature_plan
    if run_task("mv", id, "feature_plan", "--reason", "triaged; planning") != 0:
        _die(2, f"step 2/4 mv {where}→feature_plan failed — triage block written, "
                f"task still in {where}")

    # 3. plan block
    plan_argv = [
        "append-block", "plan", "--id", id,
        "--field", f"base_commit={base_commit}",
        "--field", f"assignee_role={assignee_role}",
        "--field", f"stand_required={stand_required}",
        "--field", f"plan_kind={plan_kind}",
        "--field", f"mode={mode}",
        "--field", "ready_for_implementation=true",
    ]
    if stand_reason.strip():
        plan_argv += ["--field", f"stand_reason={stand_reason}"]
    if audit_only:
        plan_argv += ["--field", "audit_only=true"]
    if body_file:
        plan_argv += ["--body", "-" if body_file == "-" else f"@{body_file}"]
    else:
        plan_argv += ["--body", body]
    if run_task(*plan_argv) != 0:
        _die(2, f"step 3/4 plan block failed — task is in feature_plan; fix "
                f"the field error and rerun just: greatminds task append-block "
                f"plan --id {id} ... then greatminds task mv {id} "
                f"{SCOPE_QUEUE[scope]}")

    if stop_at == "plan":
        info(f"plan: {id} planned, left in feature_plan (--stop-at plan)")
        return

    # 4. route. audit-only docs tasks go straight to READER's queue
    target = "feature_docs_review" if audit_only else SCOPE_QUEUE[scope]
    reason = ("route audit-only → READER" if audit_only
              else f"route scope:{scope}")
    if run_task("mv", id, target, "--reason", reason) != 0:
        _die(2, f"step 4/4 mv feature_plan→{target} failed — plan is valid "
                f"and saved; rerun just: greatminds task mv {id} {target}")

    ok(f"plan: {id} → {target} "
       f"({'audit-only ' if audit_only else ''}scope:{scope} "
       f"assignee:{assignee_role})")


if __name__ == "__main__":
    plan()
