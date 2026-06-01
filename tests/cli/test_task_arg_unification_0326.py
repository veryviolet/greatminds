"""Tests for task 0326 (DOD2): unified task-id intake across the
``greatminds task`` subcommands.

Pre-0326 each subcommand took the id differently — agents guessed the
form every time:
  - ``task show <ID>`` short id only (a full filename → "not found");
  - ``task validate`` used ``--id`` / ``--file`` (positional rejected);
  - ``task paths`` took NO arg (``task paths <ID>`` → extra argument).

0326 routes every id-taking subcommand through ``find_task``, which now
also accepts a full filename and a path (absolute / cwd-relative /
coordination-relative), resolving identically with one error shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from greatminds.cli import task as task_mod
from greatminds.cli.task import find_task


def _make_task(coord: Path, queue: str, stem: str) -> Path:
    qdir = coord / queue
    qdir.mkdir(parents=True, exist_ok=True)
    p = qdir / f"{stem}.yaml"
    p.write_text(
        f"id: {stem}\nstream: product\ntitle: t\n"
        f"reporter: tester-agent\nopened_at: '2026-06-02T00:00:00Z'\n"
        f"priority: normal\nkind: feature\nscope: backend\n",
        encoding="utf-8")
    return p


# ---------- find_task: new filename + path shapes ----------


def test_find_task_full_filename_with_suffix(tmp_path: Path) -> None:
    _make_task(tmp_path, "feature_dev", "0326-foo")
    found = find_task(tmp_path, "0326-foo.yaml")
    assert found is not None and found[1] == "feature_dev"
    assert found[0].name == "0326-foo.yaml"


def test_find_task_coord_relative_path(tmp_path: Path) -> None:
    _make_task(tmp_path, "feature_dev", "0326-foo")
    found = find_task(tmp_path, "feature_dev/0326-foo.yaml")
    assert found is not None and found[0].name == "0326-foo.yaml"


def test_find_task_absolute_path(tmp_path: Path) -> None:
    p = _make_task(tmp_path, "feature_test", "0326-bar")
    found = find_task(tmp_path, str(p.resolve()))
    assert found is not None and found[0].resolve() == p.resolve()
    assert found[1] == "feature_test"


def test_find_task_all_forms_resolve_to_same_file(tmp_path: Path) -> None:
    p = _make_task(tmp_path, "feature_dev", "0326-foo")
    forms = ["0326", "0326-foo", "0326-foo.yaml",
             "feature_dev/0326-foo.yaml", str(p.resolve())]
    resolved = {find_task(tmp_path, f)[0].resolve() for f in forms}
    assert resolved == {p.resolve()}, (
        "0326: every id form must resolve to the same task file")


def test_find_task_unknown_returns_none(tmp_path: Path) -> None:
    _make_task(tmp_path, "feature_dev", "0326-foo")
    assert find_task(tmp_path, "9999") is None
    assert find_task(tmp_path, "feature_dev/9999-nope.yaml") is None


# ---------- CLI subcommand consistency ----------


@pytest.fixture()
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    coord = proj / "coordination"
    _make_task(coord, "feature_dev", "0326-foo")
    monkeypatch.chdir(proj)
    return proj


def _run(args: list[str]):
    return CliRunner().invoke(task_mod.task, args, catch_exceptions=False)


def test_show_accepts_short_id_filename_and_path(project) -> None:
    for form in ("0326", "0326-foo.yaml", "feature_dev/0326-foo.yaml"):
        res = _run(["show", form])
        assert res.exit_code == 0, f"show {form!r}: {res.output}"
        assert "queue: feature_dev" in res.output


def test_validate_positional_id_works(project) -> None:
    for form in ("0326", "0326-foo.yaml", "feature_dev/0326-foo.yaml"):
        res = _run(["validate", form])
        assert res.exit_code == 0, f"validate {form!r}: {res.output}"
        assert "valid:" in res.output


def test_validate_back_compat_id_and_file(project) -> None:
    res = _run(["validate", "--id", "0326"])
    assert res.exit_code == 0, res.output
    path = project / "coordination" / "feature_dev" / "0326-foo.yaml"
    res2 = _run(["validate", "--file", str(path)])
    assert res2.exit_code == 0, res2.output


def test_paths_with_id_prints_task_path(project) -> None:
    res = _run(["paths", "0326"])
    assert res.exit_code == 0, res.output
    assert "queue: feature_dev" in res.output
    assert "0326-foo.yaml" in res.output


def test_paths_without_id_prints_global(project) -> None:
    res = _run(["paths"])
    assert res.exit_code == 0, res.output
    assert "coord:" in res.output and "canon:" in res.output


def test_unknown_id_consistent_error_across_subcommands(project) -> None:
    for sub in (["show", "9999"], ["validate", "9999"], ["paths", "9999"]):
        res = CliRunner().invoke(task_mod.task, sub, catch_exceptions=True)
        out = (res.output or "") + str(res.exception or "")
        assert "9999 not found" in out, f"{sub}: {out}"
