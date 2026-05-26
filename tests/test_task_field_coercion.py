"""Regression tests for ``greatminds task`` field coercion + body-file handling.

These hit the two bugs reported against 1.1.0 in real-world use:

  1. ``coerce_value`` blanket-split *every* ``--field`` value on commas, so
     any prose value (e.g. ``stand_reason="POST X, then GET Y"``) was
     silently turned into a YAML list — and downstream validators choked.
  2. ``task append-block`` lost its ``--body-file`` option during the
     argparse-to-click migration. The plan orchestrator and a number of
     agent prompts still passed it, breaking the orchestrator.

We exercise the actual ``cli.task`` code path (no shell), so a regression
shows up the moment ``pytest`` runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from greatminds.cli.task import coerce_value, LIST_FIELDS


# ---------------------------------------------------------------------------
# 1. coerce_value: commas only split for LIST_FIELDS keys
# ---------------------------------------------------------------------------


def test_stand_reason_with_commas_stays_string():
    """Prose with commas in a non-LIST_FIELDS key must stay a string."""
    v = coerce_value("stand_reason", "POST /node, then GET /health")
    assert isinstance(v, str)
    assert v == "POST /node, then GET /health"


def test_stand_reason_with_colon_stays_string():
    """Colons inside a non-LIST_FIELDS value must NOT become YAML mappings."""
    v = coerce_value("stand_reason",
                     "live-mutating verify owner: STAND-KEEPER")
    assert isinstance(v, str)
    assert v.startswith("live-mutating")


def test_stand_reason_with_commas_AND_colon_stays_string():
    """Realistic agent-supplied reason: the exact 1.1.0 crash trigger."""
    v = coerce_value(
        "stand_reason",
        "POST /node, then GET /health; live-mutating verify owner: STAND-KEEPER",
    )
    assert isinstance(v, str)
    assert "STAND-KEEPER" in v


@pytest.mark.parametrize("key", sorted(LIST_FIELDS))
def test_list_fields_still_split_on_commas(key):
    """LIST_FIELDS must still split comma-separated values into a list."""
    v = coerce_value(key, "a.py, b.py, c.py")
    assert isinstance(v, list)
    assert v == ["a.py", "b.py", "c.py"]


def test_bool_coercion():
    assert coerce_value("ready_for_test", "true") is True
    assert coerce_value("ready_for_test", "false") is False


def test_int_coercion():
    assert coerce_value("seq", "42") == 42


def test_scalar_string_passes_through():
    assert coerce_value("base_commit",
                        "678392180e79228c1ab16cc04e4fb5ee63d48258") == \
        "678392180e79228c1ab16cc04e4fb5ee63d48258"


# ---------------------------------------------------------------------------
# task 0035: bracket-list syntax on LIST_FIELDS keys parses as a YAML list
# (was previously stored as a list-of-one-string '[a.py]').
# ---------------------------------------------------------------------------


def test_files_bracket_list_parses_to_real_list():
    """``--field files=[hello.py]`` must produce ``['hello.py']``,
    not ``['[hello.py]']``."""
    v = coerce_value("files", "[hello.py]")
    assert v == ["hello.py"], v


def test_files_bracket_list_multiple_items():
    v = coerce_value("files", "[a.py, b.py, c.py]")
    assert v == ["a.py", "b.py", "c.py"], v


def test_test_files_empty_bracket_list_is_empty_list():
    """``test_files=[]`` is the canonical 'no files' shape (TESTER
    audit-only path) — must parse to ``[]``, not ``['[]']``."""
    v = coerce_value("test_files", "[]")
    assert v == [], v


def test_dependencies_bracket_list_preserves_path_separators():
    """Bracket parsing must not split on path separators."""
    v = coerce_value("dependencies", "[feature_dev/0001.yaml, feature_dev/0002.yaml]")
    assert v == ["feature_dev/0001.yaml", "feature_dev/0002.yaml"], v


def test_bracket_list_only_applies_to_list_fields():
    """Non-LIST_FIELDS keys must keep bracket text literal — prose values
    like a stand_reason that happens to contain '[...]' must NOT be
    silently turned into a list."""
    v = coerce_value("stand_reason", "[POST /x, GET /y]")
    assert isinstance(v, str)
    assert v == "[POST /x, GET /y]"


def test_malformed_bracket_falls_back_to_comma_split():
    """Truly malformed bracket text (yaml.safe_load fails or returns
    non-list) must NOT crash the call — fall back to comma split."""
    # Unclosed bracket: yaml.safe_load tolerates this in flow style and
    # returns the list anyway — covered above. The genuinely malformed
    # case is e.g. nested unbalanced quotes that yaml refuses.
    # Whatever happens, coerce_value must return *something* without
    # raising.
    v = coerce_value("files", "[a.py, b.py")  # unclosed
    assert isinstance(v, list)
    assert v  # non-empty


# ---------------------------------------------------------------------------
# 2. task append-block end-to-end: --body-file alias + --body @PATH
# ---------------------------------------------------------------------------


def _setup_project(tmp_path: Path) -> Path:
    cp = subprocess.run(
        [sys.executable, "-m", "greatminds.cli.main", "setup",
         "--project-dir", str(tmp_path), "--lang", "en"],
        capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"setup failed: {cp.stderr}"
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


def _new_task(proj: Path, title: str, scope: str = "backend",
              kind: str = "feature") -> str:
    cp = _gm(proj, "task", "new", "--stream", "product",
             "--kind", kind, "--scope", scope, "--title", title)
    assert cp.returncode == 0, cp.stderr
    line = [l for l in cp.stdout.splitlines() if "feature_inbox" in l][0]
    return line.rsplit("/", 1)[-1].removesuffix(".yaml")


def test_append_block_body_file_loads_file(tmp_path: Path):
    """Regression for 1.1.0: --body-file was dropped from click wrapper."""
    import yaml
    proj = _setup_project(tmp_path)
    tid = _new_task(proj, "test --body-file works", scope="ui", kind="bugfix")

    body_path = tmp_path / "body.md"
    body_path.write_text(
        "## Body via --body-file\n"
        "Line with commas, colons: and dashes — preserved.\n"
    )

    assert _gm(proj, "task", "append-block", "triage", "--id", tid,
               "--body", "triaged").returncode == 0
    assert _gm(proj, "task", "mv", tid, "feature_plan",
               "--reason", "to plan").returncode == 0

    cp = _gm(proj, "task", "append-block", "plan", "--id", tid,
             "--field", "base_commit=abc123def456",
             "--field", "assignee_role=UI-DEVELOPER",
             "--field", "stand_required=true",
             "--field",
             "stand_reason=POST /x, then GET /y; owner: STAND-KEEPER",
             "--field", "plan_kind=bugfix",
             "--field", "mode=A",
             "--field", "ready_for_implementation=true",
             "--body-file", str(body_path))
    assert cp.returncode == 0, cp.stderr

    plan_files = list((proj / "coordination" / "feature_plan").glob("*.yaml"))
    assert len(plan_files) == 1
    data = yaml.safe_load(plan_files[0].read_text(encoding="utf-8"))
    plan = [b for b in data["blocks"] if b.get("kind") == "plan"][0]

    assert "Body via --body-file" in plan["body"]
    assert "commas, colons" in plan["body"]
    assert isinstance(plan["stand_reason"], str)
    assert "STAND-KEEPER" in plan["stand_reason"]


def test_append_block_body_at_path_alias(tmp_path: Path):
    """The plan orchestrator's --body @PATH form must keep working."""
    import yaml
    proj = _setup_project(tmp_path)
    tid = _new_task(proj, "test --body @path works")

    body_path = tmp_path / "triage.md"
    body_path.write_text("Triage via @PATH.\n")

    cp = _gm(proj, "task", "append-block", "triage", "--id", tid,
             "--body", f"@{body_path}")
    assert cp.returncode == 0, cp.stderr

    inbox_files = list((proj / "coordination" / "feature_inbox").glob("*.yaml"))
    data = yaml.safe_load(inbox_files[0].read_text(encoding="utf-8"))
    triage = [b for b in data["blocks"] if b.get("kind") == "triage"][0]
    assert "Triage via @PATH" in triage["notes"]


