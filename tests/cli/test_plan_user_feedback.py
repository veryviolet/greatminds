"""Regression test for ``greatminds plan`` from a ``user_feedback`` task.

Background (task 0030/0032 — EXPLORER avatar dogfood on 1.2.0): the plan
convenience writes the triage block, then ``mv``s straight to
``feature_plan``. But the schema only permits

    user_feedback → feature_inbox → feature_plan

(the direct ``user_feedback → feature_plan`` transition does not exist),
so step 2/4 raises and the task is left in ``user_feedback`` with an
orphan triage block. This breaks the documented Phase A4 fresh-user
walkthrough where ``task new --in-queue user_feedback`` is the canonical
intake path for USER-filed product feedback.

Fix: when the source queue is ``user_feedback``, route through
``feature_inbox`` first; the triage block satisfies both transitions'
``requires: [triage_block]``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _setup_project(tmp_path: Path) -> Path:
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path),
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=str(tmp_path), capture_output=True, text=True,
                   check=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(tmp_path), capture_output=True, text=True,
                   check=True)
    subprocess.run(["git", "add", "."], cwd=str(tmp_path),
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(tmp_path),
                   capture_output=True, text=True, check=True)
    return tmp_path


def _gm(project_dir: Path, *argv: str,
        role: str = "ARCHITECT-PLANNER") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project_dir)
    env["GREATMINDS_ROLE"] = role
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *argv],
        capture_output=True, text=True, env=env,
    )


def _head(project_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_dir), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _new_user_feedback_task(proj: Path, title: str) -> str:
    """Create a product task directly in user_feedback (USER intake)."""
    cp = _gm(proj, "task", "new",
             "--stream", "product",
             "--kind", "feature",
             "--scope", "backend",
             "--reporter", "USER",
             "--title", title,
             "--in-queue", "user_feedback",
             role="USER")
    assert cp.returncode == 0, f"task new failed: {cp.stderr}"
    # Stdout: "wrote .../coordination/user_feedback/<id>.yaml"
    line = [l for l in cp.stdout.splitlines() if "user_feedback" in l][0]
    return line.rsplit("/", 1)[-1].removesuffix(".yaml")


def test_plan_routes_user_feedback_through_feature_inbox(tmp_path: Path):
    """plan from user_feedback must land in feature_dev with no orphans."""
    proj = _setup_project(tmp_path)
    tid = _new_user_feedback_task(proj, "Toy: add hello-world function")

    cp = _gm(proj, "plan", tid,
             "--scope", "backend",
             "--assignee-role", "DEVELOPER",
             "--base-commit", _head(proj),
             "--plan-kind", "full",
             "--mode", "A",
             "--stand-required", "false",
             "--body", "Implement hello() in src/toy/__init__.py returning the literal "
                       "string 'hello, world'. Local pytest test_hello() verifies the "
                       "return value.")
    assert cp.returncode == 0, f"plan failed: {cp.stderr}"

    coord = proj / "coordination"
    # Final landing: feature_dev (scope=backend → SCOPE_QUEUE[backend])
    final_path = coord / "feature_dev" / f"{tid}.yaml"
    assert final_path.is_file(), (
        f"task should land in feature_dev, not stuck in user_feedback. "
        f"feature_dev: {list((coord / 'feature_dev').glob('*.yaml'))}, "
        f"user_feedback: {list((coord / 'user_feedback').glob('*.yaml'))}"
    )

    # No orphan in any earlier queue
    for queue in ("user_feedback", "feature_inbox", "feature_plan"):
        leftover = list((coord / queue).glob(f"{tid}*.yaml"))
        assert not leftover, f"task leaked into {queue}: {leftover}"

    # Triage + plan blocks both present
    data = yaml.safe_load(final_path.read_text(encoding="utf-8"))
    kinds = [b.get("kind") for b in data.get("blocks", [])]
    assert "triage" in kinds, f"triage block missing: {kinds}"
    assert "plan" in kinds, f"plan block missing: {kinds}"


def test_plan_from_feature_inbox_still_one_hop(tmp_path: Path):
    """Direct feature_inbox intake (PLANNER-authored) must keep working —
    no extra hop, no behavior change for the non-user_feedback path."""
    proj = _setup_project(tmp_path)
    # Default stream=product intake is feature_inbox (without --in-queue).
    cp = _gm(proj, "task", "new",
             "--stream", "product",
             "--kind", "feature",
             "--scope", "backend",
             "--title", "Inbox intake — control")
    assert cp.returncode == 0, cp.stderr
    line = [l for l in cp.stdout.splitlines() if "feature_inbox" in l][0]
    tid = line.rsplit("/", 1)[-1].removesuffix(".yaml")

    cp = _gm(proj, "plan", tid,
             "--scope", "backend",
             "--assignee-role", "DEVELOPER",
             "--base-commit", _head(proj),
             "--plan-kind", "full",
             "--mode", "A",
             "--stand-required", "false",
             "--body", "Stub plan to verify the feature_inbox path still works.")
    assert cp.returncode == 0, cp.stderr

    coord = proj / "coordination"
    assert (coord / "feature_dev" / f"{tid}.yaml").is_file()
    for queue in ("user_feedback", "feature_inbox", "feature_plan"):
        leftover = list((coord / queue).glob(f"{tid}*.yaml"))
        assert not leftover, f"task leaked into {queue}: {leftover}"
