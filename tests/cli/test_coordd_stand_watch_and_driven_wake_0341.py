"""Tests for task 0341: coordd must route .stand lifecycle changes to
wake STAND-KEEPER — for the CURRENT (driven) fleet, and even when the
.stand dir is created lazily after coordd starts.

Two concrete gaps this closes (0269 wired the schema owner + the
chat-mode press_enter dispatch; 0341 covers what changed since):

1. Watch attachment: ``.stand`` is created lazily by the first
   ``stand lease`` (update_stand_state mkdir). If coordd starts before
   any lease, ``_add_initial_watches`` used to skip the missing dir, so
   the watch never attached. Because the direct queue→owner route fires
   on inotify events ONLY (the poll fallback yields no events), NO stand
   transition would ever wake STAND-KEEPER — it had to be nudged by
   hand. coordd now creates ``.stand`` up front so the watch always
   attaches.

2. Driven dispatch: STAND-KEEPER's window mode is now ``driven``, so a
   ``.stand`` change must run an SK turn via the driven driver (not the
   legacy press_enter path the 0269 fixture asserted).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

inotify_simple = pytest.importorskip("inotify_simple")

from greatminds.cli import coordd as coordd_mod
from greatminds.core.paths import find_canon_dir


# ---------- gap 1: watch attaches even when .stand is absent at start ----------


def test_watcher_creates_stand_dir_and_attaches_watch(tmp_path):
    """coordd starting before the first lease must still watch .stand —
    it creates the dir so the inotify watch attaches."""
    coord = tmp_path / "coordination"
    # The base coordination queues exist, but .stand does NOT yet (no
    # lease has ever been taken on this fresh project).
    for q in ("inbox", "feature_dev", "feature_test"):
        (coord / q).mkdir(parents=True)
    assert not (coord / ".stand").exists()

    watcher = coordd_mod._InotifyWatcher(coord, verbose=False)
    # .stand was created so the watch could attach...
    assert (coord / ".stand").is_dir(), (
        "0341: coordd must create .stand so its watch attaches even when "
        "no lease has been taken yet")
    # ...and it is registered in the wd→queue map as the .stand queue.
    assert ".stand" in set(watcher._wd_to_queue.values()), (
        "0341: .stand must be in the inotify wd→queue routing map")


def test_watcher_stand_event_fires_and_maps_to_stand(tmp_path):
    """Smoke: a state.yaml write under the (lazily-created) .stand dir
    produces an inotify event the watcher maps back to the .stand queue
    — the raw signal that drives the SK wake."""
    coord = tmp_path / "coordination"
    (coord / "inbox").mkdir(parents=True)
    watcher = coordd_mod._InotifyWatcher(coord, verbose=False)
    # Simulate the first lease writing state.yaml.
    (coord / ".stand" / "state.yaml").write_text(
        "state: preparing\n", encoding="utf-8")
    events = watcher.read_or_timeout(2.0)
    queues = {watcher.queue_for(ev.wd) for ev in events}
    assert ".stand" in queues, (
        f"0341: state.yaml write under .stand must surface a .stand event "
        f"(got queues {queues})")
    names = {ev.name for ev in events}
    assert "state.yaml" in names


# ---------- gap 2: driven SK is woken by a .stand change ----------


def _project_with_driven_sk(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": "stand", "role": "STAND-KEEPER",
             "tool": "claude", "mode": "driven"},
            {"name": "dev", "role": "DEVELOPER",
             "tool": "claude", "mode": "driven"},
        ],
    }), encoding="utf-8")
    return project


def test_route_stand_event_runs_coordd_deploy(tmp_path, monkeypatch):
    """1.6.0: a .stand change to `preparing` runs the COORDD deploy engine
    (deploy_lease) — the STAND-KEEPER agent is retired, so no driven turn
    and no press_enter fire."""
    project = _project_with_driven_sk(tmp_path)
    coord = project / "coordination"
    (coord / ".stand").mkdir(parents=True, exist_ok=True)
    import yaml as _y
    (coord / ".stand" / "state.yaml").write_text(_y.safe_dump({
        "state": "preparing", "queue": [], "history": [],
        "active_lease": {"lease_id": "L1", "profile": "full-deploy",
                         "worktree": str(coord.parent / "wt"),
                         "holder_role": "TESTER", "task": "0001"}}),
        encoding="utf-8")

    import threading
    done = threading.Event()
    monkeypatch.setattr("greatminds.cli.stand.deploy_lease",
                        lambda c, *, lease_id=None, **k: (done.set(),
                                                          (0, "ok"))[-1])
    monkeypatch.setattr(coordd_mod, "_spawn_driven_turn",
                        lambda *a, **kw: pytest.fail(
                            "1.6.0: .stand runs coordd deploy, not an SK turn"))
    coordd_mod._DEPLOYING_LEASES.discard("L1")

    woke = coordd_mod._route_queue_event(
        coord, find_canon_dir(), ".stand", "state.yaml", verbose=False)
    assert woke is True
    assert done.wait(timeout=3), "coordd must run deploy_lease"


def test_route_stand_event_noise_filtered(tmp_path, monkeypatch):
    """Watch-noise under .stand must not wake SK (regression net under
    the driven path)."""
    project = _project_with_driven_sk(tmp_path)
    coord = project / "coordination"
    monkeypatch.setattr(coordd_mod, "_spawn_driven_turn",
                        lambda *a, **kw: pytest.fail(
                            "0341: noise must not run a driven turn"))
    for fname in (".tmp.x", ".lock", "_TEMPLATE.yaml", "state.txt"):
        assert coordd_mod._route_queue_event(
            coord, find_canon_dir(), ".stand", fname, verbose=False) is False
