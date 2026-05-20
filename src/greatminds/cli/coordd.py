#!/usr/bin/env python3
"""coordd — optional coordination daemon. Closes the ScheduleWakeup gap.

Two responsibilities, run every <interval> seconds:

  1. Drive notify_from_journal — replay any new journal.ndjson lines and
     write wake-up messages to inbox/<role>/. (If PostToolUse hooks
     already did this, the call is a cheap no-op since state advances
     monotonically.)
  2. Scan coordination/inbox/<role>/ for new files. For each new file,
     find the role's active TTY from coordination/.agent_registry/<role>.json
     and write "проверь inbox и продолжай тик\n" to that TTY. The sleeping
     agent wakes, reads the inbox, and continues the tick.

If the daemon is NOT running, nothing changes — agents still work via
ScheduleWakeup polling, just with the old multi-minute latency. The daemon
is purely additive ("нашлепка"): killing it never breaks the pipeline.

The daemon never moves task files, never edits queues, never decides on
state. Its only side effects are:
  - calling bin/notify_from_journal (which writes inbox messages),
  - writing wake-up text to known agent TTYs.

State sources (read-only):
  - coordination/inbox/<role>/*.md   — new files to react to
  - coordination/.agent_registry/<role>.json — written by bin/start_agent
    with {role, tool, pid, tty, started_at}

Usage:
    coordd [--project-dir <dir>] [--interval-sec 1.0] [--verbose]

For a long-lived install, run via systemd-user; see bin/coordd-install.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# Send text and Enter as SEPARATE writes with a short delay between them.
# Many TUIs (codex in particular) detect "paste" when bytes arrive in one
# big chunk and don't treat a trailing CR as the Enter key press. Writing
# the text first, then a brief sleep, then a single `\r` makes the Enter
# look like a discrete keypress.
WAKE_TEXT = "проверь inbox и продолжай тик"
WAKE_ENTER = "\r\n"
WAKE_GAP_SECONDS = 0.35
REGISTRY_DIR = ".agent_registry"

# Stale-heartbeat kick: nudge an agent whose heartbeat has gone cold
# (transient API errors / rate-limits stalled its self-wake), provided
# it actually has pending work and we haven't already kicked it recently.
# Stale-kick is OFF by default. Agent prompts now carry adaptive
# backoff (self-wake), and real work arrives via inbox-file push, so
# the heartbeat kick is redundant in normal operation — and it was
# actively harmful: it nudged idle agents minute-after-minute when
# nothing was happening, and injected "продолжай тик" into the middle
# of a human's interactive chat with ARCHITECT-PLANNER. Opt in only if
# you specifically need the rate-limit-stuck recovery: COORDD_STALE_KICK=1.
STALE_KICK_ENABLED = os.environ.get("COORDD_STALE_KICK", "0") == "1"
STALE_CHECK_INTERVAL_SEC = float(os.environ.get("COORDD_STALE_CHECK_INTERVAL_SEC", "60"))
STALE_KICK_SEC           = float(os.environ.get("COORDD_STALE_KICK_SEC",           "900"))
KICK_MIN_INTERVAL_SEC    = float(os.environ.get("COORDD_KICK_MIN_INTERVAL_SEC",    "600"))
# Heartbeat-freshness guard for push_to_role keystroke injection.
# If a role's heartbeat.<role> file is younger than this many seconds,
# the agent is treated as actively working — coordd refuses to inject
# keystroke mid-thought. The wake-* file stays in inbox; stop_decide
# delivers it at the next natural Stop boundary (agent's tick end).
# Default 60s: every bin/* call refreshes heartbeat as a side-effect,
# so a busy loop-mode agent always has heartbeat <10s old.
PUSH_FRESH_GUARD_SEC     = float(os.environ.get("COORDD_PUSH_FRESH_GUARD_SEC",      "60"))
# Chat-driven roles are paced by a human, not by ticks. coordd must
# NEVER inject keystrokes ("проверь inbox и продолжай тик") into their
# pty — that corrupts the live conversation. This applies to BOTH the
# stale-kick AND the inbox-file push: the inbox message is still written
# to disk and the chat agent reads it in its own flow; coordd just does
# not type into it. (A loop-mode agent still gets the keystroke push —
# that is how it wakes from sleep.) Especially important because a
# role's own task moves generate wake-to-self files via
# notify_from_journal; without this, PLANNER nudges itself repeatedly
# on every bin/task new/mv it performs.
NO_KEYSTROKE_INJECT_ROLES = {"architect-planner", "maintainer", "user"}
# Backward-compat alias (older code/tests referenced this name).
NO_STALE_KICK_ROLES = NO_KEYSTROKE_INJECT_ROLES

# Dead-pid report (H1/H8): coordd watches the agent registry and, when
# it observes a role whose pid is no longer alive, files an `ask`
# inbox message to ARCHITECT-PLANNER so the user/planner can decide on
# diagnose/restart. Throttled per-role.
DEAD_CHECK_INTERVAL_SEC  = float(os.environ.get("COORDD_DEAD_CHECK_INTERVAL_SEC",  "60"))
DEAD_REPORT_INTERVAL_SEC = float(os.environ.get("COORDD_DEAD_REPORT_INTERVAL_SEC", "600"))


def scan_inbox_files(inbox_dir: Path) -> set[str]:
    """Find pending inbox messages across all roles. After R8 bin/inbox
    writes .yaml; legacy .md is still recognised. Already-processed
    messages (renamed to processed-*) are skipped."""
    files: set[str] = set()
    if not inbox_dir.is_dir():
        return files
    for role_dir in inbox_dir.iterdir():
        if not role_dir.is_dir():
            continue
        for pattern in ("*.yaml", "*.md"):
            for f in role_dir.glob(pattern):
                if f.name == ".gitkeep":
                    continue
                if f.name.startswith("processed-"):
                    continue
                files.add(str(f.resolve()))
    return files


def role_from_path(path: str) -> str | None:
    # .../coordination/inbox/<role>/<file>.md
    p = Path(path)
    if p.parent.name == "inbox":
        return None
    # parent name is the role (lowercase by our convention)
    return p.parent.name


def read_registry(registry_dir: Path, role: str) -> dict | None:
    f = registry_dir / f"{role}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def tty_is_alive(tty_path: str, pid: int | None) -> bool:
    if not tty_path or not tty_path.startswith("/dev/"):
        return False
    if not os.path.exists(tty_path):
        return False
    if pid is not None:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
    return True


def load_schema_roles(canon_dir: Path) -> dict:
    """Load roles section from schema.yaml. Returns {} on any failure
    (yaml missing, file missing, parse error) — coordd then behaves as
    before without stale-kick claim-queue awareness."""
    if yaml is None:
        return {}
    p = canon_dir / "schema.yaml"
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    roles = data.get("roles") or {}
    return roles if isinstance(roles, dict) else {}


def heartbeat_age_seconds(coord: Path, role_lower: str) -> float | None:
    """Seconds since heartbeat.<role> was last touched. None if missing."""
    path = coord / f"heartbeat.{role_lower}"
    if not path.is_file():
        return None
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def role_has_pending_work(coord: Path, schema_roles: dict, role_lower: str) -> bool:
    """True if role has anything to do: non-empty inbox OR any task file
    in a queue it claims from (per schema.yaml)."""
    inbox = coord / "inbox" / role_lower
    if inbox.is_dir():
        for f in inbox.glob("*.md"):
            if f.name != ".gitkeep":
                return True
    role_upper = role_lower.upper()
    meta = schema_roles.get(role_upper)
    if not isinstance(meta, dict):
        return False
    for q in (meta.get("claims_from") or []):
        if not isinstance(q, str):
            continue
        qdir = coord / q
        if not qdir.is_dir():
            continue
        for f in qdir.glob("*.md"):
            if f.name == "_TEMPLATE.md":
                continue
            return True
    return False


def list_live_roles(registry_dir: Path) -> list[str]:
    """Lowercase role names with a registry entry whose pid is alive."""
    out: list[str] = []
    if not registry_dir.is_dir():
        return out
    for f in registry_dir.glob("*.json"):
        try:
            reg = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = reg.get("pid")
        try:
            pid_int = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            continue
        if pid_int is None:
            continue
        try:
            os.kill(pid_int, 0)
        except (ProcessLookupError, PermissionError):
            continue
        out.append(f.stem)
    return out


def write_dead_report(coord: Path, role: str, reg: dict, now_ts: float) -> None:
    """File an `ask` in inbox/maintainer/ that <role>'s pid is dead.

    MAINTAINER is the system-operator role that handles restart /
    diagnose / mark-down decisions. ARCHITECT-PLANNER is a product role
    and cannot perform these operations.
    """
    target_dir = coord / "inbox" / "maintainer"
    target_dir.mkdir(parents=True, exist_ok=True)
    fname = f"ask-{int(now_ts)}-dead-{role}.yaml"
    p = target_dir / fname
    if p.exists():
        return
    pid = reg.get("pid", "?")
    tool = reg.get("tool", "?")
    started = reg.get("started_at", "?")
    body = (
        f"Role {role.upper()} (pid={pid}, tool={tool}, started_at={started}) "
        f"is no longer alive. Coordd cannot push wake-ups to it; "
        f"all work routed to {role.upper()} will stall until the process "
        f"is restarted. Diagnose and either rerun bin/start_agent "
        f"{role.upper()} <tool> or, if intentional, remove "
        f".agent_registry/{role}.json so this report stops firing."
    )
    text = (
        "to_role: MAINTAINER\n"
        f"from_role: coordd\n"
        "kind: ask\n"
        f"task_ref: ''\n"
        f"sent_at: '{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ts))}'\n"
        "answered_at: null\n"
        f"body: |\n  " + body.replace("\n", "\n  ") + "\n"
    )
    p.write_text(text, encoding="utf-8")


def push_to_role(coord: Path, role: str, file_path: str, verbose: bool) -> bool:
    """Attempt to nudge the role. Prefers writing to the unix socket
    exposed by bin/pty-launch (this end is the *master* of the agent's
    pty, so bytes become real input to the running process — identical
    to user keystrokes). Falls back to writing the slave-pts /dev/pts/N
    (legacy; shows on screen but does NOT inject input).

    Returns True if the push happened (so the caller can mark the file
    as processed), False on skip/failure so the file stays "pending"
    for retry on the next tick.

    HEARTBEAT GUARD (PUSH_FRESH_GUARD_SEC): if the role's heartbeat is
    fresh (file mtime within PUSH_FRESH_GUARD_SEC of now), the agent is
    actively working — DO NOT inject keystroke mid-thought. The wake
    file remains as wake-* in inbox; the agent's own stop_decide hook
    will pick it up at the next natural Stop boundary (after current
    tick completes). Keystroke kick is reserved for agents whose
    heartbeat is stale (idle / asleep / waiting), where interrupting
    the sleep IS the goal.
    """
    if role.lower() not in NO_KEYSTROKE_INJECT_ROLES:
        hb_path = coord / f"heartbeat.{role.lower()}"
        if hb_path.is_file():
            try:
                age = time.time() - hb_path.stat().st_mtime
                if age < PUSH_FRESH_GUARD_SEC:
                    if verbose:
                        print(
                            f"  skip-fresh: role={role} heartbeat {age:.0f}s old < {PUSH_FRESH_GUARD_SEC:.0f}s; "
                            f"agent active, NOT interrupting (file stays for natural Stop pickup)",
                            file=sys.stderr,
                        )
                    return False
            except OSError:
                pass  # treat unreadable heartbeat as stale → proceed with kick

    registry_dir = coord / REGISTRY_DIR
    reg = read_registry(registry_dir, role)
    if reg is None:
        if verbose:
            print(f"  skip: no registry entry for role={role}", file=sys.stderr)
        return False

    pid = reg.get("pid")
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_int = None
    if pid_int is not None:
        try:
            os.kill(pid_int, 0)
        except (ProcessLookupError, PermissionError):
            if verbose:
                print(f"  skip: pid {pid} not alive for role={role}", file=sys.stderr)
            return False

    # Preferred: unix socket exposed by pty-launch (writes to pty master,
    # real input injection).
    input_sock = reg.get("input_sock")
    if input_sock and Path(input_sock).exists():
        try:
            import socket as _s

            # Open TWO separate connections: one for the text, then a
            # short pause, then a second one for the Enter (\r\n). The
            # split prevents TUIs (codex in particular) from treating
            # the whole thing as a paste chunk where the trailing CR
            # is not interpreted as a keypress. It also stays compatible
            # with older pty-launch instances that close the socket
            # after one recv.
            def _send(data: bytes) -> None:
                s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
                s.settimeout(2.0)
                s.connect(input_sock)
                s.sendall(data)
                s.close()

            # Submit sequence is tool-specific. claude/codex accept
            # "\r\n". cursor-agent's Composer input does NOT submit on
            # "\r\n" — the trailing \n cancels the submit (verified
            # empirically: bare \r submits, \r\n leaves text in the
            # box). So for cursor send a bare CR.
            enter = "\r" if reg.get("tool") == "cursor" else WAKE_ENTER
            _send(WAKE_TEXT.encode("utf-8"))
            time.sleep(WAKE_GAP_SECONDS)
            _send(enter.encode("utf-8"))
            if verbose:
                print(
                    f"  nudge: role={role} via socket {input_sock} (trigger: {Path(file_path).name})",
                    file=sys.stderr,
                )
            return True
        except OSError as exc:
            if verbose:
                print(f"  socket nudge failed for role={role}: {exc}", file=sys.stderr)
            # fall through to legacy TTY write

    # Legacy fallback: write to slave pts. Shows on screen as text but
    # does NOT inject input for most terminals. Kept for backward
    # compatibility with agents not started via pty-launch.
    tty = reg.get("tty")
    if tty and tty.startswith("/dev/") and os.path.exists(tty):
        try:
            with open(tty, "wb", buffering=0) as f:
                f.write(WAKE_TEXT.encode("utf-8"))
                f.flush()
                time.sleep(WAKE_GAP_SECONDS)
                f.write(WAKE_ENTER.encode("utf-8"))
            if verbose:
                print(
                    f"  nudge: role={role} legacy tty={tty} (trigger: {Path(file_path).name})",
                    file=sys.stderr,
                )
            return True
        except OSError as exc:
            if verbose:
                print(f"  fail: role={role} tty={tty} err={exc}", file=sys.stderr)
            return False

    if verbose:
        print(f"  skip: role={role} has no usable input channel", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-coordd`` in pyproject.toml."""
    from greatminds.core.paths import find_canon_dir

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Project root containing coordination/ (default: cwd).",
    )
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=1.0,
        help="Polling interval. 1.0 keeps CPU near zero on idle. Don't go below 0.2.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    coord = args.project_dir / "coordination"
    if not coord.is_dir():
        print(f"coordd: error: {coord} not found", file=sys.stderr)
        return 1
    inbox = coord / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    registry = coord / REGISTRY_DIR
    registry.mkdir(parents=True, exist_ok=True)

    interval = max(0.2, float(args.interval_sec))

    # Graceful exit on SIGTERM / SIGINT
    stop = {"flag": False}

    def handler(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    # Baseline is empty: any unprocessed inbox file present at startup
    # gets pushed once. The previous behaviour (baseline = current files)
    # silently swallowed real pending messages whenever coordd restarted.
    # Per-file "known" tracking below still prevents repeated pushes for
    # the same file during one daemon lifetime.
    baseline: set[str] = set()
    if args.verbose:
        pending = scan_inbox_files(inbox)
        print(
            f"coordd: watching {inbox}, registry {registry}, "
            f"pending-at-start {len(pending)} files, interval {interval}s",
            file=sys.stderr,
        )

    # notify_from_journal is invoked via ``python -m greatminds.cli.notify_from_journal``
    # so this works whether or not the greatminds-notify-journal entry-point is on PATH.
    notify_invocation = [sys.executable, "-m", "greatminds.cli.notify_from_journal"]

    # Schema roles (for claim-queue awareness in stale-kick). Reloaded
    # lazily — if schema.yaml changes we'll pick up on the next process
    # restart, which is fine.
    canon_dir = find_canon_dir()
    schema_roles = load_schema_roles(canon_dir)
    if args.verbose:
        print(
            f"coordd: stale-kick enabled "
            f"(check every {STALE_CHECK_INTERVAL_SEC:.0f}s, "
            f"threshold {STALE_KICK_SEC:.0f}s, "
            f"min interval per role {KICK_MIN_INTERVAL_SEC:.0f}s, "
            f"schema roles: {len(schema_roles)})",
            file=sys.stderr,
        )

    # Per-role timestamp of last stale-kick we issued. Throttles so we
    # don't pile pushes on a role that's genuinely waiting/retrying.
    last_kick: dict[str, float] = {}
    last_stale_check: float = 0.0

    # Per-role timestamp of last dead-pid report issued to PLANNER's
    # inbox. Throttles so a long-dead agent doesn't flood the inbox.
    last_dead_report: dict[str, float] = {}
    last_dead_check: float = 0.0

    # Push exactly once per inbox file. If the push lands but the agent
    # doesn't react, that's on the agent — coordd is not the place to
    # retry; agents have their own ScheduleWakeup/sleep loop for that.
    known: set[str] = set(baseline)
    while not stop["flag"]:
        try:
            time.sleep(interval)

            # Step 1: run notify_from_journal — writes inbox messages for any
            # new journal lines. Idempotent (state file tracks last offset).
            try:
                import subprocess
                subprocess.run(
                    [
                        *notify_invocation,
                        "--project-dir", str(args.project_dir),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception as exc:
                if args.verbose:
                    print(f"coordd: notify_from_journal call failed: {exc}", file=sys.stderr)

            # Step 2: scan inbox/ for new files since last cycle, push
            # exactly ONCE per file regardless of outcome. Previously a
            # failed push (role has no registry entry / no socket — e.g.
            # a chat-only role with no pty-launch) was retried EVERY tick
            # forever, hammering the daemon and flooding the log. One
            # attempt per file per daemon lifetime; if it can't be
            # delivered, that's the agent's problem to pick up when it
            # next runs (the file is still there, just not re-pushed).
            current = scan_inbox_files(inbox)
            for path in sorted(current - known):
                role = role_from_path(path)
                known.add(path)  # mark BEFORE attempt — never retry-spam
                if role is None:
                    continue
                if role in NO_KEYSTROKE_INJECT_ROLES:
                    # chat-driven role: deliver the file (already on disk),
                    # but do NOT type into its live session.
                    if args.verbose:
                        print(f"  deliver-only (chat role, no keystroke): "
                              f"role={role} file={Path(path).name}",
                              file=sys.stderr)
                    continue
                push_to_role(coord, role, path, args.verbose)
            # Drop known entries for files agents have processed and deleted.
            known &= current

            # Step 3: stale-heartbeat kick. For each live role whose
            # heartbeat is older than STALE_KICK_SEC AND that has pending
            # work, push a wake — but at most once per KICK_MIN_INTERVAL_SEC.
            # Rationale: transient API errors / rate-limits can leave an
            # agent waiting at the prompt with no self-wake scheduled. We
            # don't try to detect "rate limit" specifically — we just
            # observe that the heartbeat has gone cold while work is
            # waiting, and nudge.
            now_ts = time.time()
            if STALE_KICK_ENABLED and now_ts - last_stale_check >= STALE_CHECK_INTERVAL_SEC:
                last_stale_check = now_ts
                for role_lower in list_live_roles(registry):
                    if role_lower in NO_STALE_KICK_ROLES:
                        continue  # human-paced chat role — never kick
                    age = heartbeat_age_seconds(coord, role_lower)
                    if age is None or age < STALE_KICK_SEC:
                        continue
                    if not role_has_pending_work(coord, schema_roles, role_lower):
                        continue
                    if now_ts - last_kick.get(role_lower, 0.0) < KICK_MIN_INTERVAL_SEC:
                        continue
                    if push_to_role(
                        coord, role_lower,
                        f"<stale-kick: heartbeat {age:.0f}s old>",
                        args.verbose,
                    ):
                        last_kick[role_lower] = now_ts

            # Step 4: dead-pid watch. For each role in the registry,
            # if pid is no longer alive, file an ask to PLANNER's inbox.
            # Throttled per-role (DEAD_REPORT_INTERVAL_SEC). When the
            # role becomes alive again (registry rewritten by pty-launch),
            # the throttle slot is reset so a future death gets reported.
            if now_ts - last_dead_check >= DEAD_CHECK_INTERVAL_SEC:
                last_dead_check = now_ts
                for reg_file in sorted(registry.glob("*.json")):
                    role = reg_file.stem
                    reg = read_registry(registry, role)
                    if reg is None:
                        continue
                    pid = reg.get("pid")
                    try:
                        pid_int = int(pid) if pid is not None else None
                    except (TypeError, ValueError):
                        pid_int = None
                    alive = False
                    if pid_int is not None:
                        try:
                            os.kill(pid_int, 0)
                            alive = True
                        except (ProcessLookupError, PermissionError):
                            alive = False
                    if alive:
                        # role came back alive → reset throttle so future
                        # deaths get reported.
                        last_dead_report.pop(role, None)
                        continue
                    last = last_dead_report.get(role, 0.0)
                    if now_ts - last < DEAD_REPORT_INTERVAL_SEC:
                        continue
                    try:
                        write_dead_report(coord, role, reg, now_ts)
                        last_dead_report[role] = now_ts
                        if args.verbose:
                            print(
                                f"  dead-report: role={role} pid={pid} → inbox/maintainer/",
                                file=sys.stderr,
                            )
                    except OSError as exc:
                        if args.verbose:
                            print(f"  dead-report failed for {role}: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            if args.verbose:
                print(f"coordd: error in poll loop: {exc}", file=sys.stderr)
            time.sleep(1.0)

    if args.verbose:
        print("coordd: exit", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
