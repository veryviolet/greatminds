"""Regression tests for src/greatminds/data/command_START.yaml.

Originally (task 0066) this file pinned a short-poll directive: codex
and cursor agents looped ``greatminds inbox list && sleep 5`` because
a single synchronous ``Bash sleep`` couldn't be interrupted by pty
input.

Post-0150 coordd ships event-driven SIGINT-routing — it walks
``/proc/<agent-pid>/task/*/children`` to find the agent's ``sleep``
descendant and SIGINTs it on any new event in the role's inbox/queue.
The bash sleep then exits, the agent's next tick fires, sub-second
latency.

Task 0176 retires the short-poll directive: agents now sleep 7200s
between ticks (safety upper bound only — coordd is the real wake).
The pre-0176 60s/120s/300s/600s adaptive-backoff table burned API
tokens on idle ticks for no reason and is removed.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _command_start_text() -> str:
    from greatminds.core.paths import find_canon_dir
    p = find_canon_dir() / "command_START.yaml"
    assert p.is_file(), f"missing canon file: {p}"
    return p.read_text(encoding="utf-8")


# ---------- 0176: long sleep directive ----------


def test_codex_cursor_branch_uses_long_sleep_post_0176():
    """0176 contract: the codex/cursor backoff branch instructs
    ``Bash sleep 7200`` as the inter-tick wait, with coordd's
    SIGINT-routing as the real wake signal. Pre-0176 they short-polled
    every 5s burning tokens on idle ticks."""
    text = _command_start_text()
    assert "sleep 7200" in text, (
        "0176: codex/cursor branch must instruct 'Bash sleep 7200' "
        "as the inter-tick wait (was: short-poll loop pre-0176)"
    )


def test_canon_explains_coordd_sigint_is_the_real_wake():
    """0176: the canon must explain WHY 7200s is safe — coordd's
    SIGINT-routing wakes the agent sub-second. Without the
    explanation, future maintainers might re-introduce short-poll
    'to keep the agent responsive'."""
    text = _command_start_text()
    assert "SIGINT" in text, (
        "0176: canon must mention SIGINT to explain the wake mechanism"
    )
    assert "coordd" in text, (
        "0176: canon must reference coordd as the wake source"
    )


def test_canon_no_short_poll_directive_post_0176():
    """0176 negative pin: short-poll wording must be GONE from the
    backoff directive. The pre-0176 mechanism is obsolete."""
    text = _command_start_text()
    # Allow historical mentions only in past-tense / explanatory
    # context (e.g. "Pre-0150 agents short-polled..."). Disallow
    # active-voice directives.
    bad_directive_signals = [
        "instead **short-poll**",
        "use **short-poll**",
        "**short-poll**:",
    ]
    for sig in bad_directive_signals:
        assert sig not in text, (
            f"0176: short-poll directive remains in canon: {sig!r}"
        )


def test_canon_no_adaptive_backoff_table_post_0176():
    """0176 negative pin: the 60s/120s/300s/600s adaptive-backoff
    table must be removed. Pre-0176 it burned tokens on idle agents
    for no reason once coordd's event-driven SIGINT shipped (0150)."""
    text = _command_start_text()
    # The exact pre-0176 table pattern.
    forbidden = [
        "0 idle in a row (just had real work) → next sleep **60s**",
        "1–2 idle → **120s**",
        "3–5 idle → **300s**",
        "6+ idle → **600s**",
    ]
    for line in forbidden:
        assert line not in text, (
            f"0176: adaptive-backoff table line still present: {line!r}"
        )


def test_canon_keeps_loop_invariant_unchanged():
    """0176 must NOT touch the 'infinite loop / never end your own
    session' invariant — that's separate from backoff length."""
    text = _command_start_text()
    assert "infinite loop" in text or "while true" in text
    assert "Never end your own" in text


# ---------- 0178 supersedes 0176 iter-2: timeout vs event-wake re-read ----------


