"""Single primitive for "wake an agent in a tmux pane" (task 0051).

Iter-2 redesign after TESTER's 4-cell avatar matrix (see 0051 review
block, cells 1-4). The iter-1 design used pane-bytes-diff as the
success signal, which produced both false negatives (cell 1: byte
delivered into idle claude prompt, no pane echo, so diff says
"unchanged") and false positives (cell 4: pane buffer scrolled because
the byte arrived but the codex process was SIGSTOPped and couldn't
process it). The right success signal is the **role's heartbeat
mtime**: that's the only place in the codebase that means "agent
actually acted on this tick". Pane-diff is now a fallback used only
when heartbeat freshness can't be observed.

Two modes (per TESTER recommendation ii):

  - ``mode="wake"`` (default) — for waking an idle-but-finished agent.
    Mirrors what ``coordd.push_to_role`` does: write WAKE_TEXT to the
    input_sock, brief gap, then WAKE_ENTER. Bare Enter on a claude/codex
    that has STOPPED its previous turn does NOT make it tick again;
    the text+Enter combo IS the wake recipe.
  - ``mode="bare-enter"`` — for cases where the tool is at a shell
    prompt or first-launch dialog and a bare keystroke kicks the next
    step. Used by ``greatminds restart`` after re-launching a dead
    window: tmux runs the launcher shell, and a bare Enter accepts the
    pre-filled ``greatminds start-agent`` line.

Two-channel send (in priority order, unchanged from iter-1):

  1. **pty-launch input_sock** — preferred. Master end of the agent's
     pty; bytes here are real keystrokes.
  2. **tmux send-keys** — fallback when no input_sock exists.

Per-tool key sequences (unchanged from iter-1):

  - claude: ``["Enter"]``
  - codex:  ``["Enter", "C-j", "C-m"]`` — defensive fallbacks; codex
    CLI key handling shifts between versions.
  - cursor: ``["Enter"]``

Verification (mode="wake"):
  Poll ``coord/heartbeat.<role-lower>`` mtime for up to ``verify_timeout_s``
  (default 30s, configurable). Success = mtime advanced past the
  pre-send baseline. This matches what every other place in the
  codebase considers "agent acted".

Verification (mode="bare-enter"):
  Pane-diff (kept from iter-1) — for the launcher/dialog case there is
  no role heartbeat to poll. The agent isn't running yet.

Failure mode: ship-blocking diagnostic with the full attempt log.
Failure for the user-visible "wake an idle agent" path returns False
explicitly; per task 0051 USER directive, callers do NOT paper this
over with a timeout retry.

Known gap (filed as a follow-up by TESTER): codex agents on the
avatar fleet did not write a `heartbeat.<role>` file at all (cell 3
SKIPPED). That's a separate plumbing bug — the codex role-loop wrapper
doesn't reach the heartbeat-write step at first tick. press_enter
correctly reports the gap (no heartbeat = no verify signal for
mode="wake"), so codex roles will currently return False under
mode="wake" until the codex heartbeat plumbing is fixed.
"""
from __future__ import annotations

import json
import os
import socket as _socket
import subprocess
import time
from pathlib import Path


AGENT_ENTER_KEYS: dict[str, list[str]] = {
    "claude": ["Enter"],
    "codex":  ["Enter", "C-j", "C-m"],
    "cursor": ["Enter"],
}

_TRUST_PROMPT_PATTERNS: tuple[str, ...] = (
    "Do you trust the files in this folder",
    "Do you trust this folder",
    "Trust this workspace",
    "Allow Codex to run",
)

# Default verify timeout was 5s in iter-2 — TESTER's cell-1 found
# claude tools take ~30s to start a fresh tick (load context, do first
# heartbeat-writing CLI call). Bumped to 30s per REVIEWER changes_requested
# on 0051 iter-2; matches observed tool latency without papering over
# real failures (heartbeat poll exits early as soon as mtime advances,
# so this is a CEILING, not a floor — fast wakes still return fast).
_DEFAULT_VERIFY_TIMEOUT_S = 30.0
_HEARTBEAT_POLL_INTERVAL_S = 0.25

# Wake recipe: text first, then Enter. Mirrors WAKE_TEXT / WAKE_ENTER in
# coordd.push_to_role — the only proven-in-prod "make agent act" recipe.
_WAKE_TEXT = b"continue your tick"
_WAKE_GAP_S = 0.2


def _has_trust_prompt(text: str) -> bool:
    return any(p in text for p in _TRUST_PROMPT_PATTERNS)


