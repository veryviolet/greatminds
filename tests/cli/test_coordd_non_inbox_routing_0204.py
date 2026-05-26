"""Tests for task 0204: coordd routes non-inbox coordination events
directly to owning roles via schema.queues.

Pre-0204 a file landing in ``feature_inbox/`` (or any non-inbox
queue) was only seen by the owning role indirectly: the journal got
the entry, notify_from_journal wrote a wake-*.md into the inbox,
inotify re-fired on the inbox subdir, and the owner finally got
SIGINT'd. EXPLORER measured 2s+ latency for that path
(review_session 0140).

0204 closes the gap: coordd's inotify dispatcher now reads the
event's watch-descriptor → resolves the queue → looks up the owner
from schema.queues → applies the schema.event_wake mechanism.
notify_from_journal stays as the cross-role messaging path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as coordd_mod
from greatminds.core.paths import find_canon_dir


# ---------- _owning_role_for_queue ----------


def test_owning_role_resolves_developer_for_feature_dev() -> None:
    """schema.queues.feature_dev.owner == DEVELOPER."""
    assert coordd_mod._owning_role_for_queue(
        find_canon_dir(), "feature_dev",
    ) == "DEVELOPER"


def test_owning_role_resolves_planner_for_feature_inbox() -> None:
    assert coordd_mod._owning_role_for_queue(
        find_canon_dir(), "feature_inbox",
    ) == "ARCHITECT-PLANNER"


def test_owning_role_unknown_for_removed_stand_queues() -> None:
    """0247 (1.3.0): stand_requests/wip/done queues REMOVED.
    _owning_role_for_queue returns None for unknown queues; this
    test pins that the removed queues are no longer schema entries."""
    canon = find_canon_dir()
    assert coordd_mod._owning_role_for_queue(
        canon, "stand_requests") is None
    assert coordd_mod._owning_role_for_queue(
        canon, "stand_wip") is None


def test_owning_role_resolves_reviewer_for_feature_review() -> None:
    assert coordd_mod._owning_role_for_queue(
        find_canon_dir(), "feature_review",
    ) == "ARCHITECT-REVIEWER"


def test_owning_role_explorer_for_review_sessions() -> None:
    assert coordd_mod._owning_role_for_queue(
        find_canon_dir(), "review_sessions",
    ) == "EXPLORER"


def test_owning_role_returns_none_for_unknown_queue() -> None:
    assert coordd_mod._owning_role_for_queue(
        find_canon_dir(), "not-a-real-queue",
    ) is None


# ---------- _route_queue_event ----------


def _project(tmp_path: Path, owner_tool: str = "codex",
              window_name: str = "dev") -> Path:
    """Toy project with coord.yaml mapping DEVELOPER to a window."""
    project = tmp_path / "project"
    (project / "coordination").mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": window_name, "role": "DEVELOPER",
             "tool": owner_tool, "mode": "loop"},
            {"name": "planner", "role": "ARCHITECT-PLANNER",
             "tool": "claude", "mode": "chat"},
        ],
    }), encoding="utf-8")
    return project


def test_route_queue_event_invokes_sigint_for_codex_owner(
    tmp_path: Path, monkeypatch,
) -> None:
    """Happy path: file lands in feature_dev/, DEVELOPER's tool is
    codex (mapped to sigint mechanism per schema.event_wake.by_tool).
    Helper invokes sigint_sleeping_descendant for DEVELOPER."""
    project = _project(tmp_path, owner_tool="codex")
    coord = project / "coordination"

    sigint_calls: list = []
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda c, role, verbose: sigint_calls.append(role),
    )

    woke = coordd_mod._route_queue_event(
        coord, find_canon_dir(), "feature_dev",
        "0199-fake-task.yaml", verbose=False,
    )
    assert woke is True
    assert sigint_calls == ["DEVELOPER"]


def test_route_queue_event_invokes_press_enter_for_claude_owner(
    tmp_path: Path, monkeypatch,
) -> None:
    """0259: claude chat-mode role (ARCHITECT-PLANNER) → press_enter
    via input_sock Channel 1 (replaces the 0186 tmux-send-keys
    shortcut). Pin the dispatch."""
    project = _project(tmp_path, owner_tool="codex")
    coord = project / "coordination"

    calls: list[dict] = []
    def fake_press_enter(coord_dir, session, window, role_lower,
                         agent_type, *, mode, verify, **_kw):
        calls.append({
            "role_lower": role_lower, "agent_type": agent_type,
            "mode": mode, "verify": verify,
        })
        return (True, "fake-ok")
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter", fake_press_enter,
    )

    woke = coordd_mod._route_queue_event(
        coord, find_canon_dir(), "feature_inbox",
        "0199-fake-task.yaml", verbose=False,
    )
    assert woke is True
    assert len(calls) == 1
    assert calls[0]["role_lower"] == "architect-planner"
    assert calls[0]["agent_type"] == "claude"
    assert calls[0]["mode"] == "wake"
    assert calls[0]["verify"] is False


def test_route_queue_event_skips_inbox_queue(tmp_path: Path,
                                                monkeypatch) -> None:
    """Inbox events go through the existing per-role inbox-scan path;
    the 0204 helper explicitly bypasses ``queue == 'inbox'``."""
    project = _project(tmp_path)
    coord = project / "coordination"
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail(
            "0204: should NOT route inbox events"
        ),
    )
    assert coordd_mod._route_queue_event(
        coord, find_canon_dir(), "inbox",
        "wake-1234.md", verbose=False,
    ) is False


def test_route_queue_event_skips_template_and_dot_files(
    tmp_path: Path, monkeypatch,
) -> None:
    """Watch noise: ``_TEMPLATE.md``, ``.tmp.*``, dot-files. None of
    them should trigger a wake."""
    project = _project(tmp_path)
    coord = project / "coordination"
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail("0204: should not wake on noise"),
    )
    for fname in ("_TEMPLATE.yaml", ".tmp.abc123",
                  "_TEMPLATE.md", ".id_counter"):
        assert coordd_mod._route_queue_event(
            coord, find_canon_dir(), "feature_dev",
            fname, verbose=False,
        ) is False


def test_route_queue_event_skips_unknown_queue(tmp_path: Path,
                                                  monkeypatch) -> None:
    """Queue not in schema → no owner resolvable → no wake."""
    project = _project(tmp_path)
    coord = project / "coordination"
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail("0204: unknown queue should noop"),
    )
    assert coordd_mod._route_queue_event(
        coord, find_canon_dir(), "not-a-real-queue",
        "0199.yaml", verbose=False,
    ) is False


def test_route_queue_event_suppresses_self_wake(tmp_path: Path,
                                                  monkeypatch) -> None:
    """0152 reuse: if the journal's most recent entry naming the task
    has actor == owner, the owner JUST did the work — skip the wake
    (otherwise the agent gets re-woken after their own action)."""
    project = _project(tmp_path)
    coord = project / "coordination"
    # Synthesize a journal line saying DEVELOPER just landed this task.
    journal = coord / "journal.ndjson"
    journal.write_text(
        '{"t":"2026-05-26T11:00:00Z","actor":"DEVELOPER",'
        '"task":"0199-fake","from":"feature_test","to":"feature_dev"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        coordd_mod, "sigint_sleeping_descendant",
        lambda *a, **kw: pytest.fail("0204: should self-wake-suppress"),
    )

    # feature_dev is owned by DEVELOPER; latest journal actor for
    # 0199-fake is DEVELOPER → suppress.
    woke = coordd_mod._route_queue_event(
        coord, find_canon_dir(), "feature_dev",
        "0199-fake.yaml", verbose=False,
    )
    assert woke is False


# ---------- _InotifyWatcher.queue_for ----------


def test_inotify_watcher_records_wd_to_queue_mapping(
    tmp_path: Path,
) -> None:
    """0204: the watcher tags each watch descriptor with its queue
    name so the main loop can dispatch by queue without re-listing
    inotify state."""
    pytest.importorskip("inotify_simple")
    coord = tmp_path / "coord"
    for q in ("inbox", "feature_dev", "stand_requests"):
        (coord / q).mkdir(parents=True)
    w = coordd_mod._InotifyWatcher(coord, verbose=False)
    # At least one wd points at feature_dev and one at stand_requests.
    queues_seen = set(w._wd_to_queue.values())
    assert "feature_dev" in queues_seen
    assert "stand_requests" in queues_seen


def test_inotify_watcher_queue_for_returns_none_for_unknown_wd(
    tmp_path: Path,
) -> None:
    pytest.importorskip("inotify_simple")
    coord = tmp_path / "coord"
    (coord / "feature_dev").mkdir(parents=True)
    w = coordd_mod._InotifyWatcher(coord, verbose=False)
    assert w.queue_for(99999) is None


# ---------- _last_journal_actor_for ----------


def test_last_journal_actor_returns_latest_for_task(tmp_path: Path) -> None:
    """Helper scans the journal tail and returns the most recent
    entry's actor for the given task. Used by self-wake suppression."""
    coord = tmp_path / "coord"
    coord.mkdir()
    journal = coord / "journal.ndjson"
    journal.write_text(
        '{"t":"2026-05-26T10:00:00Z","actor":"TESTER","task":"0199-fake"}\n'
        '{"t":"2026-05-26T11:00:00Z","actor":"DEVELOPER","task":"0199-fake"}\n',
        encoding="utf-8",
    )
    assert coordd_mod._last_journal_actor_for(
        coord, "0199-fake.yaml") == "DEVELOPER"


def test_last_journal_actor_returns_none_when_no_match(tmp_path: Path) -> None:
    coord = tmp_path / "coord"
    coord.mkdir()
    (coord / "journal.ndjson").write_text(
        '{"t":"2026-05-26T10:00:00Z","actor":"TESTER","task":"other-task"}\n',
        encoding="utf-8",
    )
    assert coordd_mod._last_journal_actor_for(
        coord, "0199-fake.yaml") is None


def test_last_journal_actor_handles_missing_journal(tmp_path: Path) -> None:
    coord = tmp_path / "coord"
    coord.mkdir()
    assert coordd_mod._last_journal_actor_for(
        coord, "0199-fake.yaml") is None
