"""Regression tests for ``greatminds task new --in-queue user_feedback`` (0033).

Background (review_sessions/0016 Phase A4 — EXPLORER avatar dogfood on
1.2.0): the canonical USER intake walkthrough invites a human to file
product feedback by running ``greatminds task new --stream product ...
--in-queue user_feedback`` from a fresh shell with no fleet env. On
1.2.0 that command errored with ``caller role unknown: set
GREATMINDS_ROLE in your shell``, contradicting the published path.

USER is the one role with no agent launcher exporting GREATMINDS_ROLE
for them (it's a human). Schema says ``user_feedback.writers: [USER]``,
so when ``--in-queue user_feedback`` is targeted and no env role is
set, defaulting to USER is the only sensible fallback. Every other
intake (feature_inbox, stand_requests, review sessions) remains
fleet-driven and keeps the strict requirement.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


def _setup_project(tmp_path: Path) -> Path:
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
    return tmp_path


def _gm_no_role(project_dir: Path, *argv: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "GREATMINDS_ROLE"}
    env["GREATMINDS_PROJECT_DIR"] = str(project_dir)
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *argv],
        capture_output=True, text=True, env=env,
    )


def test_user_feedback_intake_works_without_role_env(tmp_path: Path):
    """The published USER walkthrough must succeed in a fresh shell."""
    proj = _setup_project(tmp_path)
    cp = _gm_no_role(proj, "task", "new",
                     "--stream", "product",
                     "--kind", "feature",
                     "--scope", "backend",
                     "--reporter", "USER",
                     "--title", "Toy: add hello-world function",
                     "--in-queue", "user_feedback")
    assert cp.returncode == 0, (
        f"task new --in-queue user_feedback must succeed with no "
        f"GREATMINDS_ROLE. stderr={cp.stderr}"
    )

    files = list((proj / ".greatminds" / "user_feedback").glob("*.yaml"))
    assert len(files) == 1, files
    data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
    # Reporter is what the caller passed explicitly.
    assert data["reporter"] == "USER"


def test_user_feedback_intake_records_user_as_actor_in_journal(tmp_path: Path):
    """No-role intake must journal the actor as USER (not blank/missing)."""
    proj = _setup_project(tmp_path)
    cp = _gm_no_role(proj, "task", "new",
                     "--stream", "product",
                     "--kind", "bugfix",
                     "--scope", "backend",
                     "--reporter", "USER",
                     "--title", "USER reports something broken",
                     "--in-queue", "user_feedback")
    assert cp.returncode == 0, cp.stderr

    journal = (proj / ".greatminds" / "journal.ndjson")
    assert journal.is_file(), "journal must exist after task new"
    lines = journal.read_text(encoding="utf-8").splitlines()
    new_lines = [l for l in lines if '"from": "_new"' in l]
    assert new_lines, f"no _new journal entry. lines={lines}"
    import json as _json
    rec = _json.loads(new_lines[-1])
    assert rec["actor"] == "USER", rec


def test_feature_inbox_intake_still_requires_role_env(tmp_path: Path):
    """Strict-role requirement must remain for every non-user_feedback intake.

    Regression net: don't relax the env-var contract for the fleet
    queues; only USER intake gets the fallback.
    """
    proj = _setup_project(tmp_path)
    cp = _gm_no_role(proj, "task", "new",
                     "--stream", "product",
                     "--kind", "feature",
                     "--scope", "backend",
                     "--title", "Fleet intake — must fail")
    assert cp.returncode != 0, (
        f"feature_inbox intake must still fail without GREATMINDS_ROLE. "
        f"stdout={cp.stdout!r}"
    )
    assert "caller role unknown" in (cp.stderr + cp.stdout)


# 0258 / 0247 (1.3.0 BREAKING): ``stand`` stream removed.
# ``test_stand_intake_still_requires_role_env`` retired alongside the
# stream — see ``tests/cli/test_task_new_stream_validator_0258.py``
# for the new "stream=stand is rejected" pin.


def test_user_feedback_intake_with_role_env_uses_that_role(tmp_path: Path):
    """If GREATMINDS_ROLE IS set (e.g. ARCHITECT-PLANNER files feedback on
    behalf of USER), the existing env-var path is unchanged — the
    fallback only kicks in when no role is set.
    """
    proj = _setup_project(tmp_path)
    env = os.environ.copy()
    env["GREATMINDS_ROLE"] = "ARCHITECT-PLANNER"
    env["GREATMINDS_PROJECT_DIR"] = str(proj)
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "task", "new",
         "--stream", "product", "--kind", "feature", "--scope", "backend",
         "--reporter", "USER",
         "--title", "PLANNER files on behalf of USER",
         "--in-queue", "user_feedback"],
        capture_output=True, text=True, env=env,
    )
    assert cp.returncode == 0, cp.stderr

    journal = proj / ".greatminds" / "journal.ndjson"
    lines = journal.read_text(encoding="utf-8").splitlines()
    import json as _json
    rec = _json.loads([l for l in lines if '"from": "_new"' in l][-1])
    assert rec["actor"] == "ARCHITECT-PLANNER", rec
