"""`greatminds task list <queue> --json` — machine-readable queue listing.

Agents (e.g. a codex PLANNER) inspect queues via the CLI; before this
flag there was no structured output, so they guessed ``--json`` and
errored. --json emits id/title/queue/file per task; plain output stays
filename-only (back-compat).
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from greatminds.cli import main as main_mod


def _queue(tmp_path: Path):
    q = tmp_path / "coordination" / "feature_inbox"
    q.mkdir(parents=True)
    (q / "0001-do-thing.yaml").write_text(
        "id: 0001-do-thing\ntitle: do the thing\nkind: bugfix\n",
        encoding="utf-8")
    (q / "_TEMPLATE.yaml").write_text("id: t\n", encoding="utf-8")
    return tmp_path


def test_task_list_json_emits_records(tmp_path, monkeypatch):
    _queue(tmp_path)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(
        main_mod.cli, ["task", "list", "feature_inbox", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert len(data) == 1                       # _TEMPLATE excluded
    rec = data[0]
    assert rec["id"] == "0001-do-thing"
    assert rec["title"] == "do the thing"
    assert rec["queue"] == "feature_inbox"
    assert rec["file"] == "0001-do-thing.yaml"


def test_task_list_plain_still_filenames(tmp_path, monkeypatch):
    _queue(tmp_path)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main_mod.cli, ["task", "list", "feature_inbox"])
    assert res.exit_code == 0, res.output
    assert "0001-do-thing.yaml" in res.output
    assert "do the thing" not in res.output     # plain = filenames only


def test_task_list_json_empty_queue(tmp_path, monkeypatch):
    (tmp_path / "coordination" / "feature_plan").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(
        main_mod.cli, ["task", "list", "feature_plan", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == []
