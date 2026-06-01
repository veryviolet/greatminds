"""Tests for task 0160: ``greatminds launch`` installs a wrapper loop.

Pre-0160 ``_emit_tmux`` pre-typed ``greatminds start-agent ...`` into
each tmux pane with NO trailing Enter. Operator's first Enter ran the
agent; on agent exit, the pane reverted to a bare bash prompt with no
command to re-execute. ``greatminds restart`` then sent a bare Enter
into that empty bash — no-op — and the registry stayed empty.

0160 replaces the pre-type-no-Enter pattern with a bash wrapper-loop:
the pane runs ``while true; do read; start-agent...; done``, so each
Enter (manual or from ``restart``) triggers the next agent launch.
"""
from __future__ import annotations

import pytest

from greatminds.cli import launch as launch_mod


# ---------- the wrapper-loop string itself ----------


def test_wrapper_loop_is_a_loop(tmp_path) -> None:
    """0160 contract: wrapper carries ``while true`` / ``done`` so each
    Enter triggers another agent launch. Pre-0160 the pane had a
    one-shot pre-typed command that bash ran exactly once."""
    wrapper = launch_mod._wrapper_loop(
        "greatminds start-agent DEVELOPER claude --mode loop",
        "DEVELOPER",
    )
    assert "while true" in wrapper
    assert "done" in wrapper


def test_wrapper_loop_reads_from_tty(tmp_path) -> None:
    """The wrapper's ``read`` MUST consume from ``/dev/tty``, not from
    stdin. ``tmux send-keys`` injects characters into the pane's
    terminal device; reading from default stdin (which may be the
    inherited pipe of the wrapper's parent) would miss those bytes.
    """
    wrapper = launch_mod._wrapper_loop(
        "greatminds start-agent X claude --mode loop", "X",
    )
    assert "read -r _ </dev/tty" in wrapper, (
        "0160: wrapper must read from /dev/tty so tmux send-keys input "
        "lands on the wrapper's read, not on some inherited pipe"
    )


def test_wrapper_loop_contains_role_in_prompt(tmp_path) -> None:
    """The wrapper's ``printf 'press Enter to (re)start <ROLE>...'``
    surfaces which pane is waiting. Operator scanning many tmux
    windows needs to know which agent crashed."""
    wrapper = launch_mod._wrapper_loop(
        "greatminds start-agent ARCHITECT-REVIEWER codex --mode loop",
        "ARCHITECT-REVIEWER",
    )
    assert "ARCHITECT-REVIEWER" in wrapper


def test_wrapper_loop_contains_launch_command_verbatim(tmp_path) -> None:
    """The wrapper re-runs the exact ``greatminds start-agent ...``
    invocation that ``_launch_command`` composes. No paraphrasing —
    a typo here would silently swap the mode or skip the tool."""
    launch_cmd = "greatminds start-agent DEVELOPER claude --mode loop"
    wrapper = launch_mod._wrapper_loop(launch_cmd, "DEVELOPER")
    assert launch_cmd in wrapper


def test_wrapper_loop_is_single_line(tmp_path) -> None:
    """``tmux send-keys`` delivers the string as one keystroke
    sequence. Newlines would be interpreted as separate ``send-keys``
    inputs and could land between bash commands at the wrong time.
    The wrapper is a single ``while ... ; done`` line.
    """
    wrapper = launch_mod._wrapper_loop(
        "greatminds start-agent X claude --mode loop", "X",
    )
    assert "\n" not in wrapper, (
        "0160: wrapper must be a single line — tmux send-keys interprets "
        f"embedded newlines as separate keystrokes. Got: {wrapper!r}"
    )


# ---------- emit_tmux end-to-end (mocked tmux) ----------


@pytest.fixture
def fake_tmux(monkeypatch):
    """Record every ``_tmux`` call and return tailored exit codes.

    ``has-session`` returns rc=1 (session missing) so ``_emit_tmux``
    proceeds with creation. Everything else returns rc=0.
    """
    import subprocess

    calls: list[list[str]] = []

    def fake(*args):
        calls.append(list(args))
        # has-session: rc=1 → not found → emit proceeds to create.
        if args and args[0] == "has-session":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "")
        return subprocess.CompletedProcess(["tmux", *args], 0, "", "")

    monkeypatch.setattr(launch_mod, "_tmux", fake)
    # Skip the ``which tmux`` precondition in _emit_tmux.
    monkeypatch.setattr(launch_mod.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(
                            ["which", "tmux"], 0, "/usr/bin/tmux\n", ""))
    return calls


def _make_env_setup():
    """Build a minimal ``EnvSetup`` for ``_emit_tmux``."""
    from greatminds.core.env import EnvSetup
    return EnvSetup(env_type=None, activation="", source="test-stub")


