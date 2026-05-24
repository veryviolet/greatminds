"""Regression tests for src/greatminds/data/command_START.yaml (task 0066).

The canon bootstrap prompt template is what start_agent.py renders into
every role's first-tick instructions. It carries the inter-tick backoff
contract: claude uses ScheduleWakeup (kernel-level wake, doesn't block
pty), codex/cursor have no equivalent and need a different recipe.

EXPLORER's 0050 W4 finding: a single ``Bash sleep <N>`` issued by a
codex agent as the end-of-tick backoff is uninterruptible — pty input
(coordd's socket nudge, restart's Enter) lands in the codex prompt
buffer but can't break the running sleep tool call. Reaction latency
on codex agents was therefore = N, not the ~seconds the design wanted.

The fix in command_START.yaml replaces ``Bash sleep <N>`` for the
codex/cursor branch with a short-poll loop (5-second iterations of
``greatminds inbox list && greatminds task list <queue>``). These
tests pin the new contract so a future canon edit can't silently
re-introduce the synchronous-sleep pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _command_start_text() -> str:
    """Return the canon command_START.yaml content as a string."""
    from greatminds.core.paths import find_canon_dir
    p = find_canon_dir() / "command_START.yaml"
    assert p.is_file(), f"missing canon file: {p}"
    return p.read_text(encoding="utf-8")


def test_codex_cursor_branch_no_synchronous_sleep_directive():
    """Regression for task 0066: the codex/cursor backoff branch MUST
    NOT instruct the agent to issue a single synchronous ``Bash sleep
    <N>``. That pattern blocks pty input on codex/cursor agents.

    The check looks for the LEGACY INSTRUCTION wording — the form that
    told the agent to issue the sleep — not for any mention of
    "synchronous Bash sleep" (the new wording warns AGAINST it and
    needs to keep that warning).
    """
    text = _command_start_text()
    # The 1.2.x legacy instruction had this exact shape:
    #   'last action of the tick is a **synchronous** `Bash sleep <N>`'
    # If we ever re-introduce a positive directive of this form, fail.
    legacy_instruction = "last action of the tick is a **synchronous**"
    legacy_instruction_alt = "last action of the tick is a synchronous"
    assert legacy_instruction not in text, (
        f"command_START.yaml still contains the legacy synchronous-sleep "
        f"INSTRUCTION wording: {legacy_instruction!r}. Replace with the "
        f"short-poll loop (see task 0066)."
    )
    assert legacy_instruction_alt not in text, (
        f"command_START.yaml still contains a synchronous-sleep INSTRUCTION "
        f"wording: {legacy_instruction_alt!r}. Replace with the short-poll "
        f"loop (see task 0066)."
    )


def test_codex_cursor_branch_carries_short_poll_directive():
    """Pin the new pattern: codex/cursor branch must instruct a
    short-poll loop using greatminds CLI calls + sleep 5 iterations."""
    text = _command_start_text()
    assert "short-poll" in text or "short poll" in text, (
        "command_START.yaml missing the short-poll directive that "
        "replaces synchronous Bash sleep for codex/cursor agents"
    )
    # The poll body must reference the CLI calls that double as
    # heartbeat refreshers (every greatminds CLI touches heartbeat).
    assert "greatminds inbox list" in text, (
        "short-poll directive must include 'greatminds inbox list'"
    )
    assert "greatminds task list" in text, (
        "short-poll directive must include 'greatminds task list <queue>'"
    )
    # Per-iteration sleep should be small (5s in the spec).
    assert "Bash sleep 5" in text or "sleep 5" in text, (
        "short-poll directive should specify a 5s inter-iteration sleep "
        "(matches PLANNER's plan path A)"
    )


def test_short_poll_excludes_empty_sentinel_busy_loop():
    """Regression for REVIEWER's iter-N+1 bounce on 0066: the short-poll
    break-out condition must NOT be phrased as "tick immediately if CLI
    returns non-empty stdout" because ``greatminds inbox list`` prints
    the literal ``(empty)`` sentinel when there is no work — a literal
    reading of "non-empty stdout" would treat that as work and busy-loop
    every 5s, defeating adaptive backoff.

    The canon directive must instead break out only on **actual work
    content** (real task ids, real ask/wake/info kind tags), explicitly
    excluding the ``(empty)`` sentinel.
    """
    text = _command_start_text()
    # The fix wording must mention the (empty) sentinel and tell the
    # agent it is NOT work.
    assert "(empty)" in text, (
        "short-poll directive must explicitly mention the '(empty)' "
        "sentinel printed by 'greatminds inbox list' on an empty mailbox"
    )
    # And the break-out condition must be phrased in terms of actual
    # work content, not 'non-empty stdout'.
    assert "actual work" in text or "real" in text, (
        "short-poll break-out condition must be phrased as actual work "
        "content, not 'non-empty stdout' (REVIEWER iter-N+1 bounce on 0066)"
    )
    # Negative pin: the bare 'non-empty' phrasing without the (empty)
    # exclusion would re-introduce the busy-loop bug.
    naive = "either CLI returns\n    non-empty"
    assert naive not in text, (
        "command_START.yaml still uses the naive 'either CLI returns "
        "non-empty' phrasing that lets agents treat the '(empty)' "
        "sentinel as work and busy-loop. Replace with the actual-work "
        "phrasing (REVIEWER iter-N+1 bounce on 0066)."
    )


def test_claude_branch_still_uses_schedulewakeup():
    """The claude branch is unaffected by the codex/cursor fix —
    claude still uses ScheduleWakeup which doesn't block pty."""
    text = _command_start_text()
    assert "ScheduleWakeup" in text
    assert "delaySeconds" in text
