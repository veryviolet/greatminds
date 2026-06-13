"""Tests for task 0271: ``greatminds stand lease`` must reject any
``--worktree`` path that isn't a per-task isolated branch under
``<project_dir>/.worktrees/<seq>[-slug]``.

Pre-0271 schema only declared the state machine; the only path
enforcement lived in STAND-KEEPER's deploy whitelist, which ran AFTER
lease acquisition. The wrong path silently succeeded at the CLI;
SK rejected at runtime, leaving the lease in ``preparing/`` and
TESTER puzzled about how to fix it. 0271 makes the CLI the first
line of defense.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand as stand_mod
from greatminds.core.errors import GreatMindsError


TASK_ID = "0271-schema-enforce-tester-stand-lease-worktree-per-task-isolatio"
SEQ = "0271"


# ---------- schema source-of-truth ----------


def test_schema_lease_declares_worktree_constraint() -> None:
    """``schema.stand.resource.lease.worktree_constraint`` must
    declare ``enforced_by: cli`` so future readers see the CLI is
    authoritative (SK whitelist is the second line of defense, not
    the only one)."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    lease = (((doc.get("stand") or {}).get("resource") or {})
             .get("lease") or {})
    wc = lease.get("worktree_constraint") or {}
    assert wc.get("enforced_by") == "cli", (
        "0271: schema must declare enforced_by: cli for the worktree "
        "constraint so the contract is canon, not implementation detail"
    )
    pattern = wc.get("pattern") or ""
    assert ".worktrees" in pattern, (
        f"0271: schema pattern must mention .worktrees (got {pattern!r})"
    )


# ---------- _validate_lease_worktree (library API) ----------


def test_validator_accepts_short_seq_worktree(tmp_path: Path) -> None:
    """``.worktrees/0271`` (the convention used by `git worktree add`
    when the operator just types the seq) is the canonical form."""
    project = tmp_path / "proj"
    (project / ".worktrees" / SEQ).mkdir(parents=True)
    # No raise → accepted.
    stand_mod._validate_lease_worktree(
        TASK_ID, str(project / ".worktrees" / SEQ), project,
    )


def test_validator_accepts_full_slug_worktree(tmp_path: Path) -> None:
    """The slug variant ``.worktrees/<full-task-id>`` is also valid —
    `git worktree add` auto-uses this when no explicit branch name is
    provided."""
    project = tmp_path / "proj"
    (project / ".worktrees" / TASK_ID).mkdir(parents=True)
    stand_mod._validate_lease_worktree(
        TASK_ID, str(project / ".worktrees" / TASK_ID), project,
    )


def test_validator_rejects_main_tree_with_explicit_message(
    tmp_path: Path,
) -> None:
    """The motivating bug: passing the project_dir itself. Error must
    explain the self-modify foot-gun and show the right path."""
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(GreatMindsError) as exc:
        stand_mod._validate_lease_worktree(
            TASK_ID, str(project), project,
        )
    msg = str(exc.value)
    assert ".worktrees" in msg
    assert SEQ in msg
    # The error must allude to the self-modify hazard or the main tree.
    assert "main" in msg.lower() or "self-modify" in msg.lower() \
        or "running host" in msg.lower()


def test_validator_rejects_unrelated_path(tmp_path: Path) -> None:
    """A path outside ``.worktrees/`` (e.g. /tmp/foo) is rejected with
    a message pointing at the expected parent dir."""
    project = tmp_path / "proj"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(GreatMindsError) as exc:
        stand_mod._validate_lease_worktree(
            TASK_ID, str(elsewhere), project,
        )
    msg = str(exc.value)
    assert ".worktrees" in msg


def test_validator_rejects_wrong_task_id_under_worktrees(
    tmp_path: Path,
) -> None:
    """``.worktrees/9999`` would belong to a different task — the
    constraint requires the seq prefix to match the lease's
    ``--task`` arg."""
    project = tmp_path / "proj"
    (project / ".worktrees" / "9999").mkdir(parents=True)
    with pytest.raises(GreatMindsError) as exc:
        stand_mod._validate_lease_worktree(
            TASK_ID, str(project / ".worktrees" / "9999"), project,
        )
    msg = str(exc.value)
    assert SEQ in msg
    assert "basename" in msg or "seq" in msg