def test_emit_tmux_sends_launch_command_with_trailing_enter(
    fake_tmux, tmp_path,
) -> None:
    """0308 rewrite of the 0160 test: ``_emit_tmux`` now sends the
    bare ``greatminds start-agent <ROLE> <tool> --mode loop``
    command directly (NOT a wrapper-loop), terminated with Enter.
    Pre-0308 a bash wrapper-loop was installed; 0308 removed it."""
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _make_env_setup(), recreate=False)

    send_keys = [c for c in fake_tmux if c and c[0] == "send-keys"]
    launch_calls = [c for c in send_keys
                    if any("greatminds start-agent" in str(arg)
                           for arg in c)]
    assert len(launch_calls) == 1, (
        f"0308: expected exactly one launch-command send-keys; "
        f"got {launch_calls}"
    )
    cmd_call = launch_calls[0]
    # End with Enter so the launch command actually runs.
    assert cmd_call[-1] == "Enter"
    # 0308: no wrapper — the command must be the bare start-agent
    # invocation, NOT wrapped in ``while true; do …``.
    for arg in cmd_call:
        if isinstance(arg, str):
            assert "while true" not in arg, (
                "0308: launch send-keys must NOT carry the legacy "
                f"wrapper loop. Got: {arg}"
            )
    # And the C-u prefix must have fired before the launch command,
    # to clear any leftover bash input.
    cu_idx = next(
        (i for i, c in enumerate(send_keys) if "C-u" in c),
        None,
    )
    launch_idx = send_keys.index(cmd_call)
    assert cu_idx is not None and cu_idx < launch_idx, (
        f"0308: ``C-u`` clear must fire BEFORE the launch command. "
        f"cu_idx={cu_idx}, launch_idx={launch_idx}"
    )


def test_emit_tmux_skips_wrapper_for_bash_window(fake_tmux,
                                                  tmp_path) -> None:
    """Bash/no-role windows just open a project shell — no agent, no
    wrapper. The wrapper-install logic must be scoped to role
    windows; otherwise generic dev shells would also get the loop."""
    cfg = {
        "session": "test",
        "windows": [
            {"name": "ops", "role": "", "tool": "bash"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _make_env_setup(), recreate=False)

    send_keys = [c for c in fake_tmux if c and c[0] == "send-keys"]
    assert not any("while true" in str(arg)
                   for c in send_keys for arg in c), (
        "0160: bash window must NOT receive a wrapper loop"
    )


def test_emit_tmux_launch_command_terminates_with_enter(
    fake_tmux, tmp_path,
) -> None:
    """0308 rewrite of the pre-0160 negative pin. The original pin
    against the pre-0160 bug (bare command sent WITHOUT Enter) is
    inverted by 0308: the launch command MUST land with Enter,
    NOT wrapped in ``while true; do …``. Verify the trailing
    keystroke is Enter for every launch-command send-keys call."""
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _make_env_setup(), recreate=False)

    send_keys = [c for c in fake_tmux if c and c[0] == "send-keys"]
    for c in send_keys:
        for arg in c:
            if not isinstance(arg, str):
                continue
            if arg.startswith("greatminds start-agent"):
                # 0308: must NOT be wrapped in while-true.
                assert "while true" not in arg, (
                    f"0308 regression: launch send-keys carries the "
                    f"legacy wrapper loop. Got: {arg}"
                )
                # Must end with Enter so the command runs.
                assert c[-1] == "Enter", (
                    f"0308: launch command send-keys must terminate "
                    f"with Enter. Got: {c}"
                )


def test_emit_tmux_launch_command_per_role(fake_tmux,
                                            tmp_path) -> None:
    """0308 rewrite: each role's pane receives its own
    ``greatminds start-agent <ROLE>`` send-keys call (no wrapper).
    Pin: two roles in the same session → two launch-command sends,
    each naming the correct role and ONLY that role."""
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
            {"name": "rev", "role": "ARCHITECT-REVIEWER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _make_env_setup(), recreate=False)

    send_keys = [c for c in fake_tmux if c and c[0] == "send-keys"]
    dev_cmd = next(
        (c for c in send_keys
         if any("greatminds start-agent DEVELOPER" in str(arg)
                for arg in c)),
        None,
    )
    rev_cmd = next(
        (c for c in send_keys
         if any("greatminds start-agent ARCHITECT-REVIEWER" in str(arg)
                for arg in c)),
        None,
    )
    assert dev_cmd is not None, (
        "0308: missing DEVELOPER launch send-keys"
    )
    assert rev_cmd is not None, (
        "0308: missing ARCHITECT-REVIEWER launch send-keys"
    )
    # No cross-contamination: each command names ONLY its own role.
    assert all("ARCHITECT-REVIEWER" not in str(a) for a in dev_cmd
                if isinstance(a, str)
                and "greatminds start-agent" in a)
    assert all("DEVELOPER " not in str(a) for a in rev_cmd
                if isinstance(a, str)
                and "greatminds start-agent" in a)
