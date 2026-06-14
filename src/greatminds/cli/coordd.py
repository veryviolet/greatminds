#!/usr/bin/env python3
"""coordd — optional coordination daemon. Closes the ScheduleWakeup gap.

Two responsibilities, run every <interval> seconds:

  1. Drive notify_from_journal — replay any new journal.ndjson lines and
     write wake-up messages to inbox/<role>/. (If PostToolUse hooks
     already did this, the call is a cheap no-op since state advances
     monotonically.)
  2. Scan coordination/inbox/<role>/ for new files. For each new file,
     find the role's active TTY from coordination/.agent_registry/<role>.json
     and write "check inbox and continue your tick\n" to that TTY. The sleeping
     agent wakes, reads the inbox, and continues the tick.

If the daemon is NOT running, nothing changes — agents still work via
ScheduleWakeup polling, just with the old multi-minute latency. The daemon
is purely additive (a "side-load"): killing it never breaks the pipeline.

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

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import click

from greatminds.cli import codex_auth

try:
    import yaml
except ImportError:
    yaml = None

# Send text and Enter as SEPARATE writes with a short delay between them.
# Many TUIs (codex in particular) detect "paste" when bytes arrive in one
# big chunk and don't treat a trailing CR as the Enter key press. Writing
# the text first, then a brief sleep, then a single `\r` makes the Enter
# look like a discrete keypress.
WAKE_TEXT = "check inbox and continue your tick"
WAKE_ENTER = "\r\n"
WAKE_GAP_SECONDS = 0.35
REGISTRY_DIR = ".agent_registry"

# In-flight-turn hang detection. A driven turn runs as a coordd
# subprocess holding the role's run-lock for the turn's duration. If the
# lock has been held longer than the hang threshold AND the role's
# heartbeat has not advanced within that window, the turn is hung —
# coordd escalates ONCE to MAINTAINER (it does NOT kill; MAINTAINER
# decides). The threshold is the single global schema.heartbeat.
# hang_threshold_seconds (env override for ops).
HANG_CHECK_INTERVAL_SEC  = float(os.environ.get("COORDD_HANG_CHECK_INTERVAL_SEC", "30"))
HANG_THRESHOLD_DEFAULT   = 300.0


def _hang_threshold_seconds(canon_dir: Path) -> float:
    """Read schema.heartbeat.hang_threshold_seconds (the in-flight-turn
    hang bound). Falls back to HANG_THRESHOLD_DEFAULT. Env override:
    COORDD_HANG_THRESHOLD_SEC."""
    env = os.environ.get("COORDD_HANG_THRESHOLD_SEC")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        doc = yaml.safe_load(
            (canon_dir / "schema.yaml").read_text(encoding="utf-8")) or {}
        val = (doc.get("heartbeat") or {}).get("hang_threshold_seconds")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
    except (OSError, yaml.YAMLError):
        pass
    return HANG_THRESHOLD_DEFAULT


def write_hang_report(coord: Path, role_lower: str, lock_age: float,
                      hb_age: "float | None", now_ts: float) -> None:
    """File an inbox/maintainer ask: a driven turn for <role> appears
    hung (run-lock held + heartbeat not advancing). coordd does NOT
    kill — MAINTAINER diagnoses and decides."""
    target = coord / "inbox" / "maintainer"
    target.mkdir(parents=True, exist_ok=True)
    p = target / f"ask-{int(now_ts)}-hung-{role_lower}.yaml"
    if p.exists():
        return
    hb = f"{hb_age:.0f}s" if hb_age is not None else "never (no heartbeat this turn)"
    body = (
        f"Role {role_lower.upper()} has a DRIVEN turn that appears HUNG. "
        f"coordd dispatched a turn (run-lock held {lock_age:.0f}s) but the "
        f"role's heartbeat has not advanced ({hb} since last touch) — the "
        f"turn's subprocess is running but making no progress. coordd does "
        f"NOT kill it. Diagnose and decide: kill the turn subprocess + clear "
        f"coordination/.locks/driven-{role_lower}.lock so coordd can re-drive, "
        f"or investigate why the turn stalled (MCP init, blocked tool, rate "
        f"limit). Turn output: coordination/.turns/{role_lower}-*.log."
    )
    text = (
        "to_role: MAINTAINER\n"
        "from_role: coordd\n"
        "kind: ask\n"
        "task_ref: ''\n"
        f"sent_at: '{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_ts))}'\n"
        "answered_at: null\n"
        "body: |\n  " + body.replace("\n", "\n  ") + "\n"
    )
    p.write_text(text, encoding="utf-8")


def _driven_lock_decision(lock_age: float, hb_age: "float | None",
                          hang_threshold: float,
                          driven_hang_threshold: float,
                          reclaim_grace: float,
                          since_last_report: float) -> str:
    """Pure classifier for one driven run-lock in the hang sweep.
    Returns ``"skip"`` | ``"report"`` | ``"reclaim"``.

    - lock younger than the kill bound, or heartbeat fresh within the
      window → ``"skip"`` (the turn is legitimately running / progressing).
    - lock present past the kill bound PLUS ``reclaim_grace`` → ``"reclaim"``:
      the turn's subprocess was hard-killed at the kill bound and its
      worker's finally has had ample time to release, so a surviving lock is
      a genuine orphan coordd should unlink so the role can re-drive
      (issue #11 orphan-lock defense).
    - else, if no hang report fired within ``driven_hang_threshold`` →
      ``"report"`` (escalate once to MAINTAINER).
    - otherwise → ``"skip"`` (already escalated recently)."""
    if lock_age < driven_hang_threshold:
        return "skip"
    if hb_age is not None and hb_age < hang_threshold:
        return "skip"
    if lock_age >= driven_hang_threshold + reclaim_grace:
        return "reclaim"
    if since_last_report < driven_hang_threshold:
        return "skip"
    return "report"
# Heartbeat-freshness guard for push_to_role keystroke injection.
# If a role's heartbeat.<role> file is younger than this many seconds,
# the agent is treated as actively working — coordd refuses to inject
# keystroke mid-thought. The wake-* file stays in inbox; stop_decide
# delivers it at the next natural Stop boundary (agent's tick end).
# Default 60s: every bin/* call refreshes heartbeat as a side-effect,
# so a busy loop-mode agent always has heartbeat <10s old.
PUSH_FRESH_GUARD_SEC     = float(os.environ.get("COORDD_PUSH_FRESH_GUARD_SEC",      "60"))
# Chat-driven roles are paced by a human, not by ticks. coordd must
# NEVER inject keystrokes ("check inbox and continue your tick") into their
# pty — that corrupts the live conversation. The inbox message is still
# written to disk and the chat agent reads it in its own flow; coordd just
# does not type into it. (A loop-mode agent still gets the keystroke push —
# that is how it wakes from sleep.) Especially important because a
# role's own task moves generate wake-to-self files via
# notify_from_journal; without this, PLANNER nudges itself repeatedly
# on every task new/mv it performs.
NO_KEYSTROKE_INJECT_ROLES = {"architect-planner", "maintainer", "user"}

# Dead-pid report (H1/H8): coordd watches the agent registry and, when
# it observes a role whose pid is no longer alive, files an `ask`
# inbox message to ARCHITECT-PLANNER so the user/planner can decide on
# diagnose/restart. Throttled per-role.
DEAD_CHECK_INTERVAL_SEC  = float(os.environ.get("COORDD_DEAD_CHECK_INTERVAL_SEC",  "60"))
DEAD_REPORT_INTERVAL_SEC = float(os.environ.get("COORDD_DEAD_REPORT_INTERVAL_SEC", "600"))

# Periodic orphaned-intent reaping. An intent file is left behind
# when an agent crashes mid-tick (intent written, task never moved); the
# `intent-clean` reaper existed but nothing RAN it, so orphans piled up
# for hours on a long-lived coordination dir and surfaced as watchdog
# findings. coordd now reaps them on this cadence using the same
# safe-by-default logic (only intents older than the min-age whose task
# already left the from-queue). Min-age comes from
# schema.watchdog.intent_orphan_seconds.
INTENT_REAP_INTERVAL_SEC = float(
    os.environ.get("COORDD_INTENT_REAP_INTERVAL_SEC", "300"))
INTENT_ORPHAN_MIN_AGE_DEFAULT = 300.0
# Driven-turn retry policy. A driven turn that ERRORS or TIMES OUT is
# retried; a clean completion is NOT (the agent may legitimately have had
# nothing to do). Rate-limit (429/529) retries effectively forever with
# exponential backoff; other errors retry RETRY_HARD_MAX times then the
# role is escalated to MAINTAINER and auto-retry stops until a real event.
# Authentication failures stop immediately: retrying cannot repair an
# expired/missing external-tool session and only makes the dashboard look
# busy while no work can happen.
RETRY_RL_BASE_SEC   = float(os.environ.get("COORDD_RETRY_RL_BASE_SEC",   "30"))
RETRY_RL_CAP_SEC    = float(os.environ.get("COORDD_RETRY_RL_CAP_SEC",   "300"))
RETRY_RL_NOTIFY_AT  = int(os.environ.get("COORDD_RETRY_RL_NOTIFY_AT",     "20"))
RETRY_HARD_BASE_SEC = float(os.environ.get("COORDD_RETRY_HARD_BASE_SEC", "30"))
RETRY_HARD_CAP_SEC  = float(os.environ.get("COORDD_RETRY_HARD_CAP_SEC", "120"))
RETRY_HARD_MAX      = int(os.environ.get("COORDD_RETRY_HARD_MAX",         "3"))
DRIVEN_TURN_TIMEOUT_SEC = float(
    os.environ.get("COORDD_DRIVEN_TURN_TIMEOUT_SEC", "1800"))
# issue #11 orphan-lock defense: a driven turn's subprocess is hard-killed
# at DRIVEN_TURN_TIMEOUT_SEC and its worker's finally then releases the
# run-lock. So a lock still present this many seconds PAST the kill bound
# has no live subprocess behind it — the worker died abnormally (or coordd
# was wedged) and the lock is a genuine orphan. coordd reclaims it (unlinks)
# so the role can re-drive, instead of leaving it for manual cleanup. The
# grace is generous so it can never race a worker still finalizing its kill.
ORPHAN_RECLAIM_GRACE_SEC = float(
    os.environ.get("COORDD_ORPHAN_RECLAIM_GRACE_SEC", "300"))
# 0369 (GitHub #18): fail-fast tuning for the driven ``claude -p`` subprocess.
# The hang's root cause is claude's own API retry defaults — API_TIMEOUT_MS
# (10 min per attempt) × CLAUDE_CODE_MAX_RETRIES (10) means a rate-limited /
# overloaded turn silently retries IN-PROCESS for many minutes (observed
# 12–22 min) while holding driven-<role>.lock, then returns an EMPTY result.
# coordd already classifies a returned rate_limit/overloaded result and
# schedules its OWN backoff retry (_note_turn_outcome) — but only once the
# turn returns. Shrinking claude's in-process timeout + retry count makes the
# turn return in ~1–2 min so the lock frees and coordd's retry scheduler (with
# its visible backoff) takes over, instead of the queue starving for the full
# 30-min DRIVEN_TURN_TIMEOUT_SEC backstop. Both are env-overridable so an
# operator can widen them without a code change.
DRIVEN_API_TIMEOUT_MS = os.environ.get(
    "COORDD_DRIVEN_API_TIMEOUT_MS", "120000")  # 120s per API attempt
DRIVEN_MAX_RETRIES = os.environ.get(
    "COORDD_DRIVEN_MAX_RETRIES", "2")
# Stand auto-deploy retry: a deploy that RAISES before transitioning
# leaves the stand stuck in `preparing`. coordd re-attempts it periodically
# and, after DEPLOY_MAX_ATTEMPTS, escalates to MAINTAINER and forces the
# stand `down` so it is not stuck forever. (rc!=0 ansible failures already
# transition to `down` inside deploy_lease — those are not retried here.)
DEPLOY_MAX_ATTEMPTS = int(os.environ.get("COORDD_DEPLOY_MAX_ATTEMPTS", "3"))
DEPLOY_RETRY_INTERVAL_SEC = float(
    os.environ.get("COORDD_DEPLOY_RETRY_INTERVAL_SEC", "60"))
# Periodic re-reconcile of the driven backlog. The startup reconcile runs
# once; without a periodic repeat a driven turn that FAILED (rate-limit /
# transient error) or completed without moving its task never retries — no
# queue event fires, so the role freezes. This re-drives pending roles
# gently (it is lock-safe: skips roles with a turn in flight).
RECONCILE_INTERVAL_SEC = float(
    os.environ.get("COORDD_RECONCILE_INTERVAL_SEC", "90"))
# 0376: ENOSPC / low-disk resilience. A full root disk silently breaks
# driven turns: codex/claude obtains a refreshed auth token but cannot
# persist auth.json (ENOSPC), so the next turn reuses an already-consumed
# refresh token and fails with refresh_token_reused/token_expired — looking
# like an auth bug, not a disk bug. coordd refuses to spawn a driven turn
# when the coordination filesystem is below EITHER threshold (absolute MB or
# percent free) and surfaces an explicit ENOSPC blocker to MAINTAINER. Both
# thresholds default low (a turn is cheap to defer; a corrupted token is
# expensive) and are env-overridable for tests / tighter fleets.
DISK_MIN_FREE_MB = float(os.environ.get("COORDD_DISK_MIN_FREE_MB", "512"))
DISK_MIN_FREE_PCT = float(os.environ.get("COORDD_DISK_MIN_FREE_PCT", "1.0"))
# Re-notify MAINTAINER about a sustained low-disk block at most this often
# (a flood of queue events must not spam the inbox).
LOW_DISK_RENOTIFY_SEC = float(
    os.environ.get("COORDD_LOW_DISK_RENOTIFY_SEC", "600"))
# Retention cap on coordd's own .turns/ logs. One log was written per driven
# turn (``_turn_log_path``) and NEVER pruned — the greatminds-owned unbounded
# growth this task identified (19k files on a long-lived fleet). Keep the most
# recent N per role; older ones are operator-irrelevant once superseded.
TURN_LOG_KEEP_PER_ROLE = int(
    os.environ.get("COORDD_TURN_LOG_KEEP_PER_ROLE", "50"))
# Failure-text markers that an ENOSPC can cause (auth refresh that could not
# be persisted, or a direct write failure). When a turn fails with one of
# these, coordd appends the live disk status to the outcome detail so an
# operator can see whether a low disk likely corrupted the auth refresh.
_AUTH_DISK_FAILURE_MARKERS = (
    "refresh_token_reused",
    "token_expired",
    "no space left",
    "enospc",
    "disk quota exceeded",
    "401",
)

# 0199: PyPI version check. Defaults match schema.auto_update.
# Operator can override per-project via env (test/dev convenience).
AUTO_UPDATE_CHECK_INTERVAL_DEFAULT = 14400.0   # 4h
AUTO_UPDATE_PYPI_URL = "https://pypi.org/pypi/greatminds/json"
AUTO_UPDATE_FETCH_TIMEOUT = 10.0   # network call; never block the loop


# 0169: queue directories whose new-file events should wake coordd's
# main loop. Inbox is the primary one; active claim queues are added
# because a task landing in feature_dev/feature_test/etc. needs the
# coordd cycle to fire notify_from_journal which then writes wake-*.md
# into the right inbox.
INOTIFY_QUEUE_DIRS: tuple[str, ...] = (
    "inbox",
    "feature_inbox",
    "feature_plan",
    "feature_dev",
    "feature_ui_dev",
    "feature_docs",
    "feature_test",
    "feature_docs_review",
    "feature_review",
    "feature_blocked",
    "verified",
    # 0258 / 0247 (1.3.0 BREAKING): stand_requests / stand_wip /
    # stand_done queues REMOVED — the lease-based singleton stand
    # resource at ``.stand/state.yaml`` watches its own state via the
    # ``.stand`` subdir below (added by 0245).
    "review_sessions",
    # 0245 (0242c / Phase 3 of 0242): SK polls the singleton stand
    # resource state via inotify on ``coordination/.stand/`` so
    # state transitions (lease grants, releases, down events) wake
    # the daemon sub-second instead of waiting for the next poll
    # tick.
    ".stand",
)


class _InotifyWatcher:
    """0169: thin wrapper around inotify_simple that exposes a single
    ``read_or_timeout(timeout_s)`` method.

    The watcher adds non-recursive watches on each of
    ``coord/<queue>/`` directories listed in ``INOTIFY_QUEUE_DIRS``,
    plus one level deeper for ``coord/inbox/<role>/`` (because new
    inbox files land in per-role subdirs). Watch flags target CREATE,
    MOVED_TO (atomic mv from intent staging), and CLOSE_WRITE (the
    end of a normal file write). Reading drains pending events; the
    main loop only cares 'did anything change since last tick',
    not the per-event details.
    """

    def __init__(self, coord: Path, verbose: bool = False):
        from inotify_simple import INotify, flags
        self._inotify = INotify()
        self._flags = flags.CREATE | flags.MOVED_TO | flags.CLOSE_WRITE
        self._verbose = verbose
        # 0204: map watch-descriptor → queue-name so the main loop can
        # tell which queue an event came from (the event itself only
        # carries the watched dir's wd + the filename).
        self._wd_to_queue: dict[int, str] = {}
        self._add_initial_watches(coord)

    def _add_initial_watches(self, coord: Path) -> None:
        for sub in INOTIFY_QUEUE_DIRS:
            d = coord / sub
            if not d.is_dir():
                # 0341: the singleton stand's ``.stand/`` dir is created
                # lazily by the first ``stand lease`` (update_stand_state
                # mkdir). If coordd starts BEFORE any lease (fresh deploy
                # / restart), this watch would never attach — and because
                # the direct queue→owner route (``_route_queue_event``)
                # fires on inotify events ONLY (the poll fallback yields
                # no events), NO stand lifecycle change would ever wake
                # STAND-KEEPER. SK then has to be nudged by hand. Create
                # the dir up front so the watch always attaches and the
                # first ``state.yaml`` write (lease grant / preparing)
                # routes to SK like any other queue→owner event.
                if sub == ".stand":
                    try:
                        d.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        continue
                else:
                    continue
            try:
                wd = self._inotify.add_watch(str(d), self._flags)
                self._wd_to_queue[wd] = sub
            except OSError:
                # Watch budget exhausted, dir gone mid-init, etc.
                # Soft-degrade: skip this dir; polling still covers it.
                pass
            # Inbox has per-role subdirs that new files land in. Add
            # one level deeper for each existing role subdir.
            if sub == "inbox":
                for role_dir in d.iterdir():
                    if not role_dir.is_dir():
                        continue
                    try:
                        wd = self._inotify.add_watch(
                            str(role_dir), self._flags,
                        )
                        # Inbox subdirs are inbox-side per-role; tag
                        # them as ``inbox`` so the dispatcher knows
                        # to route via the inbox-scan path (not the
                        # 0204 queue-owner routing).
                        self._wd_to_queue[wd] = "inbox"
                    except OSError:
                        pass

    def queue_for(self, wd: int) -> str | None:
        """0204: which queue (or ``inbox``) does this watch-descriptor
        belong to. Used by the main loop to dispatch queue events to
        owning roles via schema.queues lookup."""
        return self._wd_to_queue.get(wd)

    def read_or_timeout(self, timeout_s: float) -> list:
        """Block up to ``timeout_s`` seconds. Return the (possibly
        empty) list of events. Returning early on event delivery is
        the latency win — the main loop's next iteration runs as soon
        as something interesting happens."""
        timeout_ms = max(0, int(timeout_s * 1000))
        try:
            return self._inotify.read(timeout=timeout_ms)
        except OSError:
            # Inotify FD closed under us; degrade to a plain sleep.
            time.sleep(timeout_s)
            return []


def _make_inotify_watcher(coord: Path, verbose: bool):
    """0169: optional inotify watcher. Returns None when the dep is
    missing (non-Linux) or when watch-add fails for every candidate
    dir — caller falls back to the plain polling path."""
    try:
        watcher = _InotifyWatcher(coord, verbose=verbose)
    except (ImportError, OSError) as exc:
        if verbose:
            print(
                f"coordd: inotify watcher unavailable ({exc!r}); "
                f"falling back to polling-only.",
                file=sys.stderr,
            )
        return None
    if verbose:
        print("coordd: inotify watcher armed", file=sys.stderr)
    return watcher


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


def _owning_role_for_queue(canon_dir: Path, queue: str) -> str | None:
    """0204: resolve the owner role for a queue per schema.

    Reads ``schema.queues[<queue>].owner`` — the role that owns the
    queue's content and claims from it. Returns the owner's role name
    in upper-case (matching $GREATMINDS_ROLE conventions) or None
    when the queue isn't in schema (test fixtures, future queues
    without canon entries).
    """
    try:
        schema = load_schema_roles.__globals__.get("yaml")
        if schema is None:
            import yaml as schema
        doc = schema.safe_load(
            (canon_dir / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except Exception:
        return None
    q = (doc.get("queues") or {}).get(queue) or {}
    owner = q.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    return owner.strip().upper()


def _lifecycle_for_role(canon_dir: Path, role: str) -> str | None:
    """0315 (0311 Phase 2a): read ``schema.roles.<ROLE>.lifecycle``
    (interactive / self-loop / driven). Returns None when the role
    or field is absent — caller treats that as the pre-2a wake-only
    behavior."""
    roles = load_schema_roles(canon_dir)
    entry = roles.get(role) or roles.get(role.upper()) or {}
    if not isinstance(entry, dict):
        return None
    lc = entry.get("lifecycle")
    return lc.strip() if isinstance(lc, str) and lc.strip() else None


def _driven_bootstrap_path(coord: Path, role_lower: str) -> str:
    """Path to the single static system-prompt file
    ``coordination/bootstrap.md`` (seeded from canon by ``setup``).

    Role-independent: the prompt is the same for every role, which reads
    its own contract from ``schema.roles.<GREATMINDS_ROLE>`` (coordd sets
    GREATMINDS_ROLE in the turn's env). Passed to claude's
    ``--append-system-prompt-file`` / used as codex ``baseInstructions``
    when it exists; the caller gates on ``Path(...).is_file()``. The
    ``role_lower`` arg is retained for call-site symmetry."""
    return str(coord / "bootstrap.md")


def _driven_run_lock_path(coord: Path, role_lower: str) -> Path:
    """Per-role run-lock marker: ``<coord>/.locks/driven-<role>.lock``.
    Presence means a turn is currently running for that role. The
    lock is held for the FULL turn duration (the turn runs as a
    coordd-managed subprocess in a daemon thread), so its presence +
    mtime drive both per-role serialization and hang detection."""
    return coord / ".locks" / f"driven-{role_lower}.lock"


def _write_driven_run_lock(
    lock: Path,
    role_lower: str,
    *,
    driver: str,
    log_path: str | None = None,
    thread_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Write operator-visible metadata into the driven run-lock.

    Older locks were empty files, so the dashboard/watchdog could only infer
    "running" from mtime. Keep the same path/presence contract, but make new
    locks self-describing. Best-effort: lock presence remains the invariant.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "role": role_lower.upper(),
        "driver": driver,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at_epoch": time.time(),
        "coordd_pid": os.getpid(),
    }
    if log_path:
        meta["log_path"] = log_path
    if thread_id:
        meta["thread_id"] = thread_id
    if session_id:
        meta["session_id"] = session_id
    try:
        lock.write_text(json.dumps(meta, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError:
        lock.touch()


def _turn_log_path(coord: Path, role_lower: str) -> Path:
    """Per-turn output log: ``<coord>/.turns/<role>-<ISO>.log``.

    Driven roles have NO tmux pane (the turn runs as a coordd
    subprocess), so the captured stdout/stderr of each turn is the
    operator-visible record of what the agent did."""
    d = coord / ".turns"
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return d / f"{role_lower}-{ts}.log"


def _driven_pending_path(coord: Path, role_lower: str) -> Path:
    """Per-role pending marker. Set when an event arrives mid-turn;
    the post-turn cleanup re-spawns once if present."""
    return coord / ".locks" / f"driven-{role_lower}.pending"


def _driven_retry_path(coord: Path, role_lower: str) -> Path:
    """Operator-visible retry/backoff state for a driven role.

    The scheduler's live timing uses monotonic time in ``_DRIVEN_RETRY``.
    Dashboard/watchdog and coordd restarts need wall-clock state, so each
    scheduled retry is mirrored here as JSON.
    """
    return coord / ".locks" / f"driven-{role_lower}.retry.json"


def _remove_driven_retry_status(coord: Path, role_lower: str) -> None:
    try:
        _driven_retry_path(coord, role_lower).unlink()
    except OSError:
        pass


def _write_driven_retry_status(
    coord: Path,
    role_lower: str,
    state: dict,
    detail: str,
) -> None:
    path = _driven_retry_path(coord, role_lower)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "role": role_lower.upper(),
        "klass": state.get("klass"),
        "attempts": int(state.get("attempts") or 0),
        "escalated": bool(state.get("escalated")),
        "notified": bool(state.get("notified")),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at_epoch": time.time(),
        "next_at_epoch": state.get("next_at_epoch") or 0.0,
        "detail": (detail or "")[:500],
    }
    try:
        path.write_text(json.dumps(payload, sort_keys=True) + "\n",
                        encoding="utf-8")
    except OSError:
        pass


def _read_driven_retry_status(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_persisted_retry_states(coord: Path) -> None:
    """Restore due/backoff retry state after coordd restart.

    Escalated rows stay on disk for visibility but are not inserted back into
    the live scheduler, because auto-retry intentionally stopped.
    """
    locks = coord / ".locks"
    if not locks.is_dir():
        return
    now_epoch = time.time()
    now_mono = time.monotonic()
    with _RETRY_LOCK:
        for path in sorted(locks.glob("driven-*.retry.json")):
            role_lower = path.name[len("driven-"):-len(".retry.json")]
            if role_lower in _DRIVEN_RETRY:
                continue
            data = _read_driven_retry_status(path)
            if not data or data.get("escalated"):
                continue
            klass = data.get("klass")
            if klass not in {"rate_limit", "error", "timeout"}:
                continue
            attempts = int(data.get("attempts") or 0)
            next_at_epoch = float(data.get("next_at_epoch") or now_epoch)
            _DRIVEN_RETRY[role_lower] = {
                "attempts": attempts,
                "klass": klass,
                "next_at": now_mono + max(0.0, next_at_epoch - now_epoch),
                "next_at_epoch": next_at_epoch,
                "escalated": False,
                "notified": bool(data.get("notified")),
            }


def _build_driven_claude_argv(
    session_id: str,
    bootstrap_file: str | None,
    prompt: str = "continue your tick",
    *,
    fresh: bool = False,
) -> list[str]:
    """0315: construct the ``claude --resume <sid> -p`` argv that
    runs one driven turn. 0316 supplies the
    ``--append-system-prompt-file`` so the role contract rides the
    system prompt on every fresh ``-p`` invocation; pre-0316 the
    bootstrap_file is None and the contract relies on --resume
    history.

    0317: ``fresh=True`` starts a NEW session (no ``--resume``) when
    the session-reset threshold trips — claude mints a fresh
    session-id, the caller records it. The bootstrap (system prompt)
    carries the full contract so the new session isn't context-blind.
    """
    # 1.6.2: bare `claude` — the daemon unit bakes the operator's PATH
    # (Environment=PATH), so claude resolves via PATH like in a shell. No
    # in-code path resolver.
    if fresh:
        argv = ["claude", "-p", prompt]
    else:
        argv = ["claude", "--resume", session_id, "-p", prompt]
    # 0311 driven fix: a headless ``claude -p`` turn that uses ANY tool blocks
    # on MCP-server initialization before it can act. The fleet's default MCP
    # discovery includes heavy npm-exec browser servers (playwright,
    # chrome-devtools) that hang on startup, so the turn never returns and
    # freezes coordd's run-lock — the driven "hang". Driven pipeline roles
    # need NO MCP (they work via Read/Edit/Bash + the ``greatminds`` CLI), so
    # suppress all MCP discovery with --strict-mcp-config (no --mcp-config flag
    # ⇒ zero MCP servers spawned). Built-in tools (Bash/Read/Edit) still work.
    argv.append("--strict-mcp-config")
    # 0311 driven fix (root cause of the hang): without an explicit permission
    # mode a headless ``claude -p`` turn runs in ``default`` mode, so every
    # tool call (Bash / the ``greatminds`` CLI) requires interactive approval.
    # In headless mode there is no approver, so the turn BLOCKS forever on the
    # first gated tool call — the driven "hang". Interactive agents already use
    # ``--permission-mode auto`` (start_agent.py); driven turns must too so the
    # agent can run its tools unattended.
    argv.extend(["--permission-mode", "auto"])
    if bootstrap_file:
        argv.extend(["--append-system-prompt-file", bootstrap_file])
    return argv



def _driven_subprocess_env(role_lower: str) -> dict[str, str]:
    """Env for a driven-turn subprocess: the daemon's env (which carries
    the operator PATH baked into the unit) plus ``GREATMINDS_ROLE`` so the
    agent's ``greatminds`` CLI resolves the right inbox/queues. 1.6.2: no
    PATH munging — the unit's Environment=PATH already has the tools.

    0369 (GitHub #18): also pin claude's fail-fast knobs — ``API_TIMEOUT_MS``
    and ``CLAUDE_CODE_MAX_RETRIES`` — so a rate-limited/overloaded turn returns
    in ~1–2 min instead of retrying in-process for many minutes while holding
    the run-lock. coordd's own retry scheduler then handles the backoff. These
    win over any inherited values on purpose (driven turns must fail fast); an
    operator widens them via the COORDD_DRIVEN_* overrides on the daemon."""
    return {
        **os.environ,
        "GREATMINDS_ROLE": role_lower.upper(),
        "API_TIMEOUT_MS": DRIVEN_API_TIMEOUT_MS,
        "CLAUDE_CODE_MAX_RETRIES": DRIVEN_MAX_RETRIES,
    }


# 0317: session-reset policy. ``claude --resume`` accumulates
# history across driven turns; past a threshold the context gets
# expensive + noisy. The driver tracks a per-role turn count in
# the registry and starts a fresh session (no --resume) once the
# count crosses ``SESSION_RESET_TURN_THRESHOLD``. Configurable via
# the env override; default 50.
SESSION_RESET_TURN_THRESHOLD = int(
    os.environ.get("COORDD_SESSION_RESET_TURNS", "50")
)


def _driven_turn_count(reg: dict | None) -> int:
    """Current driven-turn count for the role (0 when absent)."""
    if not reg:
        return 0
    try:
        return int(reg.get("driven_turn_count") or 0)
    except (TypeError, ValueError):
        return 0


def _should_reset_session(reg: dict | None,
                          threshold: int = SESSION_RESET_TURN_THRESHOLD
                          ) -> bool:
    """0317: True when the role's accumulated turn count has reached
    the reset threshold — the next turn should start fresh."""
    return _driven_turn_count(reg) >= threshold


def _record_driven_turn(registry_dir: Path, role_lower: str,
                        *, reset: bool,
                        new_session_id: str | None = None) -> None:
    """0317: update the role's registry after a driven turn.

    - ``reset=False`` → increment ``driven_turn_count``.
    - ``reset=True``  → set ``driven_turn_count = 1`` (this turn is
      the first of the new session) and, when ``new_session_id`` is
      given, write it to ``session_id``.

    Best-effort: a missing / unreadable registry is left alone (the
    driver tolerates count starting from 0 again)."""
    f = registry_dir / f"{role_lower}.json"
    try:
        reg = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(reg, dict):
        return
    if reset:
        reg["driven_turn_count"] = 1
        if new_session_id:
            reg["session_id"] = new_session_id
    else:
        reg["driven_turn_count"] = _driven_turn_count(reg) + 1
    try:
        f.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---- driven-turn outcome classification + retry scheduling (1.6.2) -------
#
# Driven roles have no persistent process: coordd runs each turn once and
# the event chain (a moved task → inotify → next role) sustains the
# pipeline. A turn that FAILS (rate-limit / crash / timeout) moves no
# task, so no event fires and the role would freeze forever. These helpers
# classify each finished turn and re-dispatch failed ones with backoff;
# rate-limit retries ~forever, other errors escalate to MAINTAINER.

_RETRY_LOCK = threading.Lock()
# role_lower -> {attempts:int, klass:str, next_at:float (monotonic),
#                escalated:bool, notified:bool}
_DRIVEN_RETRY: dict[str, dict] = {}


def _classify_turn_outcome(rc, stdout, *, timed_out: bool = False) -> str:
    """Classify a finished driven turn: ``ok`` | ``rate_limit`` |
    ``auth`` | ``error`` | ``timeout``. claude emits a JSON result object
    (``--output-format json``) carrying ``is_error`` / ``api_error_status``;
    a non-zero rc with no parseable success JSON is a hard error. When
    unsure we return ``error`` (bounded retry + escalate) — never a silent
    ``ok`` (would drop the work) nor ``rate_limit`` (would retry forever)."""
    if timed_out:
        return "timeout"
    obj = None
    if stdout:
        try:
            obj = json.loads(stdout)
        except (ValueError, TypeError):
            obj = None
    if isinstance(obj, dict) and obj.get("is_error"):
        status = obj.get("api_error_status")
        blob = f"{obj.get('result', '')} {status}".lower()
        if status in (401, 403) or "invalid authentication credentials" in blob \
                or ("authentication" in blob and "failed" in blob) \
                or "not logged in" in blob or "unauthorized" in blob:
            return "auth"
        if status in (429, 529) or "rate limit" in blob \
                or "rate-limit" in blob or "overloaded" in blob \
                or "temporarily limiting" in blob:
            return "rate_limit"
        return "error"
    if rc not in (0, None):
        return "error"
    return "ok"


def _retry_delay(klass: str, attempts: int) -> float:
    base, cap = ((RETRY_RL_BASE_SEC, RETRY_RL_CAP_SEC)
                 if klass == "rate_limit"
                 else (RETRY_HARD_BASE_SEC, RETRY_HARD_CAP_SEC))
    return min(cap, base * (2 ** max(0, attempts - 1)))


def _escalate_to_maintainer(coord: Path, role_lower: str, klass: str,
                            attempts: int, detail: str) -> None:
    """Best-effort inbox-info to MAINTAINER (shells out so the journal +
    heartbeat side-effects fire through the normal CLI path)."""
    body = (f"driven {role_lower.upper()} turn failed {attempts}x "
            f"({klass}); auto-retry stopped — investigate. "
            f"detail: {detail[:300]}")
    try:
        subprocess.run(
            [sys.executable, "-m", "greatminds.cli.main",
             "inbox", "send", "MAINTAINER", "--kind", "info",
             "--body", body],
            cwd=str(coord.parent),
            env={**os.environ, "GREATMINDS_ROLE": "MAINTAINER"},
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — notification is best-effort
        pass
    # Wake MAINTAINER so it acts on the escalation now, not on its next
    # self-loop wake (up to an hour away) — but ONLY if it is idle.
    _wake_maintainer_if_asleep(coord)


def _wake_maintainer_if_asleep(coord: Path, verbose: bool = False) -> None:
    """Nudge MAINTAINER's input_sock so it picks up a just-filed escalation
    immediately — but ONLY when MAINTAINER is idle (heartbeat stale). A
    MAINTAINER mid-turn is left alone: we never interrupt active work (and
    don't burn wake diagnosability). MAINTAINER is in
    NO_KEYSTROKE_INJECT_ROLES (coordd's normal driven-wake never touches it),
    so this is the one deliberate, freshness-gated exception for escalation."""
    hb = coord / "heartbeat.maintainer"
    try:
        if hb.is_file() and (
                time.time() - hb.stat().st_mtime) < PUSH_FRESH_GUARD_SEC:
            return  # MAINTAINER is active — do not disturb
    except OSError:
        pass
    try:
        push_to_role(coord, "MAINTAINER", "escalation", verbose,
                     bypass_fresh_guard=True)
    except Exception:  # noqa: BLE001 — best-effort wake
        pass


def _note_turn_outcome(coord: Path, role_lower: str, klass: str,
                       detail: str, verbose: bool) -> dict | None:
    """Update per-role retry state from a finished turn. ``ok`` clears the
    state. ``rate_limit`` schedules a backoff retry (and notifies once if
    it persists). ``error``/``timeout`` schedules a bounded retry, then
    escalates + stops. ``auth`` escalates immediately because no retry can
    fix an unusable external-tool session. Returns a snapshot of the state
    (or None on ok)."""
    now = time.monotonic()
    with _RETRY_LOCK:
        if klass == "ok":
            _DRIVEN_RETRY.pop(role_lower, None)
            _remove_driven_retry_status(coord, role_lower)
            return None
        st = _DRIVEN_RETRY.get(role_lower)
        if st is None or st.get("klass") != klass:
            st = {"attempts": 0, "klass": klass, "next_at": 0.0,
                  "next_at_epoch": 0.0, "escalated": False,
                  "notified": False}
            _DRIVEN_RETRY[role_lower] = st
        st["attempts"] += 1
        st["next_at_epoch"] = 0.0
        if klass == "auth":
            st["escalated"] = True
            st["next_at"] = 0.0
            st["next_at_epoch"] = 0.0
        elif klass == "rate_limit":
            delay = _retry_delay(klass, st["attempts"])
            st["next_at"] = now + delay
            st["next_at_epoch"] = time.time() + delay
            do_notify = (st["attempts"] == RETRY_RL_NOTIFY_AT
                         and not st["notified"])
            if do_notify:
                st["notified"] = True
        else:
            if st["attempts"] >= RETRY_HARD_MAX:
                st["escalated"] = True
            else:
                delay = _retry_delay(klass, st["attempts"])
                st["next_at"] = now + delay
                st["next_at_epoch"] = time.time() + delay
        snap = dict(st)
    _write_driven_retry_status(coord, role_lower, snap, detail)
    if klass != "rate_limit" and snap.get("escalated"):
        _escalate_to_maintainer(coord, role_lower, klass,
                                snap["attempts"], detail)
    elif klass == "rate_limit" and snap.get("notified"):
        _escalate_to_maintainer(
            coord, role_lower, klass, snap["attempts"],
            f"{detail} — still rate-limited, continuing to retry")
    if verbose:
        print(f"  retry: {role_lower} {klass} attempt {snap['attempts']}"
              f"{' → ESCALATED (auto-retry stopped)' if snap.get('escalated') else ''}",
              file=sys.stderr)
    return snap


def _clear_retry_state(role_lower: str, coord: Path | None = None) -> None:
    """A real event (queue move / inbox) drove the role — drop any
    backoff/escalation so it gets a fresh chance."""
    with _RETRY_LOCK:
        _DRIVEN_RETRY.pop(role_lower, None)
    if coord is not None:
        _remove_driven_retry_status(coord, role_lower)


def _process_due_retries(coord: Path, canon_dir: Path, verbose: bool) -> None:
    """Main-loop step: re-dispatch driven roles whose retry backoff is due.
    Targeted (only roles whose last turn failed), not a blanket sweep;
    escalated roles are skipped until a real event clears them."""
    _load_persisted_retry_states(coord)
    now = time.monotonic()
    due: list[str] = []
    with _RETRY_LOCK:
        for role_lower, st in _DRIVEN_RETRY.items():
            if st.get("escalated"):
                continue
            if st.get("next_at") and now >= st["next_at"]:
                st["next_at"] = 0.0  # consumed; the turn outcome reschedules
                due.append(role_lower)
    if not due:
        return
    coord_yaml_doc = _read_coord_yaml(coord.parent)
    for role_lower in due:
        if _driven_run_lock_path(coord, role_lower).is_file():
            continue  # a turn is already running
        role_upper = role_lower.upper()
        located = _window_and_tool_for_role(coord_yaml_doc, role_upper)
        try:
            _maybe_drive_driven_role(
                coord, canon_dir, coord_yaml_doc, located, role_upper,
                verbose, trigger=" (retry)",
            )
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"coordd: retry dispatch for {role_upper} failed: "
                      f"{exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 0376: low-disk / ENOSPC guards.
# ---------------------------------------------------------------------------
# Dedup low-disk escalations across roles (the disk is shared). Guarded by
# _RETRY_LOCK (reused; both are coordd-thread-local concerns).
_LOW_DISK_LAST_NOTIFY = {"at": 0.0}


def _disk_free(path: Path) -> tuple[int, float]:
    """(free_bytes, free_pct) for the filesystem holding ``path``. On any
    OSError returns (-1, 100.0) — a sentinel that ALWAYS passes the preflight
    (never block a turn because we couldn't stat the disk: fail open)."""
    try:
        usage = shutil.disk_usage(str(path))
        pct = (usage.free / usage.total * 100.0) if usage.total else 100.0
        return usage.free, pct
    except OSError:
        return -1, 100.0


def _disk_status_str(path: Path) -> str:
    free, pct = _disk_free(path)
    if free < 0:
        return "disk: unknown (statvfs failed)"
    return f"disk: free={free // (1024 * 1024)}MB ({pct:.1f}%)"


def _disk_preflight(path: Path) -> tuple[bool, str]:
    """(ok, diagnostic). ok=False when the filesystem holding ``path`` is
    below the absolute-MB OR the percent-free threshold — spawning a turn
    then risks an ENOSPC that corrupts the auth refresh. The diagnostic
    names ENOSPC / low-disk explicitly and is the operator-facing blocker."""
    free, pct = _disk_free(path)
    if free < 0:
        return True, ""  # couldn't stat — fail open, never block on unknowns
    free_mb = free / (1024 * 1024)
    if free_mb < DISK_MIN_FREE_MB or pct < DISK_MIN_FREE_PCT:
        return (False,
                f"LOW DISK / ENOSPC risk: free={free_mb:.0f}MB ({pct:.1f}%) "
                f"on {path} is below threshold "
                f"({DISK_MIN_FREE_MB:.0f}MB / {DISK_MIN_FREE_PCT:.1f}%). "
                f"Driven turn NOT spawned — a refreshed Codex/Claude auth "
                f"token may fail to persist (refresh_token_reused / "
                f"token_expired). Free disk space, then re-drive.")
    return True, ""


def _enrich_failure_detail(detail: str, coord: Path) -> str:
    """When a turn-failure detail matches an auth/persistence marker that an
    ENOSPC can cause, append the live disk status (and a likely-cause hint
    when the disk is actually below threshold) so operators can tell an
    auth bug apart from disk-induced auth corruption. No-op otherwise."""
    low = (detail or "").lower()
    if not any(m in low for m in _AUTH_DISK_FAILURE_MARKERS):
        return detail
    free, pct = _disk_free(coord)
    status = _disk_status_str(coord)
    hint = ""
    if free >= 0:
        free_mb = free / (1024 * 1024)
        if free_mb < DISK_MIN_FREE_MB or pct < DISK_MIN_FREE_PCT:
            hint = (" — LIKELY ENOSPC-CAUSED AUTH CORRUPTION (disk below "
                    "threshold; a refreshed token may not have persisted)")
    return f"{detail} [{status}{hint}]"


def _note_low_disk_blocker(coord: Path, role_lower: str, diag: str,
                           verbose: bool) -> None:
    """Record + escalate a low-disk preflight block. Writes a turn-log record
    (the operator-visible turn artifact, so a blocked role isn't silent) and
    files ONE inbox-info to MAINTAINER per LOW_DISK_RENOTIFY_SEC window — a
    storm of queue events must surface the blocker, not spam the inbox."""
    try:
        _turn_log_path(coord, role_lower).write_text(
            f"=== DRIVEN TURN BLOCKED (low disk / ENOSPC) ===\n{diag}\n",
            encoding="utf-8")
    except OSError:
        pass
    _write_driven_retry_status(coord, role_lower, {
        "klass": "low_disk",
        "attempts": 0,
        "next_at_epoch": 0.0,
        "escalated": True,
        "notified": False,
    }, diag)
    now = time.monotonic()
    with _RETRY_LOCK:
        due = (now - _LOW_DISK_LAST_NOTIFY["at"]) >= LOW_DISK_RENOTIFY_SEC
        if due:
            _LOW_DISK_LAST_NOTIFY["at"] = now
    if verbose:
        print(f"  0376: driven turn for {role_lower} BLOCKED — {diag}",
              file=sys.stderr)
    if not due:
        return
    body = (f"DRIVEN TURNS BLOCKED — low disk / ENOSPC. {diag} (first "
            f"blocked role: {role_lower.upper()}). coordd is refusing to "
            f"spawn driven turns until free space recovers.")
    try:
        subprocess.run(
            [sys.executable, "-m", "greatminds.cli.main",
             "inbox", "send", "MAINTAINER", "--kind", "info",
             "--body", body],
            cwd=str(coord.parent),
            env={**os.environ, "GREATMINDS_ROLE": "MAINTAINER"},
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001 — escalation is best-effort
        pass
    _wake_maintainer_if_asleep(coord)


def _prune_turn_logs(coord: Path, keep_per_role: int = TURN_LOG_KEEP_PER_ROLE,
                     verbose: bool = False) -> int:
    """Cap coordd's own .turns/ growth: keep the most recent ``keep_per_role``
    log files per role, delete older ones. One log is written per driven turn
    and was never pruned (greatminds-owned unbounded growth identified by
    0376). Returns the number of files removed. Best-effort; never raises."""
    d = coord / ".turns"
    if not d.is_dir():
        return 0
    by_role: dict[str, list[Path]] = {}
    try:
        for f in d.glob("*.log"):
            stem = f.name[:-4]  # strip ".log"
            # filename shape: <role>-<ISO-timestamp>.log; role may itself
            # contain hyphens, so split off only the trailing timestamp token.
            role = stem.rsplit("-", 1)[0] if "-" in stem else stem
            by_role.setdefault(role, []).append(f)
    except OSError:
        return 0
    removed = 0
    for files in by_role.values():
        if len(files) <= keep_per_role:
            continue
        # ISO timestamps sort lexicographically → oldest first.
        files.sort(key=lambda p: p.name)
        for f in files[:-keep_per_role]:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if verbose and removed:
        print(f"coordd: pruned {removed} old .turns/ log(s)", file=sys.stderr)
    return removed


def _finalize_driven_turn(
    coord: Path, role_lower: str, lock: Path, klass: str, detail: str,
    verbose: bool, *,
    record: "callable | None" = None,
    refire: "callable | None" = None,
) -> None:
    """0391: release the driven run-lock and run all post-turn bookkeeping
    so NO single failure can (a) leave ``driven-<role>.lock`` behind for the
    next dispatch / hang-sweep to mistake as a live turn, or (b) skip the
    retry/outcome note that keeps a failed turn's backlog recoverable.

    Every driven-turn exit path funnels through here: clean success, a
    SIGTERM/rc=143 kill, ``subprocess.TimeoutExpired``, and an exception
    raised while recording the outcome. The observed 0391 strand was a
    DEVELOPER turn that timed out / was SIGTERM-killed yet left the run-lock
    behind with no live subprocess, so the backlog was hidden forever.

    Step order + INDEPENDENT guards (a raise in one step must not skip the
    rest — earlier code chained these unguarded, so a registry write failing
    on a full disk skipped ``_note_turn_outcome`` and the retry was never
    scheduled):
      1. unlink the run-lock FIRST + unconditionally (issue #11 + 0391).
      2. ``record`` (registry turn record; claude path only) — guarded.
      3. enrich the failure detail — guarded, so a raise here cannot skip…
      4. …``_note_turn_outcome`` (schedules retry/backoff or escalates) —
         guarded; this is the recoverability-critical step.
      5. pending re-fire ONLY on a clean turn (``klass == 'ok'``); a failed
         turn is re-dispatched by the retry scheduler (no double-spawn).
    """
    # 1. Lock first, unconditionally. A turn that exited (however it exited)
    #    must never leave the lock for the next dispatch to read as live.
    try:
        lock.unlink()
    except OSError:
        pass
    # 2. Registry turn record (claude path supplies this; codex records its
    #    thread inside the turn). Guarded — a registry write failure must not
    #    skip the retry note below.
    if record is not None:
        try:
            record()
        except Exception as exc:  # noqa: BLE001 — bookkeeping, never crash
            if verbose:
                print(f"  0391: {role_lower} turn-record bookkeeping failed: "
                      f"{exc}", file=sys.stderr)
    # 3. Enrich the failure detail — guarded so a raise can't skip the note.
    try:
        detail = _enrich_failure_detail(detail, coord)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  0391: {role_lower} failure-detail enrich failed: "
                  f"{exc}", file=sys.stderr)
    # 4. Outcome / retry scheduling — the step that keeps a failed turn
    #    recoverable. Guarded so even an unexpected raise here is contained.
    try:
        _note_turn_outcome(coord, role_lower, klass, detail, verbose)
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  0391: {role_lower} outcome/retry note failed: "
                  f"{exc}", file=sys.stderr)
    # 5. Pending re-fire only on a clean turn.
    pend = _driven_pending_path(coord, role_lower)
    if pend.exists():
        try:
            pend.unlink()
        except OSError:
            pass
        if klass == "ok" and refire is not None:
            try:
                refire()
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"  0391: {role_lower} pending re-fire failed: "
                          f"{exc}", file=sys.stderr)


def _spawn_driven_turn(
    coord: Path,
    role_lower: str,
    session_id: str,
    pane: str | None,
    session_name: str | None,
    bootstrap_file: str | None,
    verbose: bool,
    *,
    spawn: "callable | None" = None,
    reg: dict | None = None,
    force_fresh: bool = False,
) -> tuple[bool, str]:
    """0315: run one turn for a driven claude role via
    ``claude --resume -p``. Honors a per-role run-lock: if a turn is
    already running, sets the pending marker and returns without
    spawning a second process.

    0317: applies the session-reset policy. When the role's
    ``driven_turn_count`` (from ``reg``) has reached
    ``SESSION_RESET_TURN_THRESHOLD``, this turn starts a FRESH
    session (no --resume) and the registry's count resets to 1.
    Otherwise --resume continues and the count increments.

    ``spawn`` is an injection seam for tests — a callable taking the
    argv list and returning a truthy handle. Default delivers the
    command into the role's tmux pane (the pane is idle bash
    between turns for driven roles).
    """
    lock = _driven_run_lock_path(coord, role_lower)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        # A turn is already running — mark pending so the post-turn
        # cleanup re-fires once, and do NOT spawn a second process.
        _driven_pending_path(coord, role_lower).touch()
        if verbose:
            print(
                f"  0315: driven turn for {role_lower} already "
                f"running; marked pending",
                file=sys.stderr,
            )
        return (False, "run-lock held; pending set")

    # Decide resume-vs-fresh from the accumulated turn count;
    # force_fresh (no session_id yet — first turn) also starts fresh.
    reset = force_fresh or _should_reset_session(reg)
    argv = _build_driven_claude_argv(
        session_id, bootstrap_file, fresh=reset)

    # Test seam: a synchronous spawn callable. Leave the lock held for
    # run-lock assertions, then release (no real subprocess / thread).
    if spawn is not None:
        try:
            _write_driven_run_lock(lock, role_lower, driver="claude",
                                   session_id=session_id)
            spawn(argv)
            _record_driven_turn(coord / REGISTRY_DIR, role_lower, reset=reset)
            return (True, f"driven turn spawned for {role_lower}"
                          f"{' (session reset)' if reset else ''}")
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    # Production: run ``claude -p`` as a coordd-managed subprocess in a
    # daemon thread. The run-lock is held for the FULL turn duration
    # (released on process exit) so coordd serializes per-role turns and
    # can detect a hung turn (lock held + heartbeat not advancing).
    # Driven roles have NO tmux pane — stdout/stderr is captured to
    # ``.turns/<role>-<ts>.log`` as the operator-visible turn record.
    # ``--output-format json`` makes claude emit a single result object
    # carrying ``session_id``, which we record so the next ``--resume``
    # turn continues the same session.
    run_argv = argv + ["--output-format", "json"]
    turn_log = _turn_log_path(coord, role_lower)
    _write_driven_run_lock(lock, role_lower, driver="claude",
                           log_path=str(turn_log), session_id=session_id)

    def _worker() -> None:
        new_sid: str | None = None
        klass = "error"
        detail = ""
        try:
            proc = subprocess.run(
                run_argv, cwd=str(coord.parent),
                capture_output=True, text=True,
                # coordd has no role of its own; export the driven role
                # so the agent's greatminds CLI resolves caller_role and
                # the static bootstrap's $GREATMINDS_ROLE is set (mirrors
                # the codex driver's _codex_appserver_env).
                env=_driven_subprocess_env(role_lower),
                timeout=DRIVEN_TURN_TIMEOUT_SEC,
            )
            try:
                turn_log.write_text(
                    f"$ {' '.join(run_argv)}\n\n"
                    f"=== stdout ===\n{proc.stdout}\n"
                    f"=== stderr ===\n{proc.stderr}\n"
                    f"=== rc={proc.returncode} ===\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            try:
                obj = json.loads(proc.stdout)
                new_sid = (obj.get("session_id") if isinstance(obj, dict)
                           else None) or None
            except (ValueError, TypeError, AttributeError):
                new_sid = None
            klass = _classify_turn_outcome(proc.returncode, proc.stdout)
            detail = (proc.stderr or proc.stdout or "")[:300]
        except subprocess.TimeoutExpired:
            klass = "timeout"
            detail = f"turn exceeded {DRIVEN_TURN_TIMEOUT_SEC:.0f}s"
            try:
                turn_log.write_text(
                    f"$ {' '.join(run_argv)}\n\n=== TIMEOUT after "
                    f"{DRIVEN_TURN_TIMEOUT_SEC:.0f}s ===\n", encoding="utf-8")
            except OSError:
                pass
            if verbose:
                print(f"  driven claude turn for {role_lower} timed out",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — log, never crash coordd
            klass = "error"
            detail = str(exc)[:300]
            if verbose:
                print(f"  driven claude turn for {role_lower} failed: "
                      f"{exc}", file=sys.stderr)
        finally:
            # 0391: every exit path (success, rc=143/SIGTERM, TimeoutExpired,
            # or a raise while recording the outcome) funnels through
            # _finalize_driven_turn, which releases the run-lock FIRST +
            # unconditionally (issue #11) and then runs each bookkeeping step
            # under its OWN guard — so a registry/I-O failure can no longer
            # skip _note_turn_outcome (the retry scheduling that keeps a
            # failed turn's backlog recoverable) or strand an orphan lock.
            _finalize_driven_turn(
                coord, role_lower, lock, klass, detail, verbose,
                record=lambda: _record_driven_turn(
                    coord / REGISTRY_DIR, role_lower,
                    reset=reset, new_session_id=new_sid),
                refire=lambda: _spawn_driven_turn(
                    coord, role_lower, (new_sid or session_id),
                    pane, session_name, bootstrap_file, verbose,
                    reg=read_registry(coord / REGISTRY_DIR, role_lower)),
            )

    import threading
    threading.Thread(target=_worker, daemon=True,
                     name=f"claude-turn-{role_lower}").start()
    if verbose:
        mode = "FRESH (reset)" if reset else "--resume"
        print(f"  driven claude turn dispatched ({mode}, subprocess) for "
              f"{role_lower}", file=sys.stderr)
    return (True, f"driven claude turn dispatched (async) for {role_lower}"
                  f"{' (session reset)' if reset else ''}")


# ---------------------------------------------------------------------------
# 0321 (0311 Phase 3b): codex driver via the codex app-server protocol.
#
# Symmetric to the claude ``-p`` driver (2a/0315): for a role with
# lifecycle == driven AND tool == codex AND coord.yaml window mode ==
# driven, coordd drives each turn through a FRESH ``codex app-server``
# process over STDIO (no --listen) — line-delimited JSON-RPC:
# ``initialize`` → ``thread/start`` (first turn, baseInstructions = the
# role contract, the 2b/0316 analogue of claude's
# --append-system-prompt-file) or ``thread/resume`` (subsequent) →
# ``turn/start`` ("continue your tick") → wait ``turn/completed`` —
# instead of PTY keystrokes. Both drivers spawn one process per turn.
#
# iter-3 (PLANNER transport decision after two live-GATE failures): the
# WS ``--listen unix://`` control socket (iter-1 direct, iter-2 via
# ``codex app-server proxy``) cannot be driven without a real WS client
# / the daemon's managed-standalone install; the stdio transport sheds
# that entire class of problems and matches our line-delimited framing.
# A per-role run-lock (shared with the claude path) prevents a second
# turn while one is in flight; the blocking turn runs in a daemon
# thread so coordd's event loop is not held. 0320's --listen WS unit is
# now vestigial to the driver (kept, cleanup later).
# ---------------------------------------------------------------------------


def _codex_appserver_argv(model: str | None = None) -> list[str]:
    """0321-iter3: argv for a per-turn ``codex app-server`` over STDIO
    (no ``--listen``). PLANNER transport decision: drop the WS/socket
    layer entirely and drive a fresh ``codex app-server`` per turn over
    stdin/stdout line-delimited JSON-RPC — symmetric to claude's
    ``claude -p`` per-turn spawn (both = fresh process per turn).
    Verified on the host: ``codex app-server`` stdio speaks
    ``{json}\\n`` framing (initialize → response, then notifications).

    Prefers an absolute ``<node> <codex.js>`` (codex's shebang is the
    relative ``#!/usr/bin/env node`` and coordd under systemd may lack
    node on PATH — the 0320 lesson), falling back to bare ``codex``.

    0375: ``model`` is the role's model, injected as a ``-c model=``
    override instead of via a per-role ``[profiles.<role>]`` / per-role
    CODEX_HOME. Driven Codex now runs in the SINGLE machine Codex home
    (auth lives there); role config that used to ride the per-role home
    is passed as ``-c`` overrides (model here; approval/sandbox below)
    plus the role contract in ``baseInstructions``. ``--profile`` is NOT
    used — on codex 0.135+ it only selects config inside a per-role
    CODEX_HOME, which is exactly the per-role-auth path we're removing."""
    # 0311 driven fix: codex app-server's Linux sandbox uses bubblewrap, which
    # needs user-namespace creation — unavailable under the systemd user
    # service, so the server aborts at startup ("needs access to create user
    # namespaces") and the codex driven turn never runs. Disable the sandbox
    # (sandbox_mode=danger-full-access) and auto-approve tools
    # (approval_policy=never) via -c overrides so a headless driven codex turn
    # starts and runs its tools unattended — symmetric to the claude path's
    # --permission-mode auto. Verified: initialize handshake succeeds with
    # these and fails (bubblewrap) without.
    cfg = ["-c", "sandbox_mode=danger-full-access",
           "-c", "approval_policy=never"]
    # 0375: role model as a -c override (TOML string value), so the role
    # keeps its model without a per-role CODEX_HOME / --profile.
    if model:
        cfg += ["-c", f'model="{model}"']
    # 0311 driven fix (codex never spawned under systemd): coordd's systemd
    # user-service PATH does NOT include the nvm node bin dir, so
    # ``shutil.which("codex")`` returns None and the argv fell back to a bare
    # ``codex`` → FileNotFoundError → the codex turn silently never spawned.
    # Resolve codex by PATH first, then fall back to the nvm node installs,
    # and run it with the node co-located with the codex bin (version match)
    # so a fresh systemd-spawned turn finds both binaries by absolute path.
    # Resolve codex to its REAL path (login shell handles non-standard
    # installs when the daemon's minimal PATH lacks it); run it with the
    # node co-located with the codex bin (version match) so a fresh
    # systemd-spawned turn finds both binaries by absolute path.
    # 1.6.2: plain resolution. The daemon unit bakes the operator's PATH
    # (Environment=PATH), so `codex` resolves via PATH and its
    # `#!/usr/bin/env node` shebang finds the matching node on PATH too —
    # no in-code resolver / nvm globbing / node co-location guessing.
    codex = shutil.which("codex") or "codex"
    return [codex, "app-server", *cfg]


def _machine_codex_home() -> str:
    """0375: the SINGLE machine Codex home for driven turns.

    Driven Codex roles authenticate via the ONE machine ChatGPT login,
    never per-role ``auth.json`` copies. Codex 0.137 stores AND refreshes
    the ChatGPT auth in ``$CODEX_HOME/auth.json`` with single-use refresh
    tokens; per-role copies diverge the moment one role refreshes
    (``refresh_token_reused`` / ``token_expired``), and every other
    role's driven turn then completes doing zero useful work — the shared
    root cause blocking #14/#21/#22 and the feature_review queue. The
    machine login is the only place auth is valid, and codex 0.137
    exposes no native split between auth home and session/state home, so
    the whole driven turn runs in the machine home.

    Resolution order:
      1. ``GREATMINDS_CODEX_HOME`` — explicit operator override.
      2. An inherited ``CODEX_HOME`` that is NOT a per-role
         ``coordination/.codex-home/<role>`` home (a real machine home
         the daemon was launched with).
      3. ``~/.codex`` (codex's default).

    0390: delegates to :func:`greatminds.cli.codex_auth.machine_codex_home`
    — the single shared resolver so the driven (coordd) and paned
    (start_agent) Codex paths cannot drift on auth-home selection.
    """
    return codex_auth.machine_codex_home()


def _codex_role_model(coord: Path | None, role_lower: str | None) -> str | None:
    """0375: read the role's model from its codex profile SOURCE.

    The per-role ``coordination/.codex-home/<role>`` home is retained as
    role-profile SOURCE MATERIAL ONLY (model selection) — NEVER for auth.
    We read ``model = "..."`` from the profile layer
    ``<role>.config.toml`` (the 0332 split) or the base ``config.toml``
    and inject it via a ``-c model=`` argv override, so a driven turn
    keeps its role model while authenticating against the single machine
    login. Returns ``None`` when no model is declared (codex uses its own
    default).

    0390: the per-role config read delegates to
    :func:`greatminds.cli.codex_auth.read_role_codex_model` — the single
    shared reader so driven and paned Codex stay aligned on model
    selection."""
    if not coord or not role_lower:
        return None
    home = coord / ".codex-home" / role_lower
    return codex_auth.read_role_codex_model(home, role_lower)


def _codex_appserver_env(role_lower: str | None = None,
                         coord: Path | None = None) -> dict:
    """Environment for the per-turn ``codex app-server`` (0375 rewrite).

    ``CODEX_HOME`` points at the SINGLE machine Codex home (see
    :func:`_machine_codex_home`), NOT a per-role
    ``coordination/.codex-home/<role>`` home. WHY (0375): codex 0.137
    refreshes the ChatGPT auth in ``$CODEX_HOME/auth.json`` with
    single-use tokens, so per-role auth copies diverge after the first
    refresh and driven turns fail with ``refresh_token_reused`` /
    ``token_expired`` — zero-work completions that blocked #14/#21/#22.
    The machine login is the only valid auth.

    Role-specific behavior is preserved WITHOUT a per-role auth home:

    * the role contract / bootstrap rides in ``baseInstructions``
      (``thread/start``), unchanged;
    * the role model is injected via a ``-c model=`` override (see
      :func:`_codex_role_model` / :func:`_codex_appserver_argv`);
    * ``approval_policy`` / ``sandbox_mode`` are already ``-c`` overrides.

    ``GREATMINDS_ROLE`` is set explicitly (coordd has no role of its own)
    so the agent's ``greatminds`` CLI resolves the right inbox/queues.

    ``coord`` is retained for signature compatibility (callers still pass
    it) but is no longer used to build a per-role CODEX_HOME. PATH is NOT
    touched (1.6.2): the daemon unit bakes the operator's PATH.
    """
    env = dict(os.environ)
    if role_lower:
        env["GREATMINDS_ROLE"] = role_lower.upper()
    # 0375: single machine Codex login (auth.json lives here), never a
    # per-role auth copy.
    env["CODEX_HOME"] = _machine_codex_home()
    return env


def _build_thread_start_request(req_id: int,
                                base_instructions: str | None,
                                cwd: str | None) -> dict:
    """app-server ``thread/start`` JSON-RPC request. ``baseInstructions``
    carries the role contract; ``cwd`` roots the thread at the project."""
    params: dict = {}
    if base_instructions:
        params["baseInstructions"] = base_instructions
    if cwd:
        params["cwd"] = cwd
    return {"jsonrpc": "2.0", "id": req_id,
            "method": "thread/start", "params": params}


def _build_turn_start_request(req_id: int, thread_id: str,
                              prompt: str = "continue your tick") -> dict:
    """app-server ``turn/start`` JSON-RPC request. ``input`` is the
    UserInput array — a single ``text`` item is the driven nudge."""
    return {"jsonrpc": "2.0", "id": req_id, "method": "turn/start",
            "params": {"threadId": thread_id,
                       "input": [{"type": "text", "text": prompt}]}}


def _build_initialize_request(req_id: int) -> dict:
    """app-server ``initialize`` handshake (required before thread/turn
    methods over stdio). ``clientInfo`` is mandatory per the schema."""
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {"clientInfo": {
                "name": "greatminds-coordd", "title": "greatminds",
                "version": "0"}}}


def _build_thread_resume_request(req_id: int, thread_id: str) -> dict:
    """app-server ``thread/resume`` — re-attach a persisted thread on a
    subsequent turn (the per-turn stdio process is fresh, but the thread
    state is persisted by codex, so we resume by id)."""
    return {"jsonrpc": "2.0", "id": req_id, "method": "thread/resume",
            "params": {"threadId": thread_id}}


# 0375: auth-failure signatures. A driven Codex turn that fails auth must
# NOT be reported as a zero-work "ok" completion (the #14/#21/#22 bug);
# detect these substrings anywhere in the app-server stream and surface a
# failure so coordd escalates to MAINTAINER (recovery = ``codex login`` on
# the machine $HOME/.codex).
_CODEX_AUTH_SIGNATURES = (
    "refresh_token_reused",
    "token_expired",
    "no codex credentials",
    "not logged in",
    "please run codex login",
    "please run `codex login`",
    "401 unauthorized",
    "unauthorized",
)


class _CodexAuthError(OSError):
    """0375: a driven codex turn failed on a Codex auth problem
    (single-use refresh-token reuse / expiry / missing credentials).
    Recovery is a MAINTAINER ``codex login`` on the machine
    ``$HOME/.codex`` — so the turn is surfaced as a failure, never a
    silent zero-work ``ok``."""


def _codex_auth_scan_text(msg: dict) -> str:
    """0378: return ONLY the error/transport-relevant text of an
    app-server message for auth-signature scanning — never assistant /
    tool OUTPUT.

    The earlier detector scanned ``json.dumps(msg)`` of EVERY message,
    so an ``item/commandexecution/outputdelta`` (command stdout/stderr)
    or an assistant message that merely *mentions* an auth string —
    e.g. ARCHITECT-REVIEWER reviewing/testing the auth code, whose
    output contains ``refresh_token_reused`` / ``token_expired`` /
    ``401`` / ``auth_failure`` literals — falsely tripped
    :class:`_CodexAuthError` even though the transport was healthy.

    Scope the scan instead:

    * a JSON-RPC ``error`` response is error-bearing → scan it whole;
    * ``item/*`` messages are assistant / tool OUTPUT (command output
      deltas, message text) → NEVER scanned for auth signatures;
    * other notification events (``codex/event``, ``thread/event``,
      ``turn/failed`` …) are scanned only on their explicitly
      error-bearing params fields, not the whole payload.
    """
    parts: list[str] = []
    err = msg.get("error")
    if err is not None:
        parts.append(json.dumps(err))
    method = msg.get("method") or ""
    # item/* = assistant / tool output. Its payload legitimately carries
    # auth-related strings when the agent reviews or tests auth code; it
    # is never an auth failure of THIS transport.
    if not method.startswith("item/"):
        params = msg.get("params")
        if isinstance(params, dict):
            for key in ("error", "message", "msg", "reason", "stderr"):
                val = params.get(key)
                if isinstance(val, str):
                    parts.append(val)
                elif val is not None:
                    parts.append(json.dumps(val))
    return "\n".join(parts).lower()


class _CodexStdioSession:
    """Thin line-delimited JSON-RPC client over a spawned
    ``codex app-server`` stdio process. One session drives exactly one
    turn (initialize → thread/start|resume → turn/start → wait
    turn/completed), then the process is closed. Verified framing:
    ``{json}\\n`` per message."""

    def __init__(self, proc) -> None:
        self._proc = proc
        self._buf = b""

    def _read_msg(self, deadline: float) -> dict:
        import select as _select
        import time as _time
        fd = self._proc.stdout.fileno()
        while True:
            # drain any complete line already buffered
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    continue
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise OSError("timeout reading app-server message")
            r, _w, _e = _select.select([fd], [], [], remaining)
            if not r:
                raise OSError("timeout reading app-server message")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise OSError("app-server closed before response")
            self._buf += chunk

    def send(self, request: dict) -> None:
        self._proc.stdin.write(
            (json.dumps(request) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def call(self, request: dict, deadline: float) -> dict:
        """Send a request and return its id-matched response (skipping
        notifications / other ids)."""
        want = request.get("id")
        self.send(request)
        while True:
            msg = self._read_msg(deadline)
            if isinstance(msg, dict) and msg.get("id") == want:
                return msg

    def consume_turn(self, thread_id: str, deadline: float,
                     *, turn_req_id: int = 3) -> tuple[int, str]:
        """0375: read app-server messages until ``turn/completed`` for
        ``thread_id``; return ``(work_items, transcript)``.

        Raises :class:`_CodexAuthError` on an auth-failure signature in
        an error-bearing message (0378: command/assistant OUTPUT is not
        scanned — see :func:`_codex_auth_scan_text`), and ``OSError`` on
        a ``turn/failed`` or
        an error RESPONSE to the ``turn/start`` request — so an
        auth-broken or failed turn is classified as a failure, not a
        zero-work ``ok``. ``work_items`` counts assistant/tool activity
        notifications, so the caller records whether the turn did real
        (non-zero) work — the avatar-gate evidence."""
        work_items = 0
        transcript: list[str] = []
        while True:
            msg = self._read_msg(deadline)
            if not isinstance(msg, dict):
                continue
            # 0378: scan ONLY error/transport-relevant text, never
            # command/assistant output — reviewing or testing auth code
            # must not be misread as a live auth failure.
            auth_text = _codex_auth_scan_text(msg)
            if auth_text and any(
                    sig in auth_text for sig in _CODEX_AUTH_SIGNATURES):
                raise _CodexAuthError(
                    f"codex auth failure during driven turn: "
                    f"{auth_text[:200]}")
            # An error RESPONSE to the turn/start request → the turn never
            # ran; do not treat the eventual stream as a clean completion.
            if msg.get("id") == turn_req_id and msg.get("error"):
                emsg = (msg.get("error") or {}).get("message", "")
                raise OSError(f"turn/start error: {emsg}"[:200])
            method = msg.get("method")
            if method == "turn/failed":
                raise OSError(
                    f"turn/failed: {json.dumps(msg.get('params'))}"[:200])
            if method == "turn/completed":
                if ((msg.get("params") or {}).get("threadId")
                        in (thread_id, None)):
                    return work_items, "\n".join(transcript)
            elif method and method.startswith(
                    ("item/", "codex/event", "thread/event", "agent")):
                # assistant / tool activity → the turn did real work
                work_items += 1
                if len(transcript) < 40:
                    transcript.append(method)

    def wait_turn_completed(self, thread_id: str, deadline: float) -> dict:
        """Superseded by :meth:`consume_turn` (0375); kept as a thin
        compat shim — returns a minimal completion marker."""
        self.consume_turn(thread_id, deadline)
        return {"method": "turn/completed",
                "params": {"threadId": thread_id}}


def _drive_codex_turn_stdio(
    coord: Path, role_lower: str, thread_id: str,
    base_instructions: str | None, cwd: str | None, verbose: bool,
    *, turn_timeout: float = 1800.0, handshake_timeout: float = 60.0,
    turn_log_path: Path | None = None,
) -> str:
    """0321-iter3: drive ONE codex turn over a fresh ``codex app-server``
    stdio process. Blocking — intended to run in a daemon thread (or
    synchronously in tests against a fake server). Returns the threadId
    (minted on the first turn). Raises OSError on transport failure.

    Sequence: spawn → ``initialize`` → ``thread/start`` (first turn,
    baseInstructions) or ``thread/resume`` (subsequent) → ``turn/start``
    → wait ``turn/completed`` → close (process exits).

    0375: spawns against the SINGLE machine Codex home (auth), injects
    the role model via ``-c model=``, and records the codexHome + a
    non-zero-work signal in the turn-log (avatar-gate evidence). Raises
    :class:`_CodexAuthError` when the turn fails on a Codex auth
    problem — never a silent zero-work ``ok``."""
    import subprocess as _sp
    import time as _time
    argv = _codex_appserver_argv(_codex_role_model(coord, role_lower))
    codex_home = _machine_codex_home()
    turn_log = turn_log_path or _turn_log_path(coord, role_lower)
    try:
        proc = _sp.Popen(
            argv, stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.DEVNULL,
            env=_codex_appserver_env(role_lower, coord=coord), cwd=cwd or None,
        )
    except OSError as exc:
        raise OSError(f"failed to spawn codex app-server: {exc}")
    sess = _CodexStdioSession(proc)
    try:
        hs_deadline = _time.monotonic() + handshake_timeout
        init_resp = sess.call(_build_initialize_request(1), hs_deadline)
        # 0375: the app-server reports its codexHome in the initialize
        # response — capture it for the turn-log so the avatar gate can
        # confirm the machine home (not a per-role .codex-home) was used.
        reported_home = codex_home
        try:
            mm = re.search(r'"codex_?[Hh]ome"\s*:\s*"([^"]+)"',
                           json.dumps(init_resp))
            if mm:
                reported_home = mm.group(1)
        except (TypeError, ValueError):
            pass
        if thread_id:
            resp = sess.call(_build_thread_resume_request(2, thread_id),
                             hs_deadline)
            # If thread/resume fails (a phantom / stale threadId whose
            # rollout was lost), the app-server returns a JSON-RPC error.
            # Resuming a non-existent thread leaves the agent
            # context-blind → a turn that "completes" doing NOTHING (the
            # driven-codex reviewer bug). Fall back to a FRESH thread WITH
            # baseInstructions (the role contract) so the turn has context.
            if isinstance(resp, dict) and resp.get("error"):
                if verbose:
                    msg = ((resp.get("error") or {}).get("message") or "")
                    print(f"  0321: thread/resume {thread_id} failed "
                          f"({msg[:80]}); starting a fresh thread with "
                          f"contract", file=sys.stderr)
                thread_id = ""
        if not thread_id:
            resp = sess.call(
                _build_thread_start_request(2, base_instructions, cwd),
                hs_deadline)
            thread_id = (
                (((resp or {}).get("result") or {}).get("thread") or {})
                .get("id") or "")
            if not thread_id:
                raise OSError(
                    f"thread/start returned no threadId: {resp!r}"[:200])
            _record_codex_thread(coord / REGISTRY_DIR, role_lower,
                                 thread_id)
        sess.send(_build_turn_start_request(3, thread_id))
        work_items, transcript = sess.consume_turn(
            thread_id, _time.monotonic() + turn_timeout)
        # Per-turn record (driven roles have no pane). 0375: record the
        # codexHome (machine home, not a per-role auth copy) + whether the
        # turn did non-zero work — the avatar-gate evidence (#14/#21/#22).
        try:
            turn_log.write_text(
                f"codex app-server turn for {role_lower}\n"
                f"thread_id: {thread_id}\n"
                f"codex_home: {codex_home}\n"
                f"reported_codex_home: {reported_home}\n"
                f"work_items: {work_items}\n"
                f"non_zero_work: {bool(work_items)}\n"
                f"status: turn/completed\n"
                + (f"activity: {transcript}\n" if transcript else ""),
                encoding="utf-8",
            )
        except OSError:
            pass
        if verbose:
            print(
                f"  0321: codex turn/completed for {role_lower} "
                f"(thread {thread_id}, codex_home={codex_home}, "
                f"work_items={work_items})",
                file=sys.stderr,
            )
        return thread_id
    except _CodexAuthError as exc:
        # 0375: record a clear auth-failure outcome (NOT a zero-work ok).
        try:
            turn_log.write_text(
                f"codex app-server turn for {role_lower}\n"
                f"thread_id: {thread_id}\n"
                f"codex_home: {codex_home}\n"
                f"status: auth_failure\n"
                f"detail: {exc}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        raise
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            try:
                proc.kill()
            except OSError:
                pass


def _codex_thread_id(reg: dict | None) -> str:
    """Read the role's persisted app-server threadId (analogue of the
    claude ``session_id``). Empty when absent → first turn creates it."""
    if not reg:
        return ""
    v = reg.get("thread_id")
    return v if isinstance(v, str) else ""


def _record_codex_thread(registry_dir: Path, role_lower: str,
                         thread_id: str) -> None:
    """Persist the app-server threadId in the role's registry so
    subsequent events reuse the thread (best-effort)."""
    f = registry_dir / f"{role_lower}.json"
    try:
        reg = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        reg = {}
    if not isinstance(reg, dict):
        reg = {}
    reg["thread_id"] = thread_id
    try:
        f.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _spawn_driven_codex_turn(
    coord: Path,
    role_lower: str,
    base_instructions: str | None,
    cwd: str | None,
    verbose: bool,
    *,
    transport: "callable | None" = None,
    reg: dict | None = None,
    run_async: bool = True,
) -> tuple[bool, str]:
    """0321-iter3: drive one codex turn over a per-turn ``codex
    app-server`` stdio process. Symmetric to ``_spawn_driven_turn``
    (claude ``-p``) — both spawn a fresh process per turn.

    Honors the per-role run-lock (shared with the claude path): a turn
    in flight → set the pending marker and return without spawning a
    second turn. On the first turn (no threadId in the registry) the
    stdio sequence issues ``thread/start`` (baseInstructions =
    contract) and records the threadId; later turns ``thread/resume``
    the persisted id. The actual turn runs to ``turn/completed``.

    ``transport`` is a test seam — a callable taking a JSON-RPC request
    dict and returning the response dict; when given, the request
    SEQUENCE (initialize → thread/start|resume → turn/start) is driven
    synchronously through it and the run-lock is left held for run-lock
    assertions. Without it, the blocking stdio turn runs in a daemon
    thread (``run_async``) so coordd's event loop is not held for the
    turn's duration; the thread releases the run-lock and re-fires one
    pending event on completion.
    """
    lock = _driven_run_lock_path(coord, role_lower)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        _driven_pending_path(coord, role_lower).touch()
        if verbose:
            print(
                f"  0321: codex turn for {role_lower} already running; "
                f"marked pending",
                file=sys.stderr,
            )
        return (False, "run-lock held; pending set")

    machine_home = _machine_codex_home()
    if transport is None and not codex_auth.machine_codex_auth_present(machine_home):
        detail = codex_auth.machine_codex_auth_error(
            machine_home, role_lower.upper())
        try:
            _turn_log_path(coord, role_lower).write_text(
                f"codex app-server turn for {role_lower}\n"
                f"codex_home: {machine_home}\n"
                f"status: auth_missing\n"
                f"detail: {detail}\n",
                encoding="utf-8",
            )
        except OSError:
            pass
        _note_turn_outcome(coord, role_lower, "auth", detail, verbose)
        if verbose:
            print(
                f"  0390: codex turn for {role_lower} not spawned: "
                f"machine auth missing in {machine_home}",
                file=sys.stderr,
            )
        return (False, "codex auth missing; retry/backoff recorded")

    thread_id = _codex_thread_id(reg)
    turn_log = _turn_log_path(coord, role_lower)
    _write_driven_run_lock(lock, role_lower, driver="codex",
                           log_path=str(turn_log), thread_id=thread_id)

    # Test seam: drive the request sequence synchronously through the
    # injected transport (no real codex process). Leave the lock held
    # for run-lock observability (mirrors the claude test path).
    if transport is not None:
        try:
            transport(_build_initialize_request(1))
            if thread_id:
                resp = transport(_build_thread_resume_request(2, thread_id))
                # phantom/stale threadId → fall back to a fresh thread
                # WITH baseInstructions (else the turn runs context-blind).
                if isinstance(resp, dict) and resp.get("error"):
                    thread_id = ""
            if not thread_id:
                resp = transport(_build_thread_start_request(
                    2, base_instructions, cwd))
                thread_id = (
                    (((resp or {}).get("result") or {}).get("thread")
                     or {}).get("id") or "")
                if not thread_id:
                    return (False,
                            f"thread/start returned no threadId: "
                            f"{resp!r}"[:200])
                _record_codex_thread(coord / REGISTRY_DIR, role_lower,
                                     thread_id)
            transport(_build_turn_start_request(3, thread_id))
            return (True,
                    f"codex turn driven for {role_lower} "
                    f"(thread {thread_id})")
        except Exception as exc:  # noqa: BLE001
            return (False, f"codex transport failed: {exc}"[:200])

    def _worker() -> None:
        klass = "ok"
        detail = ""
        try:
            _drive_codex_turn_stdio(
                coord, role_lower, thread_id, base_instructions, cwd,
                verbose, turn_log_path=turn_log)
        except _CodexAuthError as exc:
            # 0375: a Codex auth failure is NOT a retryable transport
            # blip and NEVER a zero-work ok — surface it loudly so the
            # escalation tells MAINTAINER to run `codex login` on the
            # machine $HOME/.codex (per-role auth copies are gone).
            klass = "auth"
            detail = f"AUTH: {exc}"[:300]
            if verbose:
                print(
                    f"  0375: codex turn for {role_lower} AUTH FAILURE: "
                    f"{exc}",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — log, never crash coordd
            msg = str(exc)
            low = msg.lower()
            if "timeout" in low:
                klass = "timeout"
            elif any(m in low for m in ("rate limit", "rate-limit",
                                        "overloaded", "429", "529")):
                klass = "rate_limit"
            else:
                klass = "error"
            detail = msg[:300]
            if verbose:
                print(
                    f"  0321: codex turn for {role_lower} failed: {exc}",
                    file=sys.stderr,
                )
        finally:
            # 0391: same exit-path hardening as the claude driver — the
            # lock is released first/unconditionally and each bookkeeping
            # step is independently guarded (codex records its thread inside
            # the turn, so no ``record`` callback here).
            _finalize_driven_turn(
                coord, role_lower, lock, klass, detail, verbose,
                refire=lambda: _spawn_driven_codex_turn(
                    coord, role_lower, base_instructions, cwd, verbose,
                    reg=read_registry(coord / REGISTRY_DIR, role_lower)),
            )

    if not run_async:
        _worker()
        return (True, f"codex turn driven for {role_lower}")
    import threading
    threading.Thread(target=_worker, daemon=True,
                     name=f"codex-turn-{role_lower}").start()
    return (True, f"codex turn dispatched (async) for {role_lower}")


def _maybe_drive_driven_role(coord: Path, canon_dir: Path,
                             coord_yaml_doc: dict | None,
                             located: tuple | None, role: str,
                             verbose: bool, trigger: str = "") -> bool | None:
    """0347: run one driven turn for ``role`` via the appropriate driver
    (claude ``--resume -p`` / codex app-server) when it is a MIGRATED
    driven role (schema lifecycle == driven AND coord.yaml window mode
    == driven). Returns the driver's bool result, or None when the role
    is NOT driven so the caller falls back to the legacy wake mechanism.

    Shared by ``_route_queue_event`` (queue / ``.stand`` events) AND the
    inbox-scan dispatch (Step 2). Before 0347 only the queue path drove
    driven roles; an inbox wake event fell through to sigint/press_enter
    against an IDLE bash pane, so a killed driven worker (e.g. a codex
    TECHNICAL-WRITER) was never re-driven / re-registered. Routing both
    paths through this helper makes a wake event recreate the worker on
    the next turn (force-fresh session when there's no prior session)."""
    tool = (located[1] if located else "").lower()
    lifecycle = _lifecycle_for_role(canon_dir, role)
    window_mode = _window_mode_for_role(coord_yaml_doc, role)
    if lifecycle != "driven" or window_mode != "driven":
        return None
    # 0376: ENOSPC preflight — never spawn a driven turn into a critically
    # low disk. A refreshed Codex/Claude auth token may then fail to persist
    # and the next turn fails with refresh_token_reused/token_expired,
    # looking like an auth bug. Surface an explicit blocker to MAINTAINER
    # (deduped) and decline to spawn rather than burn a zero-work turn.
    ok_disk, disk_diag = _disk_preflight(coord)
    if not ok_disk:
        _note_low_disk_blocker(coord, role.lower(), disk_diag, verbose)
        return False
    if "retry" not in trigger:
        # A REAL event (queue move / inbox / reconcile) drives the role —
        # clear any prior-failure backoff/escalation so genuinely new work
        # gets a clean chance (the retry path passes trigger=" (retry)").
        _clear_retry_state(role.lower(), coord)
    if tool == "claude":
        reg = read_registry(coord / REGISTRY_DIR, role.lower())
        session_id = (reg or {}).get("session_id") or ""
        session_name = (coord_yaml_doc.get("session") or "").strip() \
            if coord_yaml_doc else ""
        pane = (located[0] if located else "").strip()
        bootstrap_file = _driven_bootstrap_path(coord, role.lower())
        bf = (bootstrap_file if bootstrap_file and
              Path(bootstrap_file).is_file() else None)
        ok, diag = _spawn_driven_turn(
            coord, role.lower(), session_id, pane, session_name,
            bf, verbose, reg=reg, force_fresh=(not session_id),
        )
        if verbose:
            print(f"  0315/0318/0347: driven dispatch for {role}"
                  f"{trigger}: {diag}", file=sys.stderr)
        return ok
    if tool == "codex":
        reg = read_registry(coord / REGISTRY_DIR, role.lower())
        bootstrap_file = _driven_bootstrap_path(coord, role.lower())
        base_instructions = None
        try:
            bp = Path(bootstrap_file)
            if bp.is_file():
                base_instructions = bp.read_text(encoding="utf-8")
        except OSError:
            base_instructions = None
        ok, diag = _spawn_driven_codex_turn(
            coord, role.lower(), base_instructions,
            str(coord.parent), verbose, reg=reg,
        )
        if verbose:
            print(f"  0321/0347: codex dispatch for {role}"
                  f"{trigger}: {diag}", file=sys.stderr)
        return ok
    # Driven but an unknown tool — no driver; let the caller fall back.
    return None


def _role_pending_signature(coord: Path, role_meta: dict,
                            role_lower: str) -> frozenset:
    """0365: the SET of actionable files (``<dir>/<name>``) that give this
    driven role pending work — UNPROCESSED messages in its inbox plus tasks
    in a queue it ``claims_from`` (per schema). Counts ``.yaml`` AND ``.md``
    (1.x tasks/messages are yaml), excluding templates / processed markers /
    dotfiles. Empty == the role is idle.

    Returned as a frozenset so it doubles as a change-signature: the
    periodic reconcile (``_reconcile_driven_backlog`` with a ``seen`` dict)
    drives a role only when this backlog DIFFERS from the last state it
    drove — i.e. once per distinct backlog rather than every timer tick.
    Driven roles run on events / genuine new work, not timer polling.

    A ``processed-`` inbox marker is an ALREADY-handled (acked) message and
    is NOT pending. Counting it (the old ``_role_has_pending_task``
    ``processed_ok=True`` inbox check) made a role with only processed
    markers + empty claim queues "pending" forever, re-driven every
    reconcile — wasted API spend (the compounding half of this bug)."""
    names: set[str] = set()

    def _collect(d: Path) -> None:
        if not d.is_dir():
            return
        for f in d.iterdir():
            if not f.is_file() or f.suffix not in (".yaml", ".md"):
                continue
            n = f.name
            if n.startswith(".") or n.startswith("_TEMPLATE"):
                continue
            if n.startswith("processed-"):
                continue
            names.add(f"{d.name}/{n}")

    _collect(coord / "inbox" / role_lower)
    for q in (role_meta.get("claims_from") or []):
        if isinstance(q, str):
            _collect(coord / q)
    return frozenset(names)


def _role_has_pending_task(coord: Path, role_meta: dict,
                           role_lower: str) -> bool:
    """True if the role has an actionable file waiting (an UNPROCESSED inbox
    message or a task in a queue it ``claims_from``). Thin boolean over
    ``_role_pending_signature`` — processed inbox markers no longer count."""
    return bool(_role_pending_signature(coord, role_meta, role_lower))


def _clear_stale_driven_locks(coord: Path, verbose: bool) -> int:
    """A driven turn runs as a coordd-managed subprocess, so NO run-lock
    can survive a coordd restart — every ``driven-*.lock`` present on a
    FRESH start is stale (coordd was killed mid-turn, e.g. a daemon
    restart, taking the turn subprocess with it). Left in place, a stale
    lock makes the dispatcher AND the startup reconcile believe a turn is
    forever in-flight, so that role is NEVER re-driven again. Clear the
    locks (and their pending markers) on startup. Returns the count."""
    locks = coord / ".locks"
    if not locks.is_dir():
        return 0
    cleared = 0
    for pat in ("driven-*.lock", "driven-*.pending"):
        for f in sorted(locks.glob(pat)):
            try:
                f.unlink()
                cleared += 1
                if verbose:
                    print(f"coordd: cleared stale driven lock {f.name}",
                          file=sys.stderr)
            except OSError:
                pass
    return cleared


# 0365/0374: the one ``_reconcile_dispatch_decision`` skip reason that means
# "driven, unlocked, but idle backlog" — distinct from the lifecycle / window
# / run-lock skips. The periodic dedup resets a role's seen-signature ONLY on
# this reason (a genuinely cleared backlog), never on a lock skip (the role
# may be mid-drive on the very backlog it recorded).
_RECONCILE_REASON_NO_PENDING = "skip: no pending work in claim queues"


def _reconcile_dispatch_decision(
    coord: Path, canon_dir: Path, coord_yaml_doc: "dict | None",
    role_upper: str, meta: dict,
) -> tuple[bool, str]:
    """0374: decide whether the reconcile sweep should drive ``role_upper``
    this pass, returning ``(should_drive, reason)``.

    The dispatch trigger is a NON-EMPTY owned claim queue (e.g.
    ``feature_review`` for ARCHITECT-REVIEWER) — explicitly INDEPENDENT of
    whether the role has a live pid. A driven codex/claude role has no
    persistent process between turns (``.agent_registry/<role>.json`` holds
    only a ``thread_id`` / ``session_id``, no ``pid``); that absence is the
    normal driven steady state, NOT a dead-agent condition, and must never
    gate dispatch (GitHub #22 — a full feature_review produced zero
    REVIEWER turns and the skip had no observable reason). Split out as a
    pure decision fn so the per-role reason is unit-testable and emittable
    as a diagnostic, distinct from the codex auth/app-server failure (#21)
    that makes a *dispatched* turn complete without doing work."""
    role_lower = str(role_upper).lower()
    if _lifecycle_for_role(canon_dir, role_upper) != "driven":
        return (False, "skip: lifecycle != driven")
    if _window_mode_for_role(coord_yaml_doc, role_upper) != "driven":
        return (False, "skip: coord.yaml window mode != driven")
    if _driven_run_lock_path(coord, role_lower).is_file():
        return (False, "skip: turn in flight (run-lock held)")
    if not _role_has_pending_task(coord, meta, role_lower):
        return (False, _RECONCILE_REASON_NO_PENDING)
    return (True, "drive: pending work in an owned claim queue")


def _reconcile_driven_backlog(coord: Path, canon_dir: Path,
                              verbose: bool, *,
                              seen: dict | None = None,
                              trigger: str = " (startup-reconcile)") -> None:
    """Backlog sweep: coordd is otherwise inotify-reactive, so a task
    already sitting in a queue when coordd (re)starts — or whose event
    was consumed by a turn that failed to spawn — would never be driven
    (no fresh inotify event). Drive ONE turn for each DRIVEN role that has
    pending work in a queue it claims from and is not mid-turn (no
    run-lock). Makes ``update`` / a daemon restart pick up a hanging task
    instead of stranding it.

    The per-role drive decision (lifecycle / window / run-lock / pending)
    stays in ``_reconcile_dispatch_decision`` (0374), with its #22
    diagnostic reason preserved.

    0365: ``seen`` (the loop's dict, passed only on the PERIODIC path) makes
    the sweep EVENT-like rather than a timer poll. A role is driven only
    when its pending backlog (``_role_pending_signature``) DIFFERS from the
    state last driven; re-driving an UNCHANGED backlog every cycle is pure
    waste — the role already saw that work and either left it deliberately
    (re-driven by the next real event, e.g. an ask answer landing in its
    inbox) or its turn failed (the retry scheduler owns that). When a role's
    backlog clears, its ``seen`` entry is dropped so an identical backlog
    later re-drives once. When ``seen is None`` (STARTUP) every pending role
    is still driven once — drive-once recovery is unchanged."""
    schema_roles = load_schema_roles(canon_dir)
    if not schema_roles:
        return
    coord_yaml_doc = _read_coord_yaml(coord.parent)
    for role_upper, meta in schema_roles.items():
        if not isinstance(meta, dict):
            continue
        role_lower = str(role_upper).lower()
        should, reason = _reconcile_dispatch_decision(
            coord, canon_dir, coord_yaml_doc, role_upper, meta)
        # Diagnostic (#22): log the reconcile decision for DRIVEN roles —
        # the dispatch path was previously a silent ``continue`` per skip,
        # so a non-empty owned queue producing no turns was undebuggable.
        # The two non-driven early-outs (lifecycle / window) would fire for
        # every interactive/self-loop role every pass — skip logging those.
        if verbose and not reason.startswith(
                ("skip: lifecycle", "skip: coord.yaml window")):
            print(f"coordd: reconcile {role_upper}: {reason}",
                  file=sys.stderr)
        if not should:
            # 0365: an IDLE driven role (genuinely cleared backlog) drops
            # its seen entry so an identical backlog later re-drives once.
            # Only the no-pending skip resets — a lifecycle / window / lock
            # skip must not, since the role may be mid-drive on the very
            # backlog recorded in ``seen``.
            if seen is not None and reason == _RECONCILE_REASON_NO_PENDING:
                seen.pop(role_lower, None)
            continue
        # 0365: periodic dedup — drive once per DISTINCT backlog, not every
        # timer tick. ``should`` guarantees a non-empty signature (0374's
        # decision already confirmed pending work). ``seen is None`` at
        # startup skips the dedup → every pending role driven once.
        sig = _role_pending_signature(coord, meta, role_lower)
        if seen is not None and seen.get(role_lower) == sig:
            continue  # already drove this exact backlog — wait for a change
        located = _window_and_tool_for_role(coord_yaml_doc, role_upper)
        try:
            _maybe_drive_driven_role(
                coord, canon_dir, coord_yaml_doc, located, role_upper,
                verbose, trigger=trigger,
            )
            if seen is not None:
                seen[role_lower] = sig  # record only on a real drive attempt
        except Exception as exc:  # never let recovery crash startup
            if verbose:
                print(f"coordd: reconcile dispatch for {role_upper} "
                      f"failed: {exc}", file=sys.stderr)


# 1.6.0: leases currently being deployed by coordd (dedup so concurrent
# `.stand` events don't spawn a second deploy for the same lease).
_DEPLOYING_LEASES: set[str] = set()
# 1.6.3: per-lease auto-deploy attempt counter (a deploy that RAISES leaves
# the stand `preparing`; retry then escalate). lease_id -> attempts.
_DEPLOY_ATTEMPTS: dict[str, int] = {}


def _maybe_auto_deploy_stand(coord: Path, verbose: bool,
                             *, run_async: bool = True) -> bool:
    """1.6.0: coordd IS the stand deployer — there is no STAND-KEEPER LLM.

    When the stand is ``preparing`` with an active lease, run the lease's
    deploy profile via the deterministic engine (``stand.deploy_lease`` →
    ansible-playbook) in a background thread; the engine transitions the
    stand ready/down by rc. Because the deploy runs in coordd (a plain
    process), no Claude Code classifier / permission prompt is involved —
    the structural blocker that stranded full-deploy leases is gone.

    Dedup by lease_id so multiple ``.stand`` events (or a startup sweep +
    an event) don't double-deploy. Returns True if a deploy was started.
    """
    try:
        from greatminds.cli.stand_state import read_stand_state
        st = read_stand_state(coord)
    except Exception:
        return False
    if (st or {}).get("state") != "preparing":
        return False
    active = (st or {}).get("active_lease") or {}
    lease_id = active.get("lease_id")
    profile = active.get("profile")
    if not lease_id or not profile:
        return False
    if lease_id in _DEPLOYING_LEASES:
        return False
    _DEPLOYING_LEASES.add(lease_id)

    def _run() -> None:
        try:
            from greatminds.cli.stand import deploy_lease
            rc, _log = deploy_lease(coord, lease_id=lease_id)
            # deploy_lease transitioned the stand (ready on rc==0, down on
            # rc!=0) — the attempt completed. Clear the retry counter.
            _DEPLOY_ATTEMPTS.pop(lease_id, None)
            if verbose:
                print(f"  coordd auto-deploy lease {lease_id} rc={rc}",
                      file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never crash coordd
            # The deploy RAISED before transitioning → the stand is still
            # `preparing`. Count the attempt; retry until DEPLOY_MAX_ATTEMPTS,
            # then escalate + force the stand `down` so it is not stuck.
            n = _DEPLOY_ATTEMPTS.get(lease_id, 0) + 1
            _DEPLOY_ATTEMPTS[lease_id] = n
            detail = str(exc)[:300]
            if verbose:
                print(f"  coordd auto-deploy {lease_id} failed "
                      f"(attempt {n}/{DEPLOY_MAX_ATTEMPTS}): {exc}",
                      file=sys.stderr)
            if n >= DEPLOY_MAX_ATTEMPTS:
                _escalate_to_maintainer(
                    coord, "stand-deploy", "error", n,
                    f"lease {lease_id} profile {profile}: {detail}")
                try:
                    from greatminds.cli import stand_state as ss
                    reason = f"coordd auto-deploy failed {n}x: {detail}"

                    def _down(state: dict) -> None:
                        prev = state.get("state") or "preparing"
                        state["down_reason"] = reason
                        state["active_lease"] = None
                        ss.record_transition(state, prev, "down", "COORDD",
                                             lease_id=lease_id, reason=reason)

                    ss.update_stand_state(coord, _down)
                except Exception:  # noqa: BLE001
                    pass
                _DEPLOY_ATTEMPTS.pop(lease_id, None)
            # else: leave `preparing` — the periodic re-attempt retries.
        finally:
            _DEPLOYING_LEASES.discard(lease_id)

    if run_async:
        threading.Thread(target=_run, name=f"stand-deploy-{lease_id}",
                         daemon=True).start()
    else:
        _run()  # test seam: run the deploy inline
    if verbose:
        print(f"  1.6.0: coordd dispatched stand deploy for lease "
              f"{lease_id} (profile {profile})", file=sys.stderr)
    return True


def _route_queue_event(coord: Path, canon_dir: Path,
                       queue: str, filename: str, verbose: bool) -> bool:
    """0204: wake the owning role of ``queue`` when a file lands there.

    Pre-0204 coordd only reacted directly to ``inbox/<role>/*``
    events; non-inbox queue landings (a task moved into feature_dev,
    a stand request filed, a review session opened) reached the
    owning role only indirectly via notify_from_journal writing a
    wake-*.md into the inbox AFTER the journal got the entry. EXPLORER
    (review_session 0140) measured 2s+ latency for that path.

    Now: a file landing in any watched non-inbox queue directly
    triggers the schema.event_wake mechanism for the owning role.
    notify_from_journal still runs as before (Step 1 of the loop)
    so the inbox path remains for cross-role messaging; this hook
    is the direct route.

    Returns True if a wake was dispatched, False otherwise.
    """
    # Ignore inbox events here — those have their own routing.
    if queue == "inbox":
        return False
    # Filter file types: only .yaml / .md (task/inbox files). Atomic-
    # mv staging files (.tmp.*, dot-prefixed) are noise.
    if not (filename.endswith(".yaml") or filename.endswith(".md")):
        return False
    if filename.startswith(".") or filename.startswith("_TEMPLATE"):
        return False

    # 1.6.0: the stand is deployed by COORDD, not a STAND-KEEPER agent.
    # A `.stand` state change (→ preparing) runs the deterministic deploy
    # engine directly — no LLM role to wake.
    if queue == ".stand":
        return _maybe_auto_deploy_stand(coord, verbose)

    owner = _owning_role_for_queue(canon_dir, queue)
    if owner is None:
        return False

    # 0152 self-wake suppression: if the actor that just landed this
    # file IS the owner (e.g. PLANNER files a task into feature_inbox
    # they themselves own), the owner doesn't need to be woken — they
    # JUST did the work. The actor of the most recent journal entry
    # naming this filename is a robust signal.
    actor = _last_journal_actor_for(coord, filename)
    if actor and actor.upper() == owner.upper():
        if verbose:
            print(
                f"  0204: skip self-wake for {owner} (actor of "
                f"{queue}/{filename})",
                file=sys.stderr,
            )
        return False

    # Apply per-tool wake mechanism from schema.event_wake.by_tool
    # (the 0186 framework).
    project_dir = coord.parent
    coord_yaml_doc = _read_coord_yaml(project_dir)
    located = _window_and_tool_for_role(coord_yaml_doc, owner)
    tool = (located[1] if located else "").lower()

    # 0315 (0311 Phase 2a): driven claude roles are no longer just
    # woken — coordd RUNS the turn via ``claude --resume -p``. The
    # pane is idle bash between turns.
    #
    # 0318 (Phase 2d) migration gate: the driven driver fires only
    # when BOTH the schema lifecycle == 'driven' AND the coord.yaml
    # window mode == 'driven'. This is the per-fleet, one-at-a-time
    # migration switch. STAND-KEEPER is now migrated (window mode ==
    # driven), so a ``.stand`` state event runs an SK turn via the
    # driven branch below — the same direct queue→owner route every
    # other driven role uses (0341 ensures the ``.stand`` watch is
    # always attached so the event reaches here). Unmigrated roles
    # (chat / loop window mode), self-loop, and unmigrated codex roles
    # still fall through to the legacy wake mechanism further down.
    # 0315/0318/0321 driven dispatch (claude -p / codex app-server),
    # gated on lifecycle == driven AND window mode == driven. Extracted
    # to _maybe_drive_driven_role (0347) so the inbox-scan path shares
    # it. Returns None for non-driven roles → fall through to the legacy
    # wake mechanism below.
    driven = _maybe_drive_driven_role(
        coord, canon_dir, coord_yaml_doc, located, owner, verbose,
        trigger=f" for {queue}/{filename}")
    if driven is not None:
        return driven

    # A staged (USER-started) role — e.g. LIVE-DEVELOPER — is never
    # auto-woken by coordd: its pane holds a pre-typed start command
    # until the USER starts the session, and sending keys would corrupt
    # it. Deliver-only; the USER claims feature_live when they start.
    if _window_mode_for_role(coord_yaml_doc, owner) == "staged":
        if verbose:
            print(f"  staged role {owner}: deliver-only (USER-started), "
                  f"no event wake for {queue}/{filename}", file=sys.stderr)
        return False

    mechanism = _wake_mechanism_for_tool(
        tool, _lifecycle_for_role(canon_dir, owner))
    if mechanism == "sigint_deepest_descendant":
        sigint_sleeping_descendant(coord, owner, verbose)
        if verbose:
            print(
                f"  0204: woke {owner} (SIGINT) for "
                f"{queue}/{filename}",
                file=sys.stderr,
            )
        return True
    if mechanism == "tmux_send_keys":
        from greatminds.cli._send_enter import press_enter
        session = (coord_yaml_doc.get("session") or "").strip() \
            if coord_yaml_doc else ""
        window = (located[0] if located else "").strip()
        ok, diag = press_enter(
            coord, session, window, owner.lower(), tool or "claude",
            mode="wake", verify=False,
        )
        if verbose:
            print(
                f"  0204: woke {owner} (press_enter via input_sock) "
                f"for {queue}/{filename}: {diag}",
                file=sys.stderr,
            )
        return ok
    if verbose:
        print(
            f"  0204: no wake mechanism for tool={tool!r} "
            f"(role={owner}); deliver-only on {queue}/{filename}",
            file=sys.stderr,
        )
    return False


def _last_journal_actor_for(coord: Path, filename: str) -> str | None:
    """0204 helper: return the most recent journal entry's actor for
    the given task file (matched by id-prefix in the journal's
    ``task`` field), or None if no recent entry mentions it.

    The journal is append-only NDJSON; we scan the last ~50 entries
    so a hot loop never reads the whole file. Filename → task id by
    stripping the suffix; entries match if their ``task`` field is a
    prefix of the stem (handles both short-id and full-slug forms)."""
    import json as _json
    journal = coord / "journal.ndjson"
    if not journal.is_file():
        return None
    stem = filename.rsplit(".", 1)[0]
    try:
        # Read last ~16KB which is enough for ~50 entries.
        size = journal.stat().st_size
        with journal.open("rb") as f:
            if size > 16384:
                f.seek(size - 16384)
                f.readline()  # drop partial first line
            tail = f.read().decode("utf-8", errors="replace")
        # Scan recent → old.
        for line in reversed(tail.splitlines()):
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            task = entry.get("task") or ""
            if not task:
                continue
            if stem == task or stem.startswith(f"{task}-") \
               or task.startswith(stem) or task == stem[:4]:
                actor = entry.get("actor") or ""
                return actor.strip() if isinstance(actor, str) else None
    except OSError:
        return None
    return None


def _load_intent_orphan_min_age(canon_dir: Path) -> float:
    """0345: read ``schema.watchdog.intent_orphan_seconds`` (the min age
    before an orphaned intent may be reaped). Falls back to the default
    on any read/parse failure so coordd never crashes over config."""
    try:
        import yaml as _yaml
        doc = _yaml.safe_load(
            (canon_dir / "schema.yaml").read_text(encoding="utf-8")) or {}
        val = (doc.get("watchdog") or {}).get("intent_orphan_seconds")
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    except Exception:
        pass
    return INTENT_ORPHAN_MIN_AGE_DEFAULT


def _load_auto_update_config(canon_dir: Path) -> dict:
    """0199: read ``auto_update:`` from schema.yaml.

    Returns a dict with ``check_interval_seconds``, ``notify_target``,
    ``mode``, ``source``. Missing section → defaults that match the
    canonical 4h / MAINTAINER / notify_only / pypi values."""
    try:
        import yaml as _yaml
        doc = _yaml.safe_load(
            (canon_dir / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
    except (OSError, Exception):
        doc = {}
    au = doc.get("auto_update") or {}
    return {
        "check_interval_seconds": float(
            au.get("check_interval_seconds")
            or AUTO_UPDATE_CHECK_INTERVAL_DEFAULT
        ),
        "notify_target": str(au.get("notify_target") or "MAINTAINER"),
        "mode": str(au.get("mode") or "notify_only"),
        "source": str(au.get("source") or "pypi"),
    }


def _installed_greatminds_version() -> str | None:
    """0199: read installed version via importlib.metadata.

    Returns None if greatminds isn't installed (e.g. running from a
    raw source checkout without ``pip install -e .``)."""
    try:
        import importlib.metadata
        return importlib.metadata.version("greatminds")
    except Exception:
        return None


def _fetch_pypi_latest_version(pypi_url: str = AUTO_UPDATE_PYPI_URL,
                                timeout: float = AUTO_UPDATE_FETCH_TIMEOUT
                                ) -> str | None:
    """0199: fetch latest greatminds version from PyPI JSON API.

    Network call — wrap in broad try/except so PyPI being offline,
    DNS broken, JSON malformed, etc. don't crash coordd. Returns
    None on any failure; caller treats as "no update detected"."""
    import json as _json
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(pypi_url, timeout=timeout) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        return (payload.get("info") or {}).get("version")
    except (urllib.error.URLError, OSError, _json.JSONDecodeError,
            ValueError):
        return None


def _is_newer_version(latest: str, installed: str) -> bool:
    """0199: prefer ``packaging.version.parse`` for PEP 440 ordering.

    Falls back to lexicographic compare if packaging is somehow
    absent (shouldn't be, it's a transitive dep)."""
    try:
        from packaging.version import parse as _parse
        return _parse(latest) > _parse(installed)
    except Exception:
        return latest > installed


def _notify_maintainer_of_new_version(coord: Path, notify_target: str,
                                       latest: str, installed: str,
                                       verbose: bool) -> bool:
    """0199: file an inbox info message to ``notify_target`` about a
    newer greatminds version on PyPI.

    Returns True on success. Best-effort: a failed send doesn't crash
    coordd (which would defeat the whole notify-only design)."""
    try:
        body = (
            f"greatminds {latest} is available on PyPI (you have "
            f"{installed}). Run `greatminds update` when ready to "
            f"upgrade this fleet. Release notes: "
            f"https://pypi.org/project/greatminds/{latest}/"
        )
        cp = subprocess.run(
            [
                sys.executable, "-m", "greatminds.cli.main",
                "inbox", "send", notify_target,
                "--kind", "info",
                "--body", body,
            ],
            cwd=str(coord.parent),
            env={**os.environ, "GREATMINDS_ROLE": "MAINTAINER"},
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode == 0:
            return True
        if verbose:
            print(
                f"coordd: auto_update notify failed: "
                f"{(cp.stderr or '').strip()[:200]}",
                file=sys.stderr,
            )
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        if verbose:
            print(f"coordd: auto_update notify exception: {exc}",
                  file=sys.stderr)
        return False


def _read_coord_yaml(project_dir: Path) -> dict | None:
    """0186: read ``<project>/coord.yaml`` to look up a role's tmux
    window + tool. Cached behavior is deliberately omitted — coord.yaml
    is small and operator edits during runtime should be picked up on
    the next nudge (rate-limited anyway). Returns None on missing/
    malformed file."""
    for cand in (
        project_dir / "coord.yaml",
        project_dir / "coordination" / "coord.yaml",
    ):
        if cand.is_file():
            try:
                doc = yaml.safe_load(cand.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                return None
            return doc if isinstance(doc, dict) else None
    return None


def _window_and_tool_for_role(coord_yaml: dict | None,
                              role: str) -> tuple[str, str] | None:
    """Return ``(window_name, tool)`` for a role per coord.yaml's
    ``windows:`` list, or None when the role isn't declared (e.g.
    role-less ``ops`` bash window, or coord.yaml absent)."""
    if not coord_yaml:
        return None
    role_upper = role.upper()
    for w in coord_yaml.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if (w.get("role") or "").upper() == role_upper:
            return ((w.get("name") or "").strip(),
                    (w.get("tool") or "").strip().lower())
    return None


def _window_mode_for_role(coord_yaml: dict | None, role: str) -> str:
    """0318: return the coord.yaml ``mode:`` for a role's window (or
    "" when absent). The driven-driver migration gate is per-fleet:
    a role is driven only when its coord.yaml window mode == 'driven'
    AND its schema lifecycle == 'driven'. This lets roles migrate to
    the driven model ONE AT A TIME (Phase 2d migrates READER only;
    STAND-KEEPER etc. keep their wake path until their own phase)."""
    if not coord_yaml:
        return ""
    role_upper = role.upper()
    for w in coord_yaml.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if (w.get("role") or "").upper() == role_upper:
            return (w.get("mode") or "").strip().lower()
    return ""


def _wake_mechanism_for_tool(tool: str, lifecycle: str | None = None) -> str:
    """Schema-driven wake-mechanism dispatch.

    LIFECYCLE WINS over tool. interactive / self-loop roles (PLANNER,
    MAINTAINER) run a LIVE TUI in a tmux pane: SIGINT to a live
    codex/claude process is Ctrl-C — the agent QUITS. They must be woken
    only by typing into the pane (tmux_send_keys), NEVER sigint. The
    sigint mechanism is for a loop agent blocked on a `bash sleep`
    descendant (where the deepest descendant is the sleep, safe to
    interrupt) — not for an interactive TUI. So force tmux_send_keys for
    interactive/self-loop regardless of tool.

    For all other lifecycles the per-tool table applies. ``coord.yaml``'s
    ``tool:`` field selects the mechanism; defaults: codex/cursor →
    sigint, claude → tmux_send_keys; unknown tools → "" (no event wake)."""
    if lifecycle in ("interactive", "self-loop"):
        return "tmux_send_keys"
    try:
        from greatminds.core.paths import find_canon_dir
        doc = yaml.safe_load(
            (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
        ) or {}
        table = (doc.get("event_wake") or {}).get("by_tool") or {}
    except (OSError, yaml.YAMLError):
        table = {}
    # Sensible defaults if schema doesn't carry the table yet.
    defaults = {
        "codex": "sigint_deepest_descendant",
        "cursor": "sigint_deepest_descendant",
        "claude": "tmux_send_keys",
    }
    return table.get(tool.lower(), defaults.get(tool.lower(), ""))


def sigint_sleeping_descendant(coord: Path, role: str,
                               verbose: bool = False) -> bool:
    """0150: SIGINT the deepest sleep descendant of ``role``'s agent.

    When a new file lands in ``inbox/<role>/`` or a claim queue, the
    natural agent-wake is gated on the sleep timer's expiry (60s to
    600s depending on adaptive backoff). SIGINTing the agent's
    deepest descendant aborts a blocking ``sleep`` syscall: bash sees
    the non-zero return, the agent's tool call resolves, the next
    tick runs against the now-present inbox file. End-to-end latency
    drops from "remaining sleep window" to "coordd poll interval +
    one signal" (≤0.2s in practice).

    Safe by construction (0093 primitive's guard):
      - leaf == agent_pid (no descendants → agent is not asleep,
        possibly mid-thought) → return without signaling.
      - registry missing / pid dead → no-op.

    Returns True iff a SIGINT was delivered.
    """
    from greatminds.cli._send_enter import (
        _deepest_descendant,
        _pid_alive,
        _process_comm,
        _send_sigint,
    )

    reg = read_registry(coord / REGISTRY_DIR, role)
    if reg is None:
        return False
    pid = reg.get("pid")
    try:
        pid_int = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_int = None
    if pid_int is None or not _pid_alive(pid_int):
        return False
    leaf = _deepest_descendant(pid_int)
    if leaf is None or leaf == pid_int:
        # No sleep descendant → agent isn't asleep on a tool subprocess.
        # SIGINTing the agent itself would be hostile; skip.
        return False
    # Only interrupt an ACTUAL ``bash sleep`` backoff timer. The
    # ``leaf != pid_int`` guard alone is NOT enough: a multi-process
    # interactive TUI (codex: node → engine → threads; cursor likewise)
    # has a live ENGINE as its deepest descendant, so this branch is
    # reached and SIGINT would QUIT the agent. Gate strictly on the leaf
    # being a real ``sleep`` (mirrors press_enter's guard).
    leaf_comm = _process_comm(leaf)
    if leaf_comm != "sleep":
        if verbose:
            print(
                f"  event-wake: role={role} skip SIGINT — leaf pid={leaf} "
                f"comm={leaf_comm!r} is a live engine, not a sleep",
                file=sys.stderr,
            )
        return False
    ok = _send_sigint(leaf)
    if ok and verbose:
        print(
            f"  event-wake: role={role} SIGINT pid={leaf} "
            f"(parent agent pid={pid_int})",
            file=sys.stderr,
        )
    return ok


def push_to_role(coord: Path, role: str, file_path: str, verbose: bool,
                 bypass_fresh_guard: bool = False) -> bool:
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

    ``bypass_fresh_guard=True`` — callers that have already made the
    staleness decision against their own threshold skip this guard
    (double-checking with a fixed 60s ceiling would suppress every
    threshold below 60s). Pid liveness check still runs.
    """
    if not bypass_fresh_guard and role.lower() not in NO_KEYSTROKE_INJECT_ROLES:
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


@click.command(name="coordd",
               short_help="keystroke-pusher daemon (heartbeat-aware)",
               help=__doc__)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="project root containing coordination/ (default: cwd "
                   "or registry lookup via --project)")
@click.option("--project", "project_name", default=None,
              help="project name; resolved to project_dir via "
                   "~/.config/greatminds/projects.json. Used by the systemd "
                   "template unit greatminds-daemon@<project>.service.")
@click.option("--interval-sec", type=float, default=1.0,
              help="polling interval; 1.0 keeps CPU near zero on idle. "
                   "Don't go below 0.2.")
@click.option("--verbose", "-v", is_flag=True)
def coordd(project_dir: Path | None, project_name: str | None,
           interval_sec: float, verbose: bool) -> None:
    from greatminds.core.paths import find_canon_dir

    if project_dir is None and project_name:
        # Resolve via the per-user project registry written by
        # `greatminds daemon install` (and `greatminds setup` going forward).
        from greatminds.cli.daemon import lookup_project_dir
        resolved = lookup_project_dir(project_name)
        if resolved is None:
            click.echo(
                f"coordd: error: no project registered as {project_name!r}; "
                "run `greatminds daemon install` first",
                err=True,
            )
            raise click.exceptions.Exit(2)
        project_dir = resolved

    project_dir = project_dir or Path.cwd()
    coord = project_dir / "coordination"
    if not coord.is_dir():
        click.echo(f"coordd: error: {coord} not found", err=True)
        raise click.exceptions.Exit(1)

    inbox = coord / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    registry = coord / REGISTRY_DIR
    registry.mkdir(parents=True, exist_ok=True)

    interval = max(0.2, float(interval_sec))

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
    if verbose:
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

    # In-flight-turn hang detection (replaces the old stale-kick /
    # stalled-sweep heartbeat-cold nudges, which were a loop-mode
    # concept). A driven turn runs as a coordd subprocess holding the
    # role's run-lock; if the lock is held past the hang threshold with
    # no heartbeat progress, escalate ONCE to MAINTAINER.
    hang_threshold = _hang_threshold_seconds(canon_dir)
    last_hang_report: dict[str, float] = {}
    last_hang_check: float = 0.0

    # Per-role timestamp of last dead-pid report issued to MAINTAINER's
    # inbox. Throttles so a long-dead agent doesn't flood the inbox.
    last_dead_report: dict[str, float] = {}
    last_dead_check: float = 0.0
    if verbose:
        print(
            f"coordd: hang detection enabled "
            f"(in-flight turn, threshold {hang_threshold:.0f}s → "
            f"escalate MAINTAINER)",
            file=sys.stderr,
        )

    # 0199: PyPI version auto-check. Config from schema.auto_update;
    # cadence is independent of the polling interval. Tracks the
    # last-notified PyPI version in process memory so a still-un-
    # acted-upon newer release doesn't spam every check. Resets on
    # coordd restart (intentional: a fresh process picks up the new
    # installed version if MAINTAINER ran update meanwhile).
    auto_update_cfg = _load_auto_update_config(canon_dir)
    last_auto_update_check: float = 0.0
    last_notified_version: str | None = None

    # 0345: orphaned-intent reaping cadence + min-age (from schema).
    intent_orphan_min_age = _load_intent_orphan_min_age(canon_dir)
    last_intent_reap: float = 0.0
    if verbose:
        print(
            f"coordd: intent reaping enabled "
            f"(interval {INTENT_REAP_INTERVAL_SEC:.0f}s, "
            f"min-age {intent_orphan_min_age:.0f}s)",
            file=sys.stderr,
        )
    if verbose:
        print(
            f"coordd: auto_update enabled "
            f"(interval {auto_update_cfg['check_interval_seconds']:.0f}s, "
            f"target {auto_update_cfg['notify_target']}, "
            f"mode {auto_update_cfg['mode']}, "
            f"source {auto_update_cfg['source']})",
            file=sys.stderr,
        )

    # 0169: inotify-driven wake. The pre-0169 main loop blocked on
    # ``time.sleep(interval)`` between scans, so reaction latency was
    # bounded below by ``interval`` (default 1.0s; minimum 0.2s). With
    # inotify_simple watching ``coord/inbox/*`` and the active queue
    # dirs, the loop wakes within milliseconds of any new file. The
    # polling tick remains as a safety net for fs-quirks (NFS, fuse,
    # symlink races) and for the periodic checks that don't depend on
    # file events (stale-kick, dead-pid watch).
    #
    # Linux-only — inotify_simple has no fallback on macOS/Windows.
    # If the import fails or watch-add fails, we fall through to
    # plain polling (the pre-0169 contract).
    inotify_watcher = _make_inotify_watcher(coord, verbose)
    poll_or_event_wait = (
        inotify_watcher.read_or_timeout if inotify_watcher is not None
        else lambda timeout_s: (time.sleep(timeout_s), [])[-1]
    )
    known: set[str] = set(baseline)

    # Startup recovery, in order:
    #   1. Clear stale driven run-locks — a turn is a coordd subprocess, so
    #      any lock present on a fresh start is from a coordd that was
    #      killed mid-turn; left in place it blocks that role's dispatch
    #      FOREVER (dispatcher + reconcile both treat it as in-flight).
    #   2. Reconcile the backlog — drive any driven role with pending work
    #      in its claim queues (a task that landed before this coordd
    #      started watching, or whose event was consumed by a failed spawn).
    # Without (1), a daemon restart mid-turn permanently strands the role;
    # without (2), inotify (NEW events only) never picks up a queued task.
    #
    # 0365: backlog signatures last driven per role, so the PERIODIC reconcile
    # drives a role once per distinct backlog state instead of every
    # RECONCILE_INTERVAL_SEC tick (the timer-polling-idle-driven-roles bug).
    # Seeded by the startup reconcile so the first periodic tick does not
    # immediately re-drive what startup just drove.
    _reconcile_seen: dict = {}
    try:
        _clear_stale_driven_locks(coord, verbose)
        # 0376: cap coordd's own unbounded .turns/ log growth on startup.
        _prune_turn_logs(coord, verbose=verbose)
        _reconcile_driven_backlog(coord, canon_dir, verbose,
                                  seen=_reconcile_seen)
        # 1.6.0: a lease left `preparing` when coordd (re)started — e.g.
        # killed mid-deploy — must be picked up; coordd re-runs the deploy.
        _maybe_auto_deploy_stand(coord, verbose)
    except Exception as exc:
        if verbose:
            print(f"coordd: startup recovery failed: {exc}", file=sys.stderr)

    _last_deploy_retry = time.monotonic()
    _last_reconcile = time.monotonic()
    while not stop["flag"]:
        try:
            events = poll_or_event_wait(interval) or []

            # Periodic backlog reconcile — the autonomy backstop. coordd is
            # otherwise inotify-reactive, so a driven role that parked (its
            # turn completed without moving its task, or a self-set "wake in
            # 1h") with work still pending in its claim queue would freeze:
            # no new event fires to re-drive it. This periodic sweep re-drives
            # such roles (lock-safe: skips any role mid-turn; a no-op when no
            # role has pending work). 0365: it is NOT a timer poll —
            # ``_reconcile_seen`` makes it drive a role only when that role's
            # pending backlog CHANGED since the state last driven, so an idle
            # role (or one parked on work it deliberately left) is not
            # re-spawned every cycle (that wasted API spend was the bug).
            _now_r = time.monotonic()
            if _now_r - _last_reconcile >= RECONCILE_INTERVAL_SEC:
                _last_reconcile = _now_r
                try:
                    _reconcile_driven_backlog(
                        coord, canon_dir, verbose, seen=_reconcile_seen,
                        trigger=" (periodic-reconcile)")
                    # 0376: keep .turns/ bounded on a long-lived coordd too
                    # (startup-only pruning never fires on a process that
                    # never restarts).
                    _prune_turn_logs(coord, verbose=verbose)
                except Exception as exc:  # noqa: BLE001
                    if verbose:
                        print(f"coordd: periodic reconcile error: {exc}",
                              file=sys.stderr)

            # Re-attempt a stand deploy stuck in `preparing` (the deploy
            # raised before transitioning and fired no further event).
            # _maybe_auto_deploy_stand is a no-op unless the stand is
            # preparing with a lease not already deploying.
            _now_d = time.monotonic()
            if _now_d - _last_deploy_retry >= DEPLOY_RETRY_INTERVAL_SEC:
                _last_deploy_retry = _now_d
                try:
                    _maybe_auto_deploy_stand(coord, verbose)
                except Exception as exc:  # noqa: BLE001
                    if verbose:
                        print(f"coordd: deploy-retry error: {exc}",
                              file=sys.stderr)

            # Re-dispatch driven roles whose last turn failed and whose
            # backoff is now due (targeted retry, not a blanket sweep).
            try:
                _process_due_retries(coord, canon_dir, verbose)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"coordd: retry scheduler error: {exc}",
                          file=sys.stderr)

            # 0204: route non-inbox queue events directly to owning
            # roles. Pre-0204 a file landing in feature_inbox / stand_
            # requests / review_sessions was only seen by the owning
            # role indirectly via notify_from_journal → inbox/wake.
            # Now the inotify event triggers schema.event_wake directly
            # so reaction latency drops from ~2s to sub-second. The
            # inbox path stays for cross-role messaging — see Step 2.
            if inotify_watcher is not None:
                for ev in events:
                    queue = inotify_watcher.queue_for(ev.wd)
                    if queue is None or queue == "inbox":
                        continue
                    try:
                        _route_queue_event(
                            coord, canon_dir, queue,
                            ev.name, verbose,
                        )
                    except Exception as exc:
                        if verbose:
                            print(
                                f"coordd: 0204 route_queue_event "
                                f"error: {exc}", file=sys.stderr,
                            )

            # Step 1: run notify_from_journal — writes inbox messages for any
            # new journal lines. Idempotent (state file tracks last offset).
            try:
                subprocess.run(
                    [
                        *notify_invocation,
                        "--project-dir", str(project_dir),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception as exc:
                if verbose:
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
                # 0186: schema-driven event-wake dispatch by tool.
                # coord.yaml's tool: field per window selects which
                # wake mechanism applies:
                #   codex/cursor → sigint_deepest_descendant (0150 path)
                #   claude       → tmux_send_keys (NEW for chat-mode)
                # Pre-0186: claude chat-mode roles (PLANNER, MAINTAINER)
                # were silently skipped — inbox messages to them
                # never auto-triggered a tick. This dispatcher closes
                # the hole.
                project_dir = coord.parent
                coord_yaml_doc = _read_coord_yaml(project_dir)
                located = _window_and_tool_for_role(coord_yaml_doc, role)
                tool = (located[1] if located else "").lower()
                # 0347: a driven role woken by an inbox event must be
                # RE-DRIVEN (claude -p / codex app-server), not poked via
                # sigint/press_enter against an idle bash pane — that's
                # why a killed driven worker never came back. The driven
                # turn reads the just-delivered inbox file at tick start;
                # leave the file in place for it to consume + ack.
                driven = _maybe_drive_driven_role(
                    coord, canon_dir, coord_yaml_doc, located, role,
                    verbose, trigger=f" (inbox {Path(path).name})")
                if driven is not None:
                    continue
                mechanism = _wake_mechanism_for_tool(
                    tool, _lifecycle_for_role(canon_dir, role))
                if mechanism == "sigint_deepest_descendant":
                    sigint_sleeping_descendant(coord, role, verbose)
                    push_to_role(coord, role, path, verbose)
                elif mechanism == "tmux_send_keys":
                    from greatminds.cli._send_enter import press_enter
                    session = (coord_yaml_doc.get("session") or "").strip() \
                        if coord_yaml_doc else ""
                    window = (located[0] if located else "").strip()
                    press_enter(
                        coord, session, window, role.lower(), tool or "claude",
                        mode="wake", verify=False,
                    )
                else:
                    # No event-wake mechanism registered for this tool
                    # (e.g. an exotic / future tool, or NO_KEYSTROKE_
                    # INJECT_ROLES legacy semantic for a role with no
                    # window). Deliver-only — file is on disk; agent
                    # picks it up on its own next tick.
                    if verbose:
                        print(f"  deliver-only (no event-wake mechanism "
                              f"for tool={tool!r}): role={role} "
                              f"file={Path(path).name}",
                              file=sys.stderr)
            # Drop known entries for files agents have processed and deleted.
            known &= current

            # Step 3: in-flight-turn hang detection. A driven turn runs
            # as a coordd subprocess holding the role's run-lock
            # (.locks/driven-<role>.lock) for the turn's full duration.
            # If the lock has been held past the hang threshold AND the
            # role's heartbeat has not advanced within that window, the
            # turn is hung (subprocess alive but no progress) — escalate
            # ONCE to MAINTAINER (coordd does NOT kill; MAINTAINER
            # decides). Between turns the lock is absent → nothing is
            # checked, so an idle driven pane is never flagged.
            now_ts = time.time()
            if now_ts - last_hang_check >= HANG_CHECK_INTERVAL_SEC:
                last_hang_check = now_ts
                locks_dir = coord / ".locks"
                # A DRIVEN turn (claude -p / codex) does NOT refresh a
                # heartbeat, so the heartbeat-based hang_threshold (300s)
                # false-flagged EVERY legitimately-long driven turn as hung
                # (e.g. a 6-min task or a TESTER turn awaiting a multi-minute
                # remote compute) → a flood of bogus "hung-<role>" asks to
                # MAINTAINER. coordd already KILLS a driven turn at
                # DRIVEN_TURN_TIMEOUT_SEC, so a run-lock older than that means
                # the kill did not release it = a GENUINE stuck turn. Use that
                # as the bound for driven locks; heartbeat is just an early-out
                # for any tool that happens to refresh it.
                driven_hang_threshold = max(hang_threshold,
                                            DRIVEN_TURN_TIMEOUT_SEC)
                if locks_dir.is_dir():
                    for lk in sorted(locks_dir.glob("driven-*.lock")):
                        role_lower = lk.name[len("driven-"):-len(".lock")]
                        try:
                            lock_age = now_ts - lk.stat().st_mtime
                        except OSError:
                            continue
                        if lock_age < driven_hang_threshold:
                            continue  # turn within its kill bound — not hung
                        hb_age = heartbeat_age_seconds(coord, role_lower)
                        action = _driven_lock_decision(
                            lock_age, hb_age, hang_threshold,
                            driven_hang_threshold, ORPHAN_RECLAIM_GRACE_SEC,
                            now_ts - last_hang_report.get(role_lower, 0.0),
                        )
                        if action == "skip":
                            continue
                        if action == "reclaim":
                            # issue #11 orphan-lock defense: subprocess is
                            # certainly dead (past kill bound + grace) yet the
                            # lock survived → unlink so the role re-drives
                            # instead of waiting for manual cleanup. A hang
                            # report already told MAINTAINER earlier.
                            try:
                                lk.unlink()
                            except OSError:
                                pass
                            # 0391 diagnostic: "stale lock cleared" must be an
                            # operator-visible artifact, not just verbose
                            # stderr — write a turn-log record so the reclaim
                            # is distinguishable from a live/timed-out turn.
                            try:
                                _turn_log_path(coord, role_lower).write_text(
                                    f"=== RUN-LOCK ORPHAN RECLAIMED ===\n"
                                    f"driven-{role_lower}.lock was held "
                                    f"{lock_age:.0f}s (past kill bound "
                                    f"{driven_hang_threshold:.0f}s + grace "
                                    f"{ORPHAN_RECLAIM_GRACE_SEC:.0f}s) with no "
                                    f"live subprocess → coordd unlinked it so "
                                    f"{role_lower.upper()} can re-drive.\n",
                                    encoding="utf-8")
                            except OSError:
                                pass
                            last_hang_report.pop(role_lower, None)
                            if verbose:
                                print(f"  hang: role={role_lower} run-lock "
                                      f"orphaned {lock_age:.0f}s (past kill "
                                      f"bound + grace) → reclaimed",
                                      file=sys.stderr)
                            continue
                        # action == "report"
                        write_hang_report(coord, role_lower, lock_age,
                                          hb_age, now_ts)
                        last_hang_report[role_lower] = now_ts
                        if verbose:
                            print(f"  hang: role={role_lower} run-lock held "
                                  f"{lock_age:.0f}s, heartbeat "
                                  f"{('%.0fs' % hb_age) if hb_age is not None else 'never'}"
                                  f" → escalated MAINTAINER", file=sys.stderr)

            # Step 4: dead-pid watch. For each role in the registry,
            # if pid is no longer alive, file an ask to PLANNER's inbox.
            # Throttled per-role (DEAD_REPORT_INTERVAL_SEC). When the
            # role becomes alive again (registry rewritten by pty-launch),
            # the throttle slot is reset so a future death gets reported.
            if now_ts - last_dead_check >= DEAD_CHECK_INTERVAL_SEC:
                last_dead_check = now_ts
                _dp_coord_yaml = _read_coord_yaml(coord.parent)
                for reg_file in sorted(registry.glob("*.json")):
                    role = reg_file.stem
                    reg = read_registry(registry, role)
                    if reg is None:
                        continue
                    # 0311 driven fix: a driven role has NO persistent pid
                    # (idle bash between turns; coordd spawns each turn), so
                    # the dead-pid watch otherwise reports EVERY driven role as
                    # dead forever and floods inbox/maintainer/. Skip driven
                    # roles — their liveness is coordd's per-turn concern, not
                    # a persistent-pid check.
                    _role_u = role.upper()
                    if (_lifecycle_for_role(canon_dir, _role_u) == "driven"
                            and _window_mode_for_role(
                                _dp_coord_yaml, _role_u) == "driven"):
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
                        if verbose:
                            print(
                                f"  dead-report: role={role} pid={pid} → inbox/maintainer/",
                                file=sys.stderr,
                            )
                    except OSError as exc:
                        if verbose:
                            print(f"  dead-report failed for {role}: {exc}", file=sys.stderr)

            # (The old stalled-agent sweep that nudged loop-mode agents
            # on cold heartbeat is gone — a loop-mode concept. Driven
            # turns are coordd subprocesses with hang detection (Step 3);
            # idle driven panes are normal, not "stalled".)

            # Step 6: PyPI version auto-check. Throttled
            # by schema.auto_update.check_interval_seconds. Sends one
            # inbox info to notify_target when a newer version is
            # detected; doesn't re-spam until either (a) coordd
            # restarts (operator likely ran update) or (b) PyPI
            # publishes an even newer version. Notify-only mode —
            # MAINTAINER decides when to actually run
            # `greatminds update`.
            check_interval = auto_update_cfg["check_interval_seconds"]
            if (auto_update_cfg["source"] == "pypi"
                and auto_update_cfg["mode"] == "notify_only"
                and now_ts - last_auto_update_check >= check_interval):
                last_auto_update_check = now_ts
                try:
                    installed = _installed_greatminds_version()
                    latest = _fetch_pypi_latest_version()
                    if (installed and latest
                        and latest != last_notified_version
                        and _is_newer_version(latest, installed)):
                        ok = _notify_maintainer_of_new_version(
                            coord, auto_update_cfg["notify_target"],
                            latest, installed, verbose,
                        )
                        if ok:
                            last_notified_version = latest
                except Exception as exc:  # noqa: BLE001 — never crash loop
                    if verbose:
                        print(
                            f"coordd: auto_update check error: {exc}",
                            file=sys.stderr,
                        )

            # Step 7 (0345): reap orphaned intent files. Throttled to
            # INTENT_REAP_INTERVAL_SEC; uses the shared intent-clean core
            # (only removes intents older than the min-age whose task has
            # already left its from-queue). Maintenance, not a task/queue
            # mutation — coordd's read-only-on-content charter holds.
            if now_ts - last_intent_reap >= INTENT_REAP_INTERVAL_SEC:
                last_intent_reap = now_ts
                try:
                    from greatminds.cli.intent_clean import reap_orphan_intents
                    counts = reap_orphan_intents(coord, intent_orphan_min_age)
                    if verbose and counts["removed"]:
                        print(
                            f"coordd: reaped {counts['removed']} orphaned "
                            f"intent(s) (kept_active={counts['kept_active']}, "
                            f"kept_recent={counts['kept_recent']})",
                            file=sys.stderr,
                        )
                except Exception as exc:  # noqa: BLE001 — never crash loop
                    if verbose:
                        print(
                            f"coordd: intent reap error: {exc}",
                            file=sys.stderr,
                        )
        except KeyboardInterrupt:
            break
        except Exception as exc:
            if verbose:
                print(f"coordd: error in poll loop: {exc}", file=sys.stderr)
            time.sleep(1.0)

    if verbose:
        print("coordd: exit", file=sys.stderr)


if __name__ == "__main__":
    coordd()
