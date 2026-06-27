"""2.6.1 stand failure recovery regressions."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from greatminds.cli import coordd as cd
from greatminds.cli import stand as stand_mod
from greatminds.cli import stand_state as ss
from greatminds.cli.stand_executor import deploy_marker_path


def _coord(tmp_path: Path) -> Path:
    coord = tmp_path / "proj" / "coordination"
    (coord / ".stand").mkdir(parents=True)
    return coord


def _lease(coord: Path, lease_id: str = "l1") -> dict:
    wt = coord.parent / ".worktrees" / "0001"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: dummy\n", encoding="utf-8")
    return {
        "lease_id": lease_id,
        "task": "0001",
        "worktree": str(wt),
        "profile": "full-deploy",
        "profile_file": "full-deploy.yaml",
        "holder_role": "TESTER",
        "ttl_seconds": 14400,
        "granted_at": ss.now_iso(),
        "ready_at": None,
    }


def test_deploy_failure_frees_singleton_and_records_full_log(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    lease = _lease(coord)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": lease,
        "queue": [],
    }))

    marker = deploy_marker_path(coord, "l1")

    class Spec:
        name = "full-deploy"
        format = "yaml"
        path = coord / "stand-profiles" / "full-deploy.yaml"
        source = "lease-worktree"
        host = "stand-host"

    monkeypatch.setattr("greatminds.cli.stand_profile.load_profile",
                        lambda *a, **k: Spec())
    log = "x" * 500 + " address already in use on port 4173"

    def fake_dispatch(_spec, _meta, **_kwargs):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(log, encoding="utf-8")
        return 2, log

    monkeypatch.setattr("greatminds.cli.stand_executor.dispatch_profile",
                        fake_dispatch)
    monkeypatch.setattr(stand_mod, "_file_inbox_info", lambda *a, **k: None)
    monkeypatch.setattr(stand_mod, "_teardown_lease", lambda *a, **k: None)

    rc, _ = stand_mod.deploy_lease(coord, lease_id="l1")
    state = ss.read_stand_state(coord)

    assert rc == 2
    assert state["state"] == "free"
    assert state["active_lease"] is None
    failure = state["last_deploy_failure"]
    assert failure["deploy_log"] == str(marker)
    assert "address already in use" in failure["reason"]
    assert "full log:" in failure["reason"]


def test_stand_status_prints_last_deploy_failure(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "free",
        "last_deploy_failure": {
            "lease_id": "l1",
            "task": "0001",
            "profile": "vite-dev",
            "deploy_log": str(coord / ".stand" / "deploy-l1.log"),
            "reason": "deploy rc=2; full log: x",
        },
    }))
    monkeypatch.chdir(coord.parent)
    res = CliRunner().invoke(stand_mod.stand, ["status"])
    assert res.exit_code == 0
    assert "last_deploy_failure:" in res.output
    assert "deploy_log:" in res.output


def test_stand_free_event_reconciles_driven_backlog(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({"state": "free"}))
    calls = []
    monkeypatch.setattr(cd, "_maybe_auto_deploy_stand", lambda *a, **k: False)
    monkeypatch.setattr(cd, "_reconcile_driven_backlog",
                        lambda *a, **k: calls.append((a, k)))
    assert cd._route_queue_event(coord, tmp_path, ".stand", "state.yaml",
                                 verbose=False) is True
    assert calls


def test_coordd_auto_deploy_free_reconciles_without_watcher(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    lease = _lease(coord)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "active_lease": lease,
        "queue": [],
    }))
    calls = []

    def fake_deploy(coord_arg, *, lease_id=None, **_kwargs):
        assert coord_arg == coord
        assert lease_id == "l1"

        def free(state):
            state["state"] = "free"
            state["active_lease"] = None
        ss.update_stand_state(coord, free)
        return 118, "failed"

    monkeypatch.setattr("greatminds.cli.stand.deploy_lease", fake_deploy)
    monkeypatch.setattr(cd, "find_canon_dir", lambda: tmp_path / "canon")
    monkeypatch.setattr(cd, "_reconcile_driven_backlog",
                        lambda *a, **k: calls.append((a, k)))

    assert cd._maybe_auto_deploy_stand(coord, verbose=False,
                                       run_async=False) is True
    assert calls
    assert calls[0][1]["seen"] is None
    assert calls[0][1]["trigger"] == " (stand-free)"


def test_stand_up_emits_available_event_for_coordd(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "down",
        "down_reason": "fixed",
        "queue": [],
    }))
    monkeypatch.chdir(coord.parent)
    monkeypatch.setenv("GREATMINDS_ROLE", "MAINTAINER")
    monkeypatch.setattr(stand_mod, "_teardown_lease", lambda *a, **k: None)

    res = CliRunner().invoke(stand_mod.stand, ["up", "--reason", "clean"])

    assert res.exit_code == 0, res.output
    events = list((coord / ".stand").glob("available-*.yaml"))
    assert events
    assert "stand-up: clean" in events[0].read_text(encoding="utf-8")


def test_generic_vite_cleanup_runs_without_profile_teardown_tags(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    (coord / "PROJECT.env").write_text(
        "STAND_HOST=lattice-a\nVITE_DEV_PORT=4173\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        class CP:
            returncode = 0
            stdout = ""
            stderr = ""
        return CP()

    monkeypatch.setattr(stand_mod.subprocess, "run", fake_run)

    stand_mod._generic_teardown_lease_resources(
        coord, {"profile": "vite-dev", "lease_id": "l1"},
        reason="test")

    assert calls
    assert calls[0][0][0] == "ssh"
    assert calls[0][0][1] == "lattice-a"
    assert "4173/tcp" in calls[0][0][-1]


def test_cleanup_free_stand_orphans_clears_declared_vite_port(
        tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "free",
        "active_lease": None,
    }))
    (coord / "PROJECT.env").write_text(
        "STAND_HOST=lattice-a\nVITE_DEV_PORT=4173\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(stand_mod, "_cleanup_vite_port",
                        lambda host, port: calls.append((host, port)))

    assert stand_mod.cleanup_free_stand_orphans(coord) is True

    assert calls == [("lattice-a", "4173")]


def test_cleanup_free_stand_orphans_skips_active_lease(tmp_path, monkeypatch):
    coord = _coord(tmp_path)
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": {"lease_id": "l1"},
    }))
    (coord / "PROJECT.env").write_text("VITE_DEV_PORT=4173\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(stand_mod, "_cleanup_vite_port",
                        lambda host, port: calls.append((host, port)))

    assert stand_mod.cleanup_free_stand_orphans(coord) is False

    assert calls == []