def _capture_pane(session: str, window: str) -> str:
    cp = subprocess.run(
        ["tmux", "capture-pane", "-t", f"{session}:{window}", "-p", "-J"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        return ""
    return cp.stdout or ""


def _send_via_input_sock(input_sock_path: str, payload: bytes,
                         timeout_s: float = 2.0) -> bool:
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        s.connect(input_sock_path)
        s.sendall(payload)
        s.close()
    except OSError:
        return False
    return True


def _send_via_tmux(session: str, window: str, key: str) -> bool:
    cp = subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:{window}", key],
        capture_output=True, text=True,
    )
    return cp.returncode == 0


def _read_registry(coord_dir: Path, role_lower: str) -> dict | None:
    p = coord_dir / ".agent_registry" / f"{role_lower}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid_int: int) -> bool:
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_stopped(pid_int: int) -> bool:
    """True if ``/proc/<pid>/status`` reports State: T (stopped via SIGSTOP).

    Used by the no-heartbeat-baseline fallback in ``mode="wake"`` to
    distinguish a real wake (process actually running, can act on
    Enter) from a false-positive where input_sock bytes echoed into
    the pty buffer but the underlying process was SIGSTOPped and
    couldn't process them (REVIEWER's iter-2 finding on cell 4).

    Returns False if /proc isn't readable or the file is gone — better
    to fall through than to crash the primitive on a non-Linux host.
    """
    try:
        with open(f"/proc/{pid_int}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("State:"):
                    # Format: "State:\tT (stopped)\n" — first letter is the code.
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "T":
                        return True
                    return False
    except OSError:
        return False
    return False


_KEY_TO_BYTES: dict[str, bytes] = {
    "Enter": b"\r",
    "C-m":   b"\r",
    "C-j":   b"\n",
}


def _heartbeat_mtime(coord_dir: Path, role_lower: str) -> float | None:
    """Return mtime of ``coord/heartbeat.<role-lower>``, or None if
    the file is absent / unreadable. None means "no heartbeat signal
    to compare against" — caller falls back to pane-diff verification."""
    p = coord_dir / f"heartbeat.{role_lower}"
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _poll_heartbeat_advance(coord_dir: Path, role_lower: str,
                             baseline: float, timeout_s: float) -> bool:
    """Poll heartbeat mtime up to ``timeout_s`` seconds, looking for
    a value > ``baseline``. Returns True on advance, False on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        now_mtime = _heartbeat_mtime(coord_dir, role_lower)
        if now_mtime is not None and now_mtime > baseline:
            return True
        time.sleep(_HEARTBEAT_POLL_INTERVAL_S)
    return False


def press_enter(
    coord_dir: Path,
    session: str,
    window: str,
    role_lower: str,
    agent_type: str,
    *,
    mode: str = "wake",
    verify: bool = True,
    verify_timeout_s: float = _DEFAULT_VERIFY_TIMEOUT_S,
) -> tuple[bool, str]:
    """Wake an agent in a tmux window.

    Args:
      coord_dir: project's coordination/ directory (heartbeats + registry).
      session, window: tmux target.
      role_lower: registry key + heartbeat suffix.
      agent_type: ``claude`` | ``codex`` | ``cursor``.
      mode: ``"wake"`` (text+Enter, polls heartbeat) or ``"bare-enter"``
            (single Enter, pane-diff verify). Default ``"wake"``.
      verify: if False, return success on first successful write without
              waiting for the agent to react. Used by ``greatminds restart``
              where its own ``_verify()`` step runs the full check later.
      verify_timeout_s: max wait for heartbeat to advance (mode="wake")
              or for pane to change (mode="bare-enter"). Default 30s
              (bumped from 5s in iter-3 — matches observed tool latency
              for claude tick startup; heartbeat poll exits early on
              advance so fast wakes still return fast).

    Returns ``(success, diagnostic)``. Success = agent observably acted.
    """
    if mode not in ("wake", "bare-enter"):
        return (False, f"unknown mode {mode!r}; expected 'wake' or 'bare-enter'")

    keys = AGENT_ENTER_KEYS.get(agent_type, AGENT_ENTER_KEYS["claude"])
    reg = _read_registry(coord_dir, role_lower)

    # Pid liveness gate.
    pid_int: int | None = None
    if reg is not None:
        raw = reg.get("pid")
        try:
            pid_int = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            pid_int = None
    if pid_int is not None and not _pid_alive(pid_int):
        return (False, f"pid {pid_int} not alive — agent is dead, "
                f"need fresh launch (restart), not Enter")

    hb_baseline = _heartbeat_mtime(coord_dir, role_lower) if mode == "wake" else None
    # Pane baseline: capture in bare-enter mode AND in the wake mode's
    # no-heartbeat-baseline fallback path. The wake-no-heartbeat path
    # used to require only "post-send pane non-empty" — REVIEWER's
    # iter-2 finding: that produced a false-positive on SIGSTOPped
    # agents because input_sock echo writes the bytes into the pty
    # buffer even when the underlying process can't process them.
    # iter-3 fix: capture before-pane and require pane CHANGED (not
    # just non-empty) AND process not in T-stopped state.
    pane_baseline = ""
    if verify and (mode == "bare-enter" or (mode == "wake" and hb_baseline is None)):
        pane_baseline = _capture_pane(session, window)

    attempts: list[str] = []
    input_sock = reg.get("input_sock") if reg else None

    for key in keys:
        channel = ""
        sent = False
        key_bytes = _KEY_TO_BYTES.get(key, b"\r")

        # Channel 1: input_sock. For mode="wake", send WAKE_TEXT first,
        # brief gap, then the Enter key — mirrors coordd.push_to_role,
        # the only recipe proven in production to make an idle agent act.
        if input_sock and Path(input_sock).exists():
            if mode == "wake":
                ok1 = _send_via_input_sock(input_sock, _WAKE_TEXT)
                if ok1:
                    time.sleep(_WAKE_GAP_S)
                    sent = _send_via_input_sock(input_sock, key_bytes)
                    channel = f"input_sock={input_sock} (wake: text+{key})"
                else:
                    channel = f"input_sock={input_sock}: text write FAILED"
            else:
                sent = _send_via_input_sock(input_sock, key_bytes)
                channel = f"input_sock={input_sock} (bare {key})"

        # Channel 2: tmux send-keys fallback.
        if not sent:
            if mode == "wake":
                # Type the wake text first, then the Enter key.
                _send_via_tmux(session, window, _WAKE_TEXT.decode("utf-8"))
                time.sleep(_WAKE_GAP_S)
            sent = _send_via_tmux(session, window, key)
            channel = (f"tmux send-keys ({'wake: text+' if mode == 'wake' else 'bare '}"
                       f"{key})")

        if not sent:
            attempts.append(f"{channel}: write FAILED")
            continue

        if not verify:
            return (True, f"{channel} (no verify)")

        # Verification path 1: heartbeat-mtime polling (mode="wake").
        if mode == "wake":
            if hb_baseline is None:
                # No baseline heartbeat exists — codex first-launch path,
                # or a brand-new role with no prior tick. Fall back to
                # pane-diff + process-state check (REVIEWER iter-2
                # changes_requested: pane-non-empty alone false-positives
                # on SIGSTOPped agents because input_sock echo populates
                # the pty buffer even when the process can't act).
                #
                # Success requires ALL of:
                #   (a) pane visibly changed since baseline,
                #   (b) not parked at a trust prompt,
                #   (c) process is NOT in T-stopped state (Linux /proc).
                time.sleep(verify_timeout_s)
                after = _capture_pane(session, window)
                trust_blocked = _has_trust_prompt(after)
                pane_changed = (after != pane_baseline) and bool(after)
                stopped = (pid_int is not None and _pid_stopped(pid_int))
                if pane_changed and not trust_blocked and not stopped:
                    return (True, f"{channel} (no prior heartbeat; "
                            f"pane changed, process not stopped)")
                # Build a targeted diagnostic so callers see WHICH
                # condition failed — REVIEWER asked for honest no-action
                # signaling, not optimistic fallback success.
                fail_reasons: list[str] = []
                if not pane_changed:
                    fail_reasons.append("pane unchanged")
                if trust_blocked:
                    fail_reasons.append("pane at trust prompt")
                if stopped:
                    fail_reasons.append(f"process pid={pid_int} SIGSTOPped (T)")
                attempts.append(
                    f"{channel}: no heartbeat baseline; " + ", ".join(fail_reasons)
                )
                pane_baseline = after  # next attempt's baseline
                continue
            advanced = _poll_heartbeat_advance(
                coord_dir, role_lower, hb_baseline, verify_timeout_s,
            )
            if advanced:
                new_mtime = _heartbeat_mtime(coord_dir, role_lower) or 0
                delta = new_mtime - hb_baseline
                return (True, f"{channel} (heartbeat advanced "
                        f"+{delta:.1f}s within {verify_timeout_s:.0f}s)")
            attempts.append(f"{channel}: heartbeat did not advance within "
                            f"{verify_timeout_s:.0f}s")
            continue

        # Verification path 2: pane-diff (mode="bare-enter"). Kept for
        # launcher/dialog cases where no role heartbeat exists yet.
        time.sleep(verify_timeout_s)
        after = _capture_pane(session, window)
        changed = (after != pane_baseline) and bool(after)
        trust_blocked = _has_trust_prompt(after)
        if changed and not trust_blocked:
            return (True, f"{channel} (pane changed)")
        if trust_blocked:
            attempts.append(f"{channel}: pane still at trust prompt — "
                            f"Enter did not advance the role")
        else:
            attempts.append(f"{channel}: pane unchanged, agent did not act")
        pane_baseline = after

    return (
        False,
        f"no key advanced the agent after {len(keys)} attempt(s) "
        f"(mode={mode}): " + " | ".join(attempts),
    )