def test_canon_no_read_once_per_session_directive():
    """Negative pin: pre-0176 canon told agents to read static docs
    ``ONCE per session, not every tick`` and cache them in memory.
    0176 banned that disaster; 0178 keeps the ban."""
    text = _command_start_text()
    forbidden = [
        "ONCE per session",
        "once per session",
        "not every tick",
    ]
    for sig in forbidden:
        assert sig not in text, (
            f"pre-0176 read-once directive remains: {sig!r}"
        )


def test_canon_distinguishes_timeout_vs_event_wake_ticks():
    """0178 contract: canon must name the two tick types explicitly.
    Iter-2 said 'every tick re-read' which burns tokens on every
    event-wake; iter-3 (this task) splits timeout (re-read) from
    event-wake (no re-read)."""
    text = _command_start_text()
    assert "Timeout tick" in text, (
        "0178: canon must name 'Timeout tick' as the re-read trigger"
    )
    assert "Event-wake tick" in text, (
        "0178: canon must name 'Event-wake tick' as the no-re-read case"
    )


def test_canon_event_wake_does_not_re_read():
    """0178 positive pin: explicit 'DO NOT re-read static docs' clause
    in the event-wake branch. Without this, a careful reader might
    still interpret the canon as 'always re-read'."""
    text = _command_start_text()
    assert "DO NOT re-read" in text, (
        "0178: canon must instruct the event-wake branch NOT to "
        "re-read static docs"
    )


def test_canon_timeout_tick_lists_four_static_files():
    """0178: when the timeout-tick branch fires, all four static
    docs must be re-read. Pin each filename explicitly."""
    text = _command_start_text()
    for needle in (
        "COORDINATE.md",
        "schema.yaml",
        "PROJECT.md",
        "ROLE.md",
    ):
        assert needle in text, (
            f"0178: timeout-tick re-read must name {needle}"
        )


def test_canon_no_ambiguous_every_tick_wording():
    """0178 negative pin: the iter-2 ambiguous phrasing 'EVERY tick
    re-read' / 'by timeout OR by coordd' must be gone. They conflated
    the two tick types which is precisely what 0178 fixes."""
    text = _command_start_text()
    forbidden = [
        "Re-read static docs at the start of EVERY tick",
        "by timeout OR by coordd",
    ]
    for sig in forbidden:
        assert sig not in text, (
            f"0178: iter-2 ambiguous phrasing remains: {sig!r}"
        )


def test_canon_no_in_memory_caching_directive():
    """Negative pin: pre-0176 in-memory caching directive must stay
    banned. (The 0178 split — re-read on timeout, NOT on event-wake —
    is NOT in-memory caching: each timeout tick re-reads fresh.)"""
    text = _command_start_text()
    forbidden = [
        "keep them in memory",
        "cache in memory",
    ]
    for sig in forbidden:
        assert sig not in text, (
            f"pre-0176 in-memory caching directive remains: {sig!r}"
        )


def test_canon_explains_how_to_distinguish_tick_types():
    """0178: the canon must tell the agent HOW to know which tick type
    fired. The mechanism is the `bash sleep`'s SIGINT vs clean exit:
    SIGINT'd → event-wake; cleanly expired → timeout. Without this
    operational detail, the agent can't apply the rule."""
    text = _command_start_text()
    assert "SIGINT" in text
    # The wrapper-sleep mechanism explanation must be present.
    assert "bash sleep" in text.lower() or "Bash sleep" in text


# ---------- claude branch ----------


def test_claude_branch_still_uses_schedulewakeup():
    """The claude branch is unaffected mechanically — claude has no
    bash wrapper for coordd to SIGINT, so it still uses
    ScheduleWakeup. Post-0176 the recommended delaySeconds is 7200
    (matches codex/cursor) rather than the per-tier 60/120/300/600
    table."""
    text = _command_start_text()
    assert "ScheduleWakeup" in text
    assert "delaySeconds" in text
