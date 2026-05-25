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


def test_emit_tmux_sends_wrapper_with_trailing_enter(fake_tmux,
                                                      tmp_path) -> None:
    """0160: ``_emit_tmux`` must send the wrapper followed by Enter so
    the loop starts running and blocks at its first ``read``. Pre-0160
    it sent the start-agent line WITHOUT Enter — operator's first
    Enter then ran the agent, but no looping behavior was set up."""
    cfg = {
        "session": "test",
        "windows": [
            {"name": "dev", "role": "DEVELOPER", "tool": "claude",
             "mode": "loop"},
        ],
    }
    launch_mod._emit_tmux(tmp_path, cfg, _make_env_setup(), recreate=False)

    send_keys = [c for c in fake_tmux if c and c[0] == "send-keys"]
    # Find the wrapper send-keys (the one whose argument contains
    # ``while true``).
    wrapper_calls = [c for c in send_keys
                     if any("while true" in str(arg) for arg in c)]
    assert len(wrapper_calls) == 1, (
        f"expected exactly one wrapper send-keys; got {wrapper_calls}"
    )
    # The wrapper send-keys must end with the Enter keystroke.
    assert wrapper_calls[0][-1] == "Enter", (
        "0160: wrapper send-keys must terminate with Enter so the "
        f"loop starts; got {wrapper_calls[0]}"
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


def test_emit_tmux_no_longer_pre_types_bare_launcher(fake_tmux,
                                                       tmp_path) -> None:
    """Negative pin against the pre-0160 pattern: ``_emit_tmux`` must
    NOT send the raw ``greatminds start-agent X claude`` line as a
    standalone send-keys (without Enter). The 0160 fix replaces that
    pattern with the wrapper-loop send-keys + Enter."""
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
        # Find any arg that's the bare launch command.
        for arg in c:
            if not isinstance(arg, str):
                continue
            # Pre-0160 pattern: arg == launch cmd, no while/done around it.
            if (arg.startswith("greatminds start-agent")
                    and "while true" not in arg):
                pytest.fail(
                    "0160 regression: _emit_tmux sent the bare start-agent "
                    f"command without the wrapper loop. send-keys call: {c}"
                )


def test_emit_tmux_wrapper_includes_window_role(fake_tmux,
                                                  tmp_path) -> None:
    """The wrapper's role-name surfaces in the prompt the operator
    sees. Pin: for two roles in the same session, each pane's wrapper
    must carry its own role string (and ONLY its own role's
    start-agent line)."""
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
    dev_wrapper = next((c for c in send_keys
                        if any("while true" in str(arg) for arg in c)
                        and "DEVELOPER" in " ".join(str(a) for a in c)
                        and "ARCHITECT-REVIEWER" not in " ".join(str(a) for a in c)),
                       None)
    rev_wrapper = next((c for c in send_keys
                        if any("while true" in str(arg) for arg in c)
                        and "ARCHITECT-REVIEWER" in " ".join(str(a) for a in c)),
                       None)
    assert dev_wrapper is not None, "missing DEVELOPER wrapper"
    assert rev_wrapper is not None, "missing ARCHITECT-REVIEWER wrapper"
