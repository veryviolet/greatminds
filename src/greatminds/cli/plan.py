"""plan — ARCHITECT-PLANNER convenience: triage + plan + route in one go.

Atomic 4-step chain:

  1. ``append-block triage`` on the inbox task
  2. ``mv`` → ``feature_plan``
  3. ``append-block plan`` (with all required fields)
  4. ``mv`` → per-scope queue (or ``feature_docs_review`` for ``--audit-only``)

The chain stops at the first failing step and prints exactly where it
stopped and what to do — never a silent partial failure.

Only ``ARCHITECT-PLANNER`` may run this command (the ``GREATMINDS_ROLE``
env var is enforced; refusing other roles up-front avoids ambiguity).
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from greatminds.cli._colors import info, ok
from greatminds.cli.task import append_block, move_task
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_coord_dir


SCOPE_QUEUE = {
    "backend": "feature_dev",
    "ui":      "feature_ui_dev",
    "docs":    "feature_docs",
}


def find_task_queue(coord: Path, task_id: str) -> str | None:
    for q in coord.iterdir():
        if not q.is_dir() or q.name.startswith("."):
            continue
        if (q / f"{task_id}.yaml").is_file():
            return q.name
    return None


@click.command(
    short_help="ARCHITECT-PLANNER: triage + plan + route in 4 atomic steps",
    help=__doc__,
)
@click.argument("task_id", metavar="ID")
@click.option("--scope", required=True, type=click.Choice(sorted(SCOPE_QUEUE)))
@click.option("--assignee-role", required=True)
@click.option("--base-commit", required=True)
@click.option("--plan-kind", required=True, type=click.Choice(["full", "bugfix"]))
@click.option("--mode", required=True, type=click.Choice(["A", "B", "C"]))
@click.option("--stand-required", required=True,
              type=click.Choice(["true", "false"]))
@click.option("--stand-reason", default="")
@click.option("--body", default=None, help="plan text (literal)")
@click.option("--body-file", default=None,
              help="plan text: file path, or - for stdin")
@click.option("--triage", default=None, help="optional triage note")
@click.option("--stop-at", type=click.Choice(["plan"]),
              help="stop after plan block; do not route to per-scope queue")
@click.option("--audit-only", is_flag=True,
              help="READER audit (scope must be docs): no WRITER step — "
                   "route straight to feature_docs_review.")
def plan(task_id, scope, assignee_role, base_commit, plan_kind, mode,
         stand_required, stand_reason, body, body_file, triage,
         stop_at, audit_only) -> None:
    role = (os.environ.get("GREATMINDS_ROLE") or "").upper()
    if role != "ARCHITECT-PLANNER":
        raise GreatMindsError(
            f"only ARCHITECT-PLANNER may use greatminds plan "
            f"(GREATMINDS_ROLE={role!r})",
            exit_code=3,
        )

    if audit_only and scope != "docs":
        raise GreatMindsError(
            "--audit-only requires --scope docs (it is the READER audit path)"
        )

    if stand_required == "true" and not stand_reason.strip():
        raise GreatMindsError("--stand-required true requires --stand-reason")
    if not body and not body_file:
        raise GreatMindsError("provide --body or --body-file")

    coord = find_coord_dir()
    where = find_task_queue(coord, task_id)
    if where is None:
        raise GreatMindsError(f"task {task_id} not found in any queue")
    if where not in ("feature_inbox", "user_feedback"):
        raise GreatMindsError(
            f"task {task_id} is in {where}; plan only takes tasks "
            "from feature_inbox or user_feedback"
        )

    # 1. triage block
    triage_body = triage or f"triaged for planning (scope:{scope})"
    try:
        append_block(task_id=task_id, kind="triage", body=triage_body)
    except GreatMindsError as exc:
        raise GreatMindsError(
            f"step 1/4 triage failed: {exc.message} — task still in {where}, "
            f"nothing moved",
            exit_code=2,
        )

    # 2. mv → feature_plan
    try:
        move_task(task_id=task_id, to_queue="feature_plan",
                  reason="triaged; planning")
    except GreatMindsError as exc:
        raise GreatMindsError(
            f"step 2/4 mv {where}→feature_plan failed: {exc.message} — "
            f"triage block written, task still in {where}",
            exit_code=2,
        )

    # 3. plan block
    fields: dict[str, object] = {
        "base_commit": base_commit,
        "assignee_role": assignee_role,
        "stand_required": stand_required == "true",
        "plan_kind": plan_kind,
        "mode": mode,
        "ready_for_implementation": True,
    }
    if stand_reason.strip():
        fields["stand_reason"] = stand_reason
    if audit_only:
        fields["audit_only"] = True

    plan_body = body if body is not None else (
        "-" if body_file == "-" else f"@{body_file}"
    )
    try:
        append_block(task_id=task_id, kind="plan", fields=fields, body=plan_body)
    except GreatMindsError as exc:
        raise GreatMindsError(
            f"step 3/4 plan block failed: {exc.message} — task is in "
            f"feature_plan; fix the field error and rerun: greatminds task "
            f"append-block plan --id {task_id} ... then greatminds task mv "
            f"{task_id} {SCOPE_QUEUE[scope]}",
            exit_code=2,
        )

    if stop_at == "plan":
        info(f"plan: {task_id} planned, left in feature_plan (--stop-at plan)")
        return

    # 4. route. Audit-only docs tasks go straight to READER's queue.
    target = "feature_docs_review" if audit_only else SCOPE_QUEUE[scope]
    reason = ("route audit-only → READER" if audit_only
              else f"route scope:{scope}")
    try:
        move_task(task_id=task_id, to_queue=target, reason=reason)
    except GreatMindsError as exc:
        raise GreatMindsError(
            f"step 4/4 mv feature_plan→{target} failed: {exc.message} — "
            f"plan is valid and saved; rerun: greatminds task mv {task_id} "
            f"{target}",
            exit_code=2,
        )

    ok(f"plan: {task_id} → {target} "
       f"({'audit-only ' if audit_only else ''}scope:{scope} "
       f"assignee:{assignee_role})")


if __name__ == "__main__":
    plan()
