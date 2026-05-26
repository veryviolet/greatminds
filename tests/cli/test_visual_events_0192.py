"""Tests for task 0192: schema-driven colored one-liners on
task-mv and inbox-send.

Plan-listed events:
- claimed:     from feature_inbox/plan/blocked → implementer queue
- finished:    implementer queue → feature_test/feature_docs_review
- accepted:    any → verified
- rejected:    review/test/reader-review → implementer queue (handback)
- message_sent: ``greatminds inbox send``

Each carries a schema-resolved color name + template. Operator-visible
output to STDERR only; STDOUT untouched. ``GREATMINDS_VISUAL_OFF=1``
suppresses emission.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from greatminds.cli import _colors as colors_mod
from greatminds.cli import task as task_mod


# ---------- schema pin ----------


def test_schema_has_visual_events_section() -> None:
    """0192 schema pin: each of the 5 plan events has color + template."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    events = doc.get("visual_events")
    assert events is not None, "0192: schema.yaml missing visual_events:"
    for name in ("claimed", "finished", "accepted", "rejected",
                 "message_sent"):
        assert name in events, f"0192: visual_events.{name} missing"
        assert "color" in events[name]
        assert "template" in events[name]


def test_schema_color_names_resolve_via_colors_module() -> None:
    """Every schema-named color must be mapped in _colors._VISUAL_COLOR_MAP
    (otherwise visual() silently falls back to cyan)."""
    from greatminds.core.paths import find_canon_dir
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    used = {
        entry["color"]
        for entry in doc["visual_events"].values()
    }
    for name in used:
        assert name in colors_mod._VISUAL_COLOR_MAP, (
            f"0192: visual_events uses unmapped color {name!r}; add it "
            f"to cli/_colors.py:_VISUAL_COLOR_MAP"
        )


# ---------- _emit_visual_event (direct) ----------


def test_emit_visual_event_writes_template_to_stderr(
    capsys, monkeypatch,
) -> None:
    """Happy path: known event + correct context keys → templated line
    on stderr."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    task_mod._emit_visual_event(
        "claimed", role="DEVELOPER", task_id="0199-foo",
        from_queue="feature_plan", to_queue="feature_dev",
    )
    cap = capsys.readouterr()
    assert "[DEVELOPER] CLAIMED 0199-foo (feature_plan → feature_dev)" \
        in cap.err
    assert cap.out == ""


def test_emit_visual_event_silent_when_off_env_set(
    capsys, monkeypatch,
) -> None:
    """0192 opt-out: GREATMINDS_VISUAL_OFF=1 → zero emission."""
    monkeypatch.setenv("GREATMINDS_VISUAL_OFF", "1")
    task_mod._emit_visual_event(
        "claimed", role="DEVELOPER", task_id="0199-foo",
        from_queue="feature_plan", to_queue="feature_dev",
    )
    cap = capsys.readouterr()
    assert cap.err == ""
    assert cap.out == ""


def test_emit_visual_event_unknown_event_silent(
    capsys, monkeypatch,
) -> None:
    """Forward-compat: unknown event name (e.g. a future schema with a
    new event that this version doesn't render) → silent no-op."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    task_mod._emit_visual_event(
        "not_a_real_event", role="DEVELOPER", task_id="0199",
    )
    cap = capsys.readouterr()
    assert cap.err == ""


def test_emit_visual_event_missing_context_key_silent(
    capsys, monkeypatch,
) -> None:
    """Defensive: if the call site forgets a {placeholder}, swallow the
    KeyError silently — visual layer must never block the action."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    task_mod._emit_visual_event(
        "claimed", role="DEVELOPER",  # missing task_id/from_queue/to_queue
    )
    cap = capsys.readouterr()
    assert cap.err == ""


# ---------- _emit_visual_for_mv dispatcher ----------


def test_mv_dispatcher_claimed_from_plan_to_dev(capsys, monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    task_mod._emit_visual_for_mv(
        "0199-foo", "feature_plan", "feature_dev", reason="",
    )
    cap = capsys.readouterr()
    assert "CLAIMED" in cap.err
    assert "0199-foo" in cap.err


def test_mv_dispatcher_finished_dev_to_test(capsys, monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    task_mod._emit_visual_for_mv(
        "0199-foo", "feature_dev", "feature_test", reason="",
    )
    cap = capsys.readouterr()
    assert "FINISHED" in cap.err
    assert "feature_test" in cap.err


def test_mv_dispatcher_accepted_to_verified(capsys, monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    task_mod._emit_visual_for_mv(
        "0199-foo", "feature_review", "verified", reason="",
    )
    cap = capsys.readouterr()
    assert "ACCEPTED" in cap.err


def test_mv_dispatcher_rejected_review_to_dev(capsys, monkeypatch,
                                                tmp_path) -> None:
    """Handback: from feature_review → feature_dev → rejected event."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "ARCHITECT-REVIEWER")
    # Stub find_task / load_task to avoid disk IO for this branch test.
    monkeypatch.setattr(task_mod, "find_task",
                        lambda c, t: None)
    task_mod._emit_visual_for_mv(
        "0199-foo", "feature_review", "feature_dev", reason="changes_requested",
    )
    cap = capsys.readouterr()
    assert "REJECTED" in cap.err
    assert "changes_requested" in cap.err


def test_mv_dispatcher_no_event_for_neutral_mv(capsys, monkeypatch) -> None:
    """A mv that doesn't match any pattern (e.g. feature_dev →
    feature_blocked) emits nothing. Operators care about milestones,
    not every transition."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    task_mod._emit_visual_for_mv(
        "0199-foo", "feature_dev", "feature_blocked", reason="",
    )
    cap = capsys.readouterr()
    assert cap.err == ""


# ---------- _colors.visual ----------


def test_visual_writes_to_stderr_not_stdout(capsys, monkeypatch) -> None:
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    colors_mod.visual("hello", "violet")
    cap = capsys.readouterr()
    assert cap.err.strip().endswith("hello")
    assert cap.out == ""


def test_visual_off_silences_emission(capsys, monkeypatch) -> None:
    monkeypatch.setenv("GREATMINDS_VISUAL_OFF", "1")
    colors_mod.visual("hello", "violet")
    cap = capsys.readouterr()
    assert cap.err == ""


def test_visual_unknown_color_still_emits(capsys, monkeypatch) -> None:
    """Future-color names that aren't yet mapped fall back to cyan and
    still print — operator sees the line."""
    monkeypatch.delenv("GREATMINDS_VISUAL_OFF", raising=False)
    colors_mod.visual("hello", "color-not-yet-mapped")
    cap = capsys.readouterr()
    assert "hello" in cap.err
