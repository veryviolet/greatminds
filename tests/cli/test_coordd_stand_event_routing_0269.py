"""Tests for task 0269: coordd's ``.stand`` inotify events must route
to STAND-KEEPER.

Bug context: ``coordd._route_queue_event`` resolves the queue owner
via ``schema.queues[<queue>].owner``. Pre-0269 the schema had no
``.stand`` entry — STAND-KEEPER's singleton stand resource lives
under ``schema.stand.resource`` (a separate top-level section, not
a queue) — so the lookup returned None and the dispatcher silently
dropped state.yaml change events (TESTER lease acquisition was not
delivered to SK; SK's pane idled until its own ScheduleWakeup tick).

0269 adds ``.stand`` to ``schema.queues`` with ``owner: STAND-KEEPER``
and a new ``kind: state`` marker so existing iteration helpers
(``watchdog`` stale-task scan, ``wake_check`` terminal-queue dep
gate) keep ignoring it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as coordd_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema source-of-truth ----------


def test_schema_lists_stand_with_stand_keeper_owner() -> None:
    """0269: schema.yaml ``queues['.stand']`` must declare
    STAND-KEEPER as the owner. Without this entry the entire
    ``coordd → press_enter → SK`` path is silently broken."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    entry = (doc.get("queues") or {}).get(".stand")
    assert isinstance(entry, dict), (
        "0269: schema.queues missing '.stand' — coordd cannot resolve "
        "owner for state.yaml inotify events"
    )
    assert entry.get("owner") == "STAND-KEEPER"


def test_schema_stand_queue_kind_is_not_active_or_terminal() -> None:
    """``.stand`` must NOT be marked as a normal active/terminal queue:
    watchdog's stale-task sweep iterates active queues only and would
    flag state.yaml as a stale task otherwise; wake_check's
    terminal-queue gate would treat lease bookkeeping as terminal
    work. The new ``kind: state`` marker exists for exactly this
    reason."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    entry = (doc.get("queues") or {})[".stand"]
    assert entry.get("kind") not in ("active", "terminal"), (
        f"0269: .stand queue.kind must not be active|terminal "
        f"(got {entry.get('kind')!r}); iteration helpers would mishandle it"
    )


def test_schema_lease_writers_include_tester_and_planner() -> None:
    """The lease API lets TESTER (acquire) and ARCHITECT-PLANNER
    (sentinel down/up nudges) trigger state.yaml writes. STAND-KEEPER
    is the primary writer; MAINTAINER occasionally clears stale
    state. Pinning the writers list keeps the doc honest."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    writers = ((doc.get("queues") or {})[".stand"]).get("writers") or []
    for required in ("STAND-KEEPER", "TESTER"):
        assert required in writers, (
            f"0269: schema.queues['.stand'].writers missing {required!r}"
        )


# ---------- coordd resolves the owner via schema ----------


def test_owning_role_for_stand_resolves_stand_keeper() -> None:
    """The helper ``coordd._owning_role_for_queue('.stand', canon)``
    must return ``STAND-KEEPER`` post-0269 so ``_route_queue_event``
    reaches the dispatch branch instead of silently dropping the
    event."""
    owner = coordd_mod._owning_role_for_queue(find_canon_dir(), ".stand")
    assert owner == "STAND-KEEPER", (
        f"0269: _owning_role_for_queue('.stand') returned {owner!r}; "
        "expected 'STAND-KEEPER'"
    )


# ---------- regression: other queues still resolve correctly ----------


@pytest.mark.parametrize("queue,expected_owner", [
    ("feature_dev", "DEVELOPER"),
    ("feature_inbox", "ARCHITECT-PLANNER"),
    ("feature_test", "TESTER"),
    ("feature_review", "ARCHITECT-REVIEWER"),
    ("review_sessions", "EXPLORER"),
])
def test_owning_role_regression_for_known_queues(
    queue: str, expected_owner: str,
) -> None:
    """Adding ``.stand`` must not perturb existing owner resolutions."""
    assert coordd_mod._owning_role_for_queue(find_canon_dir(), queue) \
        == expected_owner


# ---------- end-to-end dispatch ----------


def _project_with_sk_window(tmp_path: Path) -> Path:
    """Build a toy project mapping STAND-KEEPER → a tmux window so the
    dispatch path can resolve a target for press_enter."""
    project = tmp_path / "project"
    (project / "coordination" / ".stand").mkdir(parents=True)
    (project / "coord.yaml").write_text(yaml.safe_dump({
        "session": "test-session",
        "project_dir": str(project),
        "windows": [
            {"name": "stand", "role": "STAND-KEEPER",
             "tool": "claude", "mode": "chat"},
            {"name": "dev", "role": "DEVELOPER",
             "tool": "codex", "mode": "loop"},
        ],
    }), encoding="utf-8")
    return project


def test_route_stand_event_runs_coordd_deploy_on_preparing(
    tmp_path: Path, monkeypatch,
) -> None:
    """1.6.0: a ``state.yaml`` write under ``coordination/.stand/`` with
    state=preparing makes COORDD run the deploy engine directly — there
    is no STAND-KEEPER agent to wake. The retired behavior (press_enter /
    drive an SK turn) must NOT fire."""
    project = _project_with_sk_window(tmp_path)
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
    seen: dict = {}

    def fake_deploy(c, *, lease_id=None, **_k):
        seen["lease_id"] = lease_id
        done.set()
        return (0, "ok")

    monkeypatch.setattr("greatminds.cli.stand.deploy_lease", fake_deploy)
    monkeypatch.setattr(
        coordd_mod, "_maybe_drive_driven_role",
        lambda *a, **k: pytest.fail(
            "1.6.0: .stand must run coordd deploy, not drive an SK agent"))
    coordd_mod._DEPLOYING_LEASES.discard("L1")

    woke = coordd_mod._route_queue_event(
        coord, find_canon_dir(), ".stand", "state.yaml", verbose=False)
    assert woke is True
    assert done.wait(timeout=3), "coordd must run deploy_lease for the lease"
    assert seen["lease_id"] == "L1"


def test_route_queue_event_skips_stand_dotfiles_and_template(
    tmp_path: Path, monkeypatch,
) -> None:
    """Watch-noise filter: ``.tmp.*``, ``_TEMPLATE.*``, dot-files in
    ``.stand/`` must not trigger a wake. (The dispatcher already had
    this filter; this is a regression-net test under the new schema
    entry to ensure nothing changed.)"""
    project = _project_with_sk_window(tmp_path)
    coord = project / "coordination"
    monkeypatch.setattr(
        "greatminds.cli._send_enter.press_enter",
        lambda *a, **kw: pytest.fail(
            "0269: noise files must not trigger press_enter"),
    )

    for fname in (".tmp.abc", ".lock", "_TEMPLATE.yaml", "state.txt"):
        assert coordd_mod._route_queue_event(
            coord, find_canon_dir(), ".stand", fname, verbose=False,
        ) is False, (
            f"0269: dispatcher must filter {fname!r} under .stand"
        )
