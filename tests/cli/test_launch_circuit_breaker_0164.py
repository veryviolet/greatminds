"""Tests for task 0164 Layer C: launch wrapper-loop circuit breaker.

A chronically broken agent (missing codex binary, stale session UUID
before the 0164 discover_codex_session fix landed, etc.) spammed the
pane forever under the 0160 wrapper-loop. 0164 adds a circuit breaker:
the loop exits after ``CIRCUIT_BREAKER_FAILS`` consecutive non-zero
exits within ``CIRCUIT_BREAKER_WINDOW_SEC`` and prints a recovery hint
naming ``GREATMINDS_FRESH=1 greatminds start-agent ...``.
"""
from __future__ import annotations

import pytest

from greatminds.cli import launch as launch_mod


def _wrapper_for_dev() -> str:
    return launch_mod._wrapper_loop(
        "greatminds start-agent DEVELOPER claude --mode loop",
        "DEVELOPER",
    )


# ---------- structural pins on the bash one-liner ----------


def test_wrapper_initializes_fail_counter(tmp_path) -> None:
    """0164 contract: wrapper initializes the ``fails=0`` counter
    BEFORE entering the loop. Without this, the first non-zero exit
    would hit ``fails=$((fails + 1))`` on an unset variable (bash sees
    empty + 1 = 1, but readers can't tell whether the counter was
    intentional or accidental)."""
    wrapper = _wrapper_for_dev()
    assert "fails=0" in wrapper


def test_wrapper_initializes_window_timestamp(tmp_path) -> None:
    """The window-start timestamp must be initialized so the first
    fail compares against a known value (0 → ``now - 0`` is large →
    the window resets and ``fails`` starts at 1)."""
    wrapper = _wrapper_for_dev()
    assert "last_fail_window_start=0" in wrapper


def test_wrapper_captures_exit_code(tmp_path) -> None:
    """``rc=$?`` after the launch_cmd. Without capturing $? the
    breaker can't tell success from failure."""
    wrapper = _wrapper_for_dev()
    assert "rc=$?" in wrapper


def test_wrapper_resets_counter_on_window_expiry(tmp_path) -> None:
    """Outside the rolling window, the counter resets so a slow drip
    of failures (e.g. one a minute) doesn't accumulate to a false
    trip. The breaker is for RAPID failures only."""
    wrapper = _wrapper_for_dev()
    assert "last_fail_window_start=$now" in wrapper
    # Comparison: now - last_fail_window_start > WINDOW_SEC
    assert f"gt {launch_mod.CIRCUIT_BREAKER_WINDOW_SEC}" in wrapper


def test_wrapper_resets_counter_on_success(tmp_path) -> None:
    """After a successful agent run, the counter resets so a future
    sporadic failure doesn't carry over from an earlier hiccup."""
    wrapper = _wrapper_for_dev()
    # The 'else' branch after rc != 0 must reset fails.
    assert "else" in wrapper
    # Two ``fails=0`` occurrences expected: the initializer + the
    # success-branch reset (or the window-expiry reset). At minimum
    # the success-branch reset must be present.
    assert wrapper.count("fails=0") >= 2


def test_wrapper_increments_fail_counter(tmp_path) -> None:
    """``fails=$((fails + 1))`` on each non-zero exit."""
    wrapper = _wrapper_for_dev()
    assert "fails=$((fails + 1))" in wrapper


def test_wrapper_exits_after_threshold(tmp_path) -> None:
    """``exit 1`` once fails reaches CIRCUIT_BREAKER_FAILS."""
    wrapper = _wrapper_for_dev()
    assert f"ge {launch_mod.CIRCUIT_BREAKER_FAILS}" in wrapper
    assert "exit 1" in wrapper


def test_wrapper_prints_recovery_hint(tmp_path) -> None:
    """On trip, the wrapper prints a hint naming ``GREATMINDS_FRESH=1
    greatminds start-agent <ROLE>``. Without this, the operator
    inherits a dead pane with no idea what to do next."""
    wrapper = _wrapper_for_dev()
    assert "GREATMINDS_FRESH=1" in wrapper
    assert "DEVELOPER" in wrapper
    assert "STOPPING wrapper" in wrapper


def test_wrapper_is_still_single_line(tmp_path) -> None:
    """0160 invariant preserved: the breaker logic stays on the same
    line as the rest of the wrapper (tmux send-keys requirement —
    newlines would split it across keystrokes and bash would
    misinterpret)."""
    wrapper = _wrapper_for_dev()
    assert "\n" not in wrapper, (
        f"0164 must NOT introduce newlines into the wrapper. Got: "
        f"{wrapper!r}"
    )


def test_circuit_breaker_constants_have_sensible_defaults() -> None:
    """3 fails in 30s is the breaker default. Pin so a future refactor
    can't silently make the loop too permissive (operator pain) or
    too strict (false trips on a single network hiccup)."""
    assert launch_mod.CIRCUIT_BREAKER_FAILS == 3
    assert launch_mod.CIRCUIT_BREAKER_WINDOW_SEC == 30