# 0247 (1.3.0): test_hosts_* tests REMOVED — `greatminds stand
# request` CLI is gone alongside the stand-stream queues. The new
# lease API does not expose --hosts at all (PROJECT.md per-profile
# deploy recipe owns hosts).


# ---------------------------------------------------------------------------
# task 0067: --evidence-for / --hosts / any _split_multivalue option must
# parse YAML bracket-list syntax. Was poisoning every stand_done with
# evidence_for: ['[<task-id>]'] which gate-check could never match.
# ---------------------------------------------------------------------------


def test_split_multivalue_unit_bracket_list_parses_to_real_list():
    """Direct unit test on the click callback: ``[a, b, c]`` → 3-element
    list of strings (NOT one literal '[a, b, c]')."""
    from greatminds.cli.task import _split_multivalue
    out = _split_multivalue(None, None, ("[task-a, task-b, task-c]",))
    assert out == ["task-a", "task-b", "task-c"], out


def test_split_multivalue_unit_bracket_with_single_item():
    """The minimal bracket-list shape ``[X]`` — exact repro from the
    USER bug: 'evidence_for' poisoned with '[<task-id>]'."""
    from greatminds.cli.task import _split_multivalue
    out = _split_multivalue(None, None, ("[0051-foo]",))
    assert out == ["0051-foo"], out


def test_split_multivalue_unit_bracket_empty_list():
    """Empty bracket-list ``[]`` → empty result (returned as None per
    the convention 'None when nothing was passed')."""
    from greatminds.cli.task import _split_multivalue
    out = _split_multivalue(None, None, ("[]",))
    # ``out`` is "or None" at the end, so empty list collapses to None.
    assert out is None, out


def test_split_multivalue_unit_comma_path_unchanged():
    """Comma-separated path remains unchanged (1.1.0 regression net)."""
    from greatminds.cli.task import _split_multivalue
    out = _split_multivalue(None, None, ("host-a,host-b,host-c",))
    assert out == ["host-a", "host-b", "host-c"], out


# 0247 (1.3.0): test_stand_request_*_bracket_list tests REMOVED —
# the stand_request CLI command is gone. The unit-level
# _split_multivalue bracket-list tests above still pin the
# regression fix (the helper is still used by other CLI options).


def test_append_block_rejects_both_body_and_body_file(tmp_path: Path):
    """Passing both flags is a usage error, not a silent precedence rule."""
    proj = _setup_project(tmp_path)
    tid = _new_task(proj, "test both flags fail cleanly")

    fp = tmp_path / "b.md"
    fp.write_text("body")

    cp = _gm(proj, "task", "append-block", "triage", "--id", tid,
             "--body", "literal", "--body-file", str(fp))
    assert cp.returncode != 0
    msg = (cp.stderr + cp.stdout).lower()
    assert "exactly one" in msg
