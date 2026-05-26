"""Tests for task 0258: complete the 0247 BREAKING removal at the
CLI/validator surface.

0247 emptied ``schema.streams`` of the legacy ``stand`` entry and
pulled the request/wip/done queues from ``queue_accepts_blocks``, but
the python-layer enums in ``cli/task.py`` still accepted ``stream:
stand`` and ``--kind=stand_request``. 0258 closes that gap so a
``greatminds task new --stream stand …`` is rejected outright and the
old ``--kind=stand_request`` carries an actionable migration message.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from greatminds.cli import task as task_mod
from greatminds.core.errors import GreatMindsError


# ---------- module-constant tables ----------


def test_allowed_intake_queues_has_no_stand_entry() -> None:
    """0258: ``stand`` key must be absent from ALLOWED_INTAKE_QUEUES;
    leaving it would let ``--in-queue=stand_requests`` round-trip past
    the validator even though the queue itself is gone."""
    assert "stand" not in task_mod.ALLOWED_INTAKE_QUEUES
    # The remaining streams stay intact.
    assert "product" in task_mod.ALLOWED_INTAKE_QUEUES
    assert "review_session" in task_mod.ALLOWED_INTAKE_QUEUES


def test_default_intake_queue_rejects_stand() -> None:
    """``default_intake_queue('stand')`` must raise — pre-0258 it
    returned ``'stand_requests'`` (a queue that no longer exists)."""
    with pytest.raises(KeyError):
        task_mod.default_intake_queue("stand")
    # Sanity: the two surviving streams still resolve.
    assert task_mod.default_intake_queue("product") == "feature_inbox"
    assert task_mod.default_intake_queue("review_session") == "review_sessions"


# ---------- validate_header rejects stream=stand ----------


def test_validate_header_rejects_stream_stand() -> None:
    """A header carrying ``stream: stand`` must fail validation with a
    message naming the surviving stream choices."""
    bogus = {
        "id": "0001-probe",
        "stream": "stand",
        "title": "probe",
        "reporter": "USER",
        "opened_at": "2026-05-27T00:00:00Z",
        "priority": "normal",
    }
    with pytest.raises(GreatMindsError) as excinfo:
        task_mod.validate_header(bogus)
    msg = str(excinfo.value)
    assert "product" in msg and "review_session" in msg
    assert "stand" not in msg.split(":", 1)[-1] or "got: 'stand'" in msg


# ---------- create_task rejects stream=stand and kind=stand_request ----------


def _project(tmp_path: Path) -> Path:
    """Build a toy project with coordination/feature_inbox/ + a
    schema canon link so the CLI can find it via cwd."""
    proj = tmp_path / "proj"
    (proj / "coordination" / "feature_inbox").mkdir(parents=True)
    (proj / "coordination" / "user_feedback").mkdir()
    return proj


def _run_gm(proj: Path, *args: str, role: str = "ARCHITECT-PLANNER"
            ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_ROLE"] = role
    env["GREATMINDS_PROJECT_DIR"] = str(proj)
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", *args],
        capture_output=True, text=True, env=env, cwd=str(proj),
    )


def test_task_new_rejects_stream_stand_at_click_layer(tmp_path: Path) -> None:
    """The Click ``--stream`` choice must not accept ``stand``."""
    proj = _project(tmp_path)
    cp = _run_gm(proj, "task", "new", "--stream", "stand",
                 "--title", "probe")
    assert cp.returncode != 0, (
        f"--stream=stand must be rejected. stdout={cp.stdout!r} "
        f"stderr={cp.stderr!r}"
    )
    # Click's invalid-choice message names the surviving options.
    err = (cp.stdout + cp.stderr).lower()
    assert "stand" in err
    assert "product" in err and "review_session" in err


def test_task_new_rejects_kind_stand_request(tmp_path: Path) -> None:
    """Even within ``--stream product`` a leftover ``--kind=stand_request``
    must be rejected with an actionable 1.3.0 migration message."""
    proj = _project(tmp_path)
    cp = _run_gm(proj, "task", "new",
                 "--stream", "product",
                 "--kind", "stand_request",
                 "--scope", "backend",
                 "--title", "probe — must fail")
    assert cp.returncode != 0
    msg = cp.stdout + cp.stderr
    # Either the early stand_request guard in create_task or the
    # PRODUCT_KINDS enum check must fire — both reject.
    assert "stand_request" in msg or "kind" in msg


def test_create_task_library_rejects_stand_stream(tmp_path: Path) -> None:
    """The library entry-point used by stand.py / plan.py / inbox.py
    must also reject stream=stand (not only the Click layer)."""
    proj = _project(tmp_path)
    os.environ["GREATMINDS_ROLE"] = "ARCHITECT-PLANNER"
    os.environ["GREATMINDS_PROJECT_DIR"] = str(proj)
    try:
        with pytest.raises(GreatMindsError) as excinfo:
            task_mod.create_task(
                stream="stand", title="probe",
                request_type="deploy", profile="full-deploy",
            )
        assert "stand" in str(excinfo.value).lower()
    finally:
        os.environ.pop("GREATMINDS_PROJECT_DIR", None)


def test_create_task_library_rejects_kind_stand_request(tmp_path: Path) -> None:
    """Library-level guard pin: ``kind='stand_request'`` is rejected
    regardless of stream, with a message that references the lease API."""
    proj = _project(tmp_path)
    os.environ["GREATMINDS_ROLE"] = "ARCHITECT-PLANNER"
    os.environ["GREATMINDS_PROJECT_DIR"] = str(proj)
    try:
        with pytest.raises(GreatMindsError) as excinfo:
            task_mod.create_task(
                stream="product", title="probe",
                kind="stand_request", scope="backend",
            )
        msg = str(excinfo.value).lower()
        assert "stand_request" in msg
        assert "lease" in msg or "1.3.0" in msg or "0247" in msg
    finally:
        os.environ.pop("GREATMINDS_PROJECT_DIR", None)
