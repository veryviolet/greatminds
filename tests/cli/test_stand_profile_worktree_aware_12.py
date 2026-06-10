"""GitHub issue #12: coordd's deterministic stand deploy loaded the stand
profile from the MAIN coordination tree, never the active lease's worktree
— so a stand-profile fix under review could not be deployed/validated
before merge (it silently ran the unchanged main copy).

load_profile now accepts ``worktree=`` and prefers
``<worktree>/coordination/stand-profiles/<name>`` over the main tree,
recording which tree won on ``ProfileSpec.source``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_profile as sp
from greatminds.core.errors import GreatMindsError


def _playbook(host: str) -> dict:
    return {"name": "full-deploy", "hosts": host,
            "tasks": [{"name": "ping", "ansible.builtin.ping": None}]}


def _write(coord: Path, name: str, data: dict) -> None:
    d = coord / sp.STAND_PROFILES_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """Return (main_coord, worktree_root)."""
    main = tmp_path / "main" / "coordination"
    main.mkdir(parents=True)
    wt = tmp_path / "wt"
    (wt / "coordination").mkdir(parents=True)
    return main, wt


def test_no_worktree_loads_main_source_main(tmp_path):
    main, _ = _layout(tmp_path)
    _write(main, "full-deploy", _playbook("main-host"))
    spec = sp.load_profile(main, "full-deploy")
    assert spec.source == "main"
    assert spec.yaml_data["hosts"] == "main-host"


def test_worktree_copy_preferred_over_main(tmp_path):
    """When the lease worktree has its own copy, it wins and is the one
    actually parsed (content differs from main)."""
    main, wt = _layout(tmp_path)
    _write(main, "full-deploy", _playbook("main-host"))
    _write(wt / "coordination", "full-deploy", _playbook("worktree-host"))

    spec = sp.load_profile(main, "full-deploy", worktree=wt)

    assert spec.source == "lease-worktree"
    assert spec.yaml_data["hosts"] == "worktree-host"
    assert str(wt) in str(spec.path)


def test_worktree_without_profile_falls_back_to_main(tmp_path):
    """A worktree that lacks the profile cleanly falls back to the main
    tree (source=main), no error."""
    main, wt = _layout(tmp_path)
    _write(main, "full-deploy", _playbook("main-host"))
    # worktree coordination/ exists but has no stand-profiles/full-deploy

    spec = sp.load_profile(main, "full-deploy", worktree=wt)

    assert spec.source == "main"
    assert spec.yaml_data["hosts"] == "main-host"


def test_relative_or_bad_worktree_falls_back(tmp_path):
    """A worktree path that does not resolve to a real profile dir just
    falls back to main — never crashes (issue #10 stores relative paths)."""
    main, _ = _layout(tmp_path)
    _write(main, "full-deploy", _playbook("main-host"))

    spec = sp.load_profile(main, "full-deploy",
                           worktree="does/not/exist")
    assert spec.source == "main"


def test_missing_everywhere_names_both_paths(tmp_path):
    main, wt = _layout(tmp_path)  # neither tree has the profile
    with pytest.raises(GreatMindsError) as ei:
        sp.load_profile(main, "ghost", worktree=wt)
    msg = str(ei.value)
    # both searched yaml paths surfaced
    assert str(wt) in msg
    assert str(main) in msg
