"""Tests for task 0243 (0242a, Phase 1 of 0242 stand redesign):
schema + .stand/state.yaml + ``greatminds stand status``.

Phase 1 ships read-only — the state file IO + status CLI. Mutating
ops (lease/release/down/up) land in 0244 and beyond. fcntl-protected
write helpers are already in place here for use by those tasks.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time
from pathlib import Path

import pytest
import yaml

from greatminds.cli import stand_state as ss
from greatminds.cli import stand as stand_mod
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


# ---------- schema pin ----------


def test_schema_has_stand_resource_section() -> None:
    """0243 schema pin: ``stand.resource`` carries states +
    transitions + lease config + access_control + state_file."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    resource = (doc.get("stand") or {}).get("resource")
    assert resource is not None, "0243: schema.stand.resource missing"
    assert resource.get("states") == ["free", "preparing", "ready", "down"]
    assert resource.get("queue") == "fifo"
    lease = resource.get("lease") or {}
    assert lease.get("ttl_seconds_default") == 14400
    assert lease.get("auto_release_on_no_response") is True
    assert resource.get("state_file") == ".stand/state.yaml"


def test_schema_transitions_cover_all_state_pairs() -> None:
    """Every documented transition has a from/to/by."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    transitions = (doc["stand"]["resource"].get("transitions") or [])
    pairs = {(t["from"], t["to"]) for t in transitions}
    # Plan-mandated transitions:
    assert ("free", "preparing") in pairs
    assert ("preparing", "ready") in pairs
    assert ("preparing", "down") in pairs
    assert ("ready", "free") in pairs
    assert ("ready", "down") in pairs
    assert ("down", "free") in pairs


# ---------- read_stand_state ----------


def test_read_returns_empty_state_when_file_missing(tmp_path: Path) -> None:
    """Greenfield project: no state.yaml → empty-state view (no
    crash). Callers don't need to special-case bootstrap."""
    coord = tmp_path / "coordination"
    coord.mkdir()
    state = ss.read_stand_state(coord)
    assert state["state"] == "free"
    assert state["active_lease"] is None
    assert state["queue"] == []
    assert state["history"] == []


def test_read_returns_parsed_state(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    sp = ss.state_file_path(coord)
    sp.parent.mkdir(parents=True)
    sp.write_text(yaml.safe_dump({
        "state": "preparing",
        "active_lease": {"lease_id": "abc-123", "task": "0099-foo",
                         "holder_role": "TESTER"},
        "queue": [],
    }), encoding="utf-8")

    state = ss.read_stand_state(coord)
    assert state["state"] == "preparing"
    assert state["active_lease"]["lease_id"] == "abc-123"


def test_read_fills_missing_keys_defensively(tmp_path: Path) -> None:
    """File with partial keys (e.g. legacy / hand-edited) → loader
    fills in defaults so callers always see the full shape."""
    coord = tmp_path / "coordination"
    sp = ss.state_file_path(coord)
    sp.parent.mkdir(parents=True)
    sp.write_text(yaml.safe_dump({"state": "down"}), encoding="utf-8")
    state = ss.read_stand_state(coord)
    assert state["state"] == "down"
    assert state["queue"] == []          # default-filled
    assert state["history"] == []         # default-filled


def test_read_raises_on_malformed_yaml(tmp_path: Path) -> None:
    coord = tmp_path / "coordination"
    sp = ss.state_file_path(coord)
    sp.parent.mkdir(parents=True)
    sp.write_text("{ this is not yaml at all", encoding="utf-8")
    with pytest.raises(GreatMindsError) as exc:
        ss.read_stand_state(coord)
    assert "stand state file" in str(exc.value).lower()


def test_read_raises_on_non_mapping_top_level(tmp_path: Path) -> None:
    """A list/scalar at the top would break every consumer; reject
    early with a clear error."""
    coord = tmp_path / "coordination"
    sp = ss.state_file_path(coord)
    sp.parent.mkdir(parents=True)
    sp.write_text(yaml.safe_dump([1, 2, 3]), encoding="utf-8")
    with pytest.raises(GreatMindsError) as exc:
        ss.read_stand_state(coord)
    assert "mapping" in str(exc.value).lower()


# ---------- update_stand_state ----------


def test_update_writes_state_file(tmp_path: Path) -> None:
    """The mutator's modifications are persisted under fcntl."""
    coord = tmp_path / "coordination"

    def mutator(state):
        state["state"] = "preparing"
        state["active_lease"] = {"lease_id": "xyz"}

    ss.update_stand_state(coord, mutator)

    persisted = ss.read_stand_state(coord)
    assert persisted["state"] == "preparing"
    assert persisted["active_lease"] == {"lease_id": "xyz"}


def test_update_rejects_invalid_state(tmp_path: Path) -> None:
    """0243 contract: only the documented states are valid. Catches
    typos before they corrupt the FSM."""
    coord = tmp_path / "coordination"

    def mutator(state):
        state["state"] = "definitely-not-a-state"

    with pytest.raises(GreatMindsError) as exc:
        ss.update_stand_state(coord, mutator)
    assert "free" in str(exc.value)


def test_update_is_idempotent_across_runs(tmp_path: Path) -> None:
    """Two consecutive updates with the same mutation result in the
    same persisted state."""
    coord = tmp_path / "coordination"
    ss.update_stand_state(coord, lambda s: s.update({"state": "ready"}))
    ss.update_stand_state(coord, lambda s: s.update({"state": "ready"}))
    assert ss.read_stand_state(coord)["state"] == "ready"


def test_update_concurrent_writes_serialize(tmp_path: Path) -> None:
    """0243 fcntl pin: two concurrent writers see deterministic
    serialization, not a corrupted half-written file. The lock
    enforces 'one writer at a time'; the OUTCOME of two writes
    racing is implementation-defined (either ordering); both must
    leave a valid yaml file with one of their target states."""
    coord = tmp_path / "coordination"
    # Seed the state file so each writer reads an existing valid one.
    ss.update_stand_state(coord, lambda s: s.update({"state": "free"}))

    def make_writer(label):
        def mutator(state):
            state["state"] = "preparing"
            state["last_state_change_by"] = label
            # tiny sleep increases overlap odds without ballooning test
            time.sleep(0.01)
        return mutator

    t1 = threading.Thread(target=ss.update_stand_state,
                          args=(coord, make_writer("T1")))
    t2 = threading.Thread(target=ss.update_stand_state,
                          args=(coord, make_writer("T2")))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # File parses cleanly and last_state_change_by names ONE of the
    # writers (not a partial / null / corrupt value).
    state = ss.read_stand_state(coord)
    assert state["state"] == "preparing"
    assert state["last_state_change_by"] in ("T1", "T2")


# ---------- record_transition ----------


def test_record_transition_appends_to_history(tmp_path: Path) -> None:
    state = ss._empty_state()
    ss.record_transition(state, "free", "preparing", "STAND-KEEPER",
                          lease_id="abc-123", reason="new lease")
    assert state["state"] == "preparing"
    assert state["last_state_change_by"] == "STAND-KEEPER"
    assert len(state["history"]) == 1
    entry = state["history"][0]
    assert entry["from"] == "free"
    assert entry["to"] == "preparing"
    assert entry["by"] == "STAND-KEEPER"
    assert entry["lease_id"] == "abc-123"


def test_record_transition_caps_history_length() -> None:
    """0243 contract: history bounded to HISTORY_TAIL_LEN so the
    file doesn't grow unbounded across a long-running project."""
    state = ss._empty_state()
    for i in range(ss.HISTORY_TAIL_LEN + 5):
        ss.record_transition(state, "free", "preparing", f"R{i}")
    assert len(state["history"]) == ss.HISTORY_TAIL_LEN
    # The most recent entry survived.
    assert state["history"][-1]["by"] == f"R{ss.HISTORY_TAIL_LEN + 4}"


# ---------- stand status CLI ----------


def test_status_renders_free_empty(tmp_path: Path, monkeypatch) -> None:
    """No state file → status shows free/no-lease/empty-queue."""
    from click.testing import CliRunner
    coord = tmp_path / "coordination"
    coord.mkdir()
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(stand_mod.stand, ["status"])
    assert result.exit_code == 0, result.output
    assert "state: free" in result.output
    assert "active_lease: (none)" in result.output
    assert "queue: (empty)" in result.output


def test_status_renders_active_lease(tmp_path: Path, monkeypatch) -> None:
    """Mid-lease state: status names the active lease + ttl."""
    coord = tmp_path / "coordination"
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "ready",
        "active_lease": {
            "lease_id": "abc-123-uuid",
            "task": "0199-test-task",
            "worktree": "/tmp/wt",
            "holder_role": "TESTER",
            "ttl_seconds": 14400,
        },
    }))
    monkeypatch.chdir(tmp_path)

    from click.testing import CliRunner
    result = CliRunner().invoke(stand_mod.stand, ["status"])
    assert result.exit_code == 0
    assert "state: ready" in result.output
    assert "0199-test-task" in result.output
    assert "TESTER" in result.output
    assert "14400" in result.output


