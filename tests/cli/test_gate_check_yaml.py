"""Regression tests for `greatminds gate-check` on the yaml-native schema.

Bug context: lattice's MAINTAINER kept hand-patching gate_check.py through
0.1.1 → 1.0.0 → 1.1.2 because the reader still assumed fenced markdown +
top-level keys + result='pass'. Five sub-issues, all in the same parser
path. The legacy fenced .md path must keep working — lattice has ~151
existing stand_done/*.md files.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import gate_check as gc_mod


SCHEMA_QUEUES_YAML = """\
version: 1
queues:
  feature_inbox: {owner: P, writers: [P], kind: active}
  feature_plan:  {owner: P, writers: [P], kind: active}
  feature_dev:   {owner: D, writers: [D], kind: active}
  feature_test:  {owner: T, writers: [T], kind: active}
  feature_review:{owner: R, writers: [R], kind: active}
  verified:      {owner: R, writers: [R], kind: terminal}
  stand_done:    {owner: S, writers: [S], kind: terminal}
"""


@pytest.fixture
def canon_dir(tmp_path_factory):
    """A minimal canon dir with schema.yaml that lists product queues."""
    d = tmp_path_factory.mktemp("canon")
    (d / "schema.yaml").write_text(SCHEMA_QUEUES_YAML, encoding="utf-8")
    return d


def _make_project(tmp_path: Path) -> Path:
    """Empty coordination tree."""
    coord = tmp_path / "coordination"
    for q in ("feature_inbox", "feature_plan", "feature_dev",
              "feature_test", "feature_review", "verified", "stand_done"):
        (coord / q).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _yaml_task(
    project_dir: Path,
    queue: str,
    task_id: str,
    *,
    stand_required: bool = True,
    base_commit: str = "abc1234",
    evidence_for_top: list[str] | None = None,
) -> Path:
    """Write a yaml-native task file (blocks list, no fences)."""
    doc = {
        "id": task_id,
        "stream": "product",
        "title": "x",
        "blocks": [
            {
                "kind": "plan",
                "by": "ARCHITECT-PLANNER",
                "stand_required": stand_required,
                "base_commit": base_commit,
                "ready_for_implementation": True,
            },
            {
                "kind": "implementation",
                "by": "DEVELOPER",
                "base_commit": base_commit,
                "ready_for_test": True,
            },
        ],
    }
    if evidence_for_top is not None:
        doc["evidence_for"] = evidence_for_top
    path = project_dir / "coordination" / queue / f"{task_id}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _yaml_stand_done(
    project_dir: Path,
    stand_id: str,
    *,
    result: str = "ok",
    commit: str = "abc1234",
    evidence_for: list[str] | None = None,
    evidence_for_top: list[str] | None = None,
) -> Path:
    """Write a yaml-native stand_done evidence file."""
    sr: dict = {
        "kind": "stand_result",
        "by": "STAND-KEEPER",
        "result": result,
        "commit": commit,
    }
    if evidence_for is not None:
        sr["evidence_for"] = evidence_for
    doc: dict = {
        "id": stand_id,
        "stream": "stand",
        "blocks": [sr],
    }
    if evidence_for_top is not None:
        doc["evidence_for"] = evidence_for_top
    path = project_dir / "coordination" / "stand_done" / f"{stand_id}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _md_task(
    project_dir: Path,
    queue: str,
    task_id: str,
    *,
    stand_required: bool = True,
    base_commit: str = "abc1234",
) -> Path:
    """Write a legacy fenced .md task file."""
    plan = yaml.safe_dump({
        "plan": {
            "by": "ARCHITECT-PLANNER",
            "stand_required": stand_required,
            "base_commit": base_commit,
            "ready_for_implementation": True,
        },
    }).strip()
    impl = yaml.safe_dump({
        "implementation": {
            "by": "DEVELOPER",
            "base_commit": base_commit,
            "ready_for_test": True,
        },
    }).strip()
    front = yaml.safe_dump({"id": task_id, "stream": "product", "title": "x"}).strip()
    text = f"---\n{front}\n---\n\n# {task_id}\n\n---\n{plan}\n---\n\n---\n{impl}\n---\n"
    path = project_dir / "coordination" / queue / f"{task_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _md_stand_done(
    project_dir: Path,
    stand_id: str,
    *,
    result: str = "pass",
    commit: str = "abc1234",
    evidence_for: list[str] | None = None,
) -> Path:
    """Write a legacy fenced .md stand_done evidence file."""
    sr_payload: dict = {
        "stand_result": {
            "by": "STAND-KEEPER",
            "result": result,
            "commit": commit,
        },
    }
    if evidence_for is not None:
        sr_payload["stand_result"]["evidence_for"] = evidence_for
    front = yaml.safe_dump({"id": stand_id, "stream": "stand"}).strip()
    sr = yaml.safe_dump(sr_payload).strip()
    text = f"---\n{front}\n---\n\n# {stand_id}\n\n---\n{sr}\n---\n"
    path = project_dir / "coordination" / "stand_done" / f"{stand_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path: Path, canon_dir: Path, task_id: str):
    return CliRunner().invoke(
        gc_mod.gate_check,
        [task_id, "--project-dir", str(tmp_path), "--canon-dir", str(canon_dir)],
        catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Sub-issue 1 + 5 + 4: yaml task + yaml stand_done + result=ok + top-level evidence_for
# ---------------------------------------------------------------------------


def test_yaml_native_task_with_yaml_stand_evidence_passes(tmp_path, canon_dir):
    """Pure yaml-native path: pass."""
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0421-foo",
               evidence_for_top=["0421-foo"])
    _yaml_stand_done(tmp_path, "0099-stand-for-0421",
                     result="ok",
                     evidence_for_top=["0421-foo"])
    result = _run(tmp_path, canon_dir, "0421-foo")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 2: no regression on legacy fenced .md path
# ---------------------------------------------------------------------------


def test_legacy_md_task_with_md_stand_evidence_still_passes(tmp_path, canon_dir):
    _make_project(tmp_path)
    _md_task(tmp_path, "verified", "0099-legacy")
    _md_stand_done(tmp_path, "0099-stand", result="pass",
                   evidence_for=["0099-legacy"])
    result = _run(tmp_path, canon_dir, "0099-legacy")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 3: mixed shapes — yaml task + legacy md evidence (and vice versa)
# ---------------------------------------------------------------------------


def test_yaml_task_with_legacy_md_stand_evidence(tmp_path, canon_dir):
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0301-mix")
    _md_stand_done(tmp_path, "0301-stand", result="pass",
                   evidence_for=["0301-mix"])
    result = _run(tmp_path, canon_dir, "0301-mix")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


def test_legacy_md_task_with_yaml_stand_evidence(tmp_path, canon_dir):
    _make_project(tmp_path)
    _md_task(tmp_path, "verified", "0302-mix")
    _yaml_stand_done(tmp_path, "0302-stand",
                     result="ok",
                     evidence_for_top=["0302-mix"])
    result = _run(tmp_path, canon_dir, "0302-mix")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 4: result=fail / result=partial → outcome 'fail'
# ---------------------------------------------------------------------------


def test_yaml_task_with_fail_result(tmp_path, canon_dir):
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0501-fail")
    _yaml_stand_done(tmp_path, "0501-stand", result="fail",
                     evidence_for_top=["0501-fail"])
    result = _run(tmp_path, canon_dir, "0501-fail")
    assert result.exit_code == 1, result.output
    assert "fail" in result.output


def test_yaml_task_with_partial_result(tmp_path, canon_dir):
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0502-partial")
    _yaml_stand_done(tmp_path, "0502-stand", result="partial",
                     evidence_for_top=["0502-partial"])
    result = _run(tmp_path, canon_dir, "0502-partial")
    assert result.exit_code == 1, result.output
    assert "fail" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 6: missing stand_done evidence → 'missing'
# ---------------------------------------------------------------------------


def test_yaml_task_missing_stand_evidence(tmp_path, canon_dir):
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0601-missing")
    # No stand_done file written.
    result = _run(tmp_path, canon_dir, "0601-missing")
    assert result.exit_code == 2, result.output
    assert "missing" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 7: _TEMPLATE.{md,yaml} skipped by stem check
# ---------------------------------------------------------------------------


def test_template_files_not_picked_as_task(tmp_path, canon_dir):
    _make_project(tmp_path)
    # Put both shapes of _TEMPLATE in the verified queue alongside the real task.
    (tmp_path / "coordination" / "verified" / "_TEMPLATE.md").write_text(
        "---\nid: TEMPLATE\n---\n", encoding="utf-8")
    (tmp_path / "coordination" / "verified" / "_TEMPLATE.yaml").write_text(
        "id: TEMPLATE\n", encoding="utf-8")
    _yaml_task(tmp_path, "verified", "0701-real")
    _yaml_stand_done(tmp_path, "0701-stand",
                     evidence_for_top=["0701-real"])
    result = _run(tmp_path, canon_dir, "0701-real")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


# ---------------------------------------------------------------------------
# Sub-issue 8: evidence_for only at top-level (NOT inside stand_result)
# ---------------------------------------------------------------------------


def test_evidence_for_top_level_only_resolves_via_fallback(tmp_path, canon_dir):
    """evidence_for at file-top of stand_done; absent inside stand_result."""
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0801-toplevel")
    # Explicitly: stand_result block carries NO evidence_for; file top does.
    _yaml_stand_done(tmp_path, "0801-stand",
                     evidence_for=None,
                     evidence_for_top=["0801-toplevel"])
    result = _run(tmp_path, canon_dir, "0801-toplevel")
    assert result.exit_code == 0, result.output
    assert "pass" in result.output


# ---------------------------------------------------------------------------
# Bonus: stand_required:false short-circuits to 'n/a' on yaml-native
# ---------------------------------------------------------------------------


def test_yaml_task_with_stand_required_false_is_na(tmp_path, canon_dir):
    _make_project(tmp_path)
    _yaml_task(tmp_path, "verified", "0901-na", stand_required=False)
    result = _run(tmp_path, canon_dir, "0901-na")
    assert result.exit_code == 0, result.output
    assert "n/a" in result.output
