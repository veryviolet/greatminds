"""Tests for `greatminds migrate` — project-config migration.

Covers the coord.yaml driven-model migration (old all-paned → driven,
preserving session / worktrees override / custom windows, with backup)
and legacy-artifact removal. The canon coord.yaml.template is the source
of the new window roster.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from greatminds.cli import migrate as mg


_OLD_ALL_PANED = """\
session: myproj
project_dir: /opt/myproj
worktrees:
  default_branch: unify
windows:
  - {name: planner, role: ARCHITECT-PLANNER, mode: chat}
  - {name: dev, role: DEVELOPER, mode: loop}
  - {name: tester, role: TESTER, mode: loop}
  - {name: maintainer, role: MAINTAINER, mode: chat}
  - {name: ops, role: "", mode: bash}
"""


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "coord.yaml").write_text(body, encoding="utf-8")
    return tmp_path


# ---------- coord.yaml migration ----------


def test_migrates_old_all_paned_to_driven(tmp_path: Path):
    _write(tmp_path, _OLD_ALL_PANED)
    status, detail = mg.migrate_coord_yaml(tmp_path)
    assert status == "migrated", detail
    new = yaml.safe_load((tmp_path / "coord.yaml").read_text())
    modes = [(w.get("role"), w.get("mode")) for w in new["windows"]]
    # workers are now driven; a dashboard window exists.
    assert any(m == "driven" for _r, m in modes), modes
    assert any((w.get("name") == "dashboard") for w in new["windows"])


def test_migration_preserves_session_worktrees_and_custom_window(tmp_path: Path):
    _write(tmp_path, _OLD_ALL_PANED)
    mg.migrate_coord_yaml(tmp_path)
    new = yaml.safe_load((tmp_path / "coord.yaml").read_text())
    assert new["session"] == "myproj"
    assert new["project_dir"] == "/opt/myproj"
    # per-project worktrees override (default_branch) survives migration.
    assert new["worktrees"]["default_branch"] == "unify"
    # custom role-less window preserved.
    assert any(w.get("name") == "ops" for w in new["windows"])


def test_migration_backs_up_old(tmp_path: Path):
    _write(tmp_path, _OLD_ALL_PANED)
    mg.migrate_coord_yaml(tmp_path)
    bak = tmp_path / "coord.yaml.premigrate.bak"
    assert bak.is_file()
    assert "mode: chat" in bak.read_text()  # the old paned content


def test_already_driven_is_skipped(tmp_path: Path):
    body = ("session: x\nproject_dir: /x\nwindows:\n"
            "  - {name: planner, role: ARCHITECT-PLANNER, mode: chat}\n"
            "  - {name: dev, role: DEVELOPER, mode: driven}\n")
    _write(tmp_path, body)
    status, _d = mg.migrate_coord_yaml(tmp_path)
    assert status == "already-current"
    # unchanged + no backup written.
    assert (tmp_path / "coord.yaml").read_text() == body
    assert not (tmp_path / "coord.yaml.premigrate.bak").exists()


def test_no_coord_yaml(tmp_path: Path):
    status, _d = mg.migrate_coord_yaml(tmp_path)
    assert status == "no-file"


# ---------- legacy artifact removal ----------


def test_removes_legacy_root_files_and_empty_bot_queues(tmp_path: Path):
    (tmp_path / "command_START.yaml").write_text("x", encoding="utf-8")
    (tmp_path / "DEVELOPER.md").write_text("x", encoding="utf-8")
    (tmp_path / "keep_me.py").write_text("x", encoding="utf-8")  # product file
    coord = tmp_path / "coordination"
    (coord / "bot_inbox").mkdir(parents=True)            # empty → removed
    (coord / "bot_wip").mkdir()
    (coord / "bot_wip" / "0001.yaml").write_text("x", encoding="utf-8")  # non-empty

    removed = mg.remove_legacy_artifacts(tmp_path)

    assert "command_START.yaml" in removed
    assert "DEVELOPER.md" in removed
    assert "coordination/bot_inbox/" in removed
    assert not (tmp_path / "command_START.yaml").exists()
    assert (tmp_path / "keep_me.py").exists()             # product file untouched
    assert (coord / "bot_wip").is_dir()                   # non-empty queue kept
    assert "coordination/bot_wip/" not in removed


# ---------- canonical roster loads from canon template ----------


def test_canonical_windows_from_template():
    wins = mg._canonical_windows("sess", "/p")
    roles = {(w.get("role") or "").upper() for w in wins}
    modes = {(w.get("mode") or "") for w in wins}
    assert "ARCHITECT-PLANNER" in roles
    assert "driven" in modes                              # workers driven
    assert any(w.get("name") == "dashboard" for w in wins)