def test_status_renders_queue(tmp_path: Path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "preparing",
        "queue": [
            {"lease_id": "uuid-1-pending",
             "task": "0200-a", "holder_role": "TESTER"},
            {"lease_id": "uuid-2-pending",
             "task": "0201-b", "holder_role": "EXPLORER"},
        ],
    }))
    monkeypatch.chdir(tmp_path)

    from click.testing import CliRunner
    result = CliRunner().invoke(stand_mod.stand, ["status"])
    assert result.exit_code == 0
    assert "queue: 2 pending" in result.output
    assert "0200-a" in result.output
    assert "0201-b" in result.output


def test_status_renders_down_reason(tmp_path: Path, monkeypatch) -> None:
    coord = tmp_path / "coordination"
    ss.update_stand_state(coord, lambda s: s.update({
        "state": "down",
        "down_reason": "docker compose build failed: out of disk",
    }))
    monkeypatch.chdir(tmp_path)

    from click.testing import CliRunner
    result = CliRunner().invoke(stand_mod.stand, ["status"])
    assert result.exit_code == 0
    assert "state: down" in result.output
    assert "out of disk" in result.output


def test_status_renders_history_tail(tmp_path: Path, monkeypatch) -> None:
    """History last-5 visible in status output."""
    coord = tmp_path / "coordination"
    def mutator(state):
        state["state"] = "free"
        # Manually append history entries (record_transition cycles
        # the state field, this test wants to pin the history-render
        # path independently).
        state["history"] = [
            {"t": "2026-05-26T10:00:00Z", "from": "free",
             "to": "preparing", "by": "STAND-KEEPER",
             "lease_id": "u1"},
            {"t": "2026-05-26T11:00:00Z", "from": "preparing",
             "to": "ready", "by": "STAND-KEEPER",
             "lease_id": "u1"},
            {"t": "2026-05-26T12:00:00Z", "from": "ready",
             "to": "free", "by": "TESTER",
             "lease_id": "u1"},
        ]
    ss.update_stand_state(coord, mutator)
    monkeypatch.chdir(tmp_path)

    from click.testing import CliRunner
    result = CliRunner().invoke(stand_mod.stand, ["status"])
    assert result.exit_code == 0
    assert "history" in result.output
    assert "free → preparing" in result.output
    assert "preparing → ready" in result.output
    assert "ready → free" in result.output