def test_validator_rejects_empty_worktree() -> None:
    """Defensive: empty string / None must be rejected outright."""
    with pytest.raises(GreatMindsError):
        stand_mod._validate_lease_worktree(TASK_ID, "", Path("/tmp"))
    with pytest.raises(GreatMindsError):
        stand_mod._validate_lease_worktree(TASK_ID, None, Path("/tmp"))


def test_validator_resolves_relative_paths(tmp_path: Path) -> None:
    """Relative paths must be resolved against the cwd before the
    parent check — operators shouldn't be blocked because they typed
    ``./.worktrees/0271`` from the project root."""
    project = tmp_path / "proj"
    (project / ".worktrees" / SEQ).mkdir(parents=True)
    cwd = os.getcwd()
    try:
        os.chdir(project)
        stand_mod._validate_lease_worktree(
            TASK_ID, f".worktrees/{SEQ}", project,
        )
    finally:
        os.chdir(cwd)


# ---------- end-to-end through the Click command ----------


def _project_with_state(tmp_path: Path) -> Path:
    """Build a toy project + initial empty state.yaml so the lease
    command can attempt its mutator (we exit on the validator
    before that path matters, but the runtime still expects a coord
    dir)."""
    project = tmp_path / "proj"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coordination" / ".stand" / "state.yaml").write_text(
        yaml.safe_dump({"state": "free"}), encoding="utf-8",
    )
    # The lease resolves --profile to a real playbook file in
    # stand-profiles/ (profile.yaml IS the ansible playbook). Seed one so
    # the profile check passes and the worktree validator (the subject of
    # these tests) is what decides accept/reject.
    sp = project / "coordination" / "stand-profiles"
    sp.mkdir(parents=True, exist_ok=True)
    (sp / "full-deploy.yaml").write_text(
        yaml.safe_dump([{"name": "p", "hosts": "localhost", "tasks": []}]),
        encoding="utf-8",
    )
    return project


def _run_lease(project: Path, *args: str
                ) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GREATMINDS_PROJECT_DIR"] = str(project)
    env["GREATMINDS_ROLE"] = "TESTER"
    return subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main",
         "stand", "lease", *args],
        capture_output=True, text=True, env=env, cwd=str(project),
    )


def test_stand_lease_cli_rejects_main_tree(tmp_path: Path) -> None:
    """End-to-end: ``greatminds stand lease --worktree <project_dir>``
    must exit ≠ 0 BEFORE any state.yaml mutation."""
    project = _project_with_state(tmp_path)
    cp = _run_lease(project,
                    "--task", TASK_ID,
                    "--worktree", str(project),
                    "--profile", "full-deploy")
    assert cp.returncode != 0, (
        f"0271: CLI must reject main-tree worktree. "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    err = cp.stdout + cp.stderr
    assert ".worktrees" in err

    # State file untouched (still free).
    state = yaml.safe_load(
        (project / "coordination" / ".stand" / "state.yaml")
        .read_text(encoding="utf-8")
    )
    assert state.get("state") == "free", (
        "0271: validator must reject BEFORE state.yaml is mutated"
    )


def test_stand_lease_cli_rejects_existing_nongit_worktree(
    tmp_path: Path,
) -> None:
    """A directory under .worktrees is not enough; leases deploy source
    git worktrees, not no-git deployed payload directories."""
    project = _project_with_state(tmp_path)
    wt = project / ".worktrees" / SEQ
    wt.mkdir(parents=True)

    cp = _run_lease(project,
                    "--task", TASK_ID,
                    "--worktree", str(wt),
                    "--profile", "full-deploy")

    assert cp.returncode != 0
    err = cp.stdout + cp.stderr
    assert "not a git worktree" in err
    assert "no-git deployed payloads" in err


def test_stand_lease_cli_accepts_correct_worktree(tmp_path: Path) -> None:
    """A valid ``.worktrees/<seq>`` path must allow the lease command
    to reach its mutator and write state.yaml. The lease_id is the
    LAST line of stdout per the doc-string contract."""
    project = _project_with_state(tmp_path)
    wt = project / ".worktrees" / SEQ
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /tmp/fake\n", encoding="utf-8")
    cp = _run_lease(project,
                    "--task", TASK_ID,
                    "--worktree", str(wt),
                    "--profile", "full-deploy")
    assert cp.returncode == 0, (
        f"0271: CLI must accept a valid worktree. "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    assert "lease_id" in cp.stdout
