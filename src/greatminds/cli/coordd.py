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
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

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

# Stale-heartbeat kick: nudge an agent whose heartbeat has gone cold
# (transient API errors / rate-limits stalled its self-wake), provided
# it actually has pending work and we haven't already kicked it recently.
# Stale-kick is OFF by default. Agent prompts now carry adaptive
# backoff (self-wake), and real work arrives via inbox-file push, so
# the heartbeat kick is redundant in normal operation — and it was
# actively harmful: it nudged idle agents minute-after-minute when
# nothing was happening, and injected "continue your tick" into the middle
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
# NEVER inject keystrokes ("check inbox and continue your tick") into their
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

# Stalled-agent sweep (task 0017): even with no pending work, an agent
# whose heartbeat goes cold past `STALLED_THRESHOLD_DEFAULT` is treated
# as stuck (typically: Anthropic server-side rate-limit aborted the turn
# before `ScheduleWakeup` got called). Coordd nudges it through the same
# input_sock channel `push_to_role` uses. Defaults match
# `schema.watchdog.heartbeat_stale_seconds` so "stale" is one definition
# across the system. Overridable per-project via `coord.yaml: coordd:`.
STALLED_SWEEP_INTERVAL_DEFAULT = 300.0   # 5 min
STALLED_THRESHOLD_DEFAULT      = 600.0   # 10 min

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
    "stand_requests",
    "stand_wip",
    "stand_done",
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


def _read_coordd_config(project_dir: Path) -> tuple[float, float]:
    """Read coord.yaml ``coordd:`` block; fall back to defaults.

    Returns ``(stalled_sweep_interval_seconds, stalled_threshold_seconds)``.
    Invalid (non-numeric, ``<= 0``) values trigger a stderr warning and
    fall back to the module-level defaults. Missing block or missing
    coord.yaml → silent defaults. Cheap (one yaml.safe_load); coordd
    calls this at startup, not per-sweep.
    """
    interval = STALLED_SWEEP_INTERVAL_DEFAULT
    threshold = STALLED_THRESHOLD_DEFAULT
    coord_yaml_path = project_dir / "coord.yaml"
    if yaml is None or not coord_yaml_path.is_file():
        return interval, threshold
    try:
        cfg = yaml.safe_load(coord_yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return interval, threshold
    if not isinstance(cfg, dict):
        return interval, threshold
    sub = cfg.get("coordd")
    if not isinstance(sub, dict):
        return interval, threshold

    def _coerce(key: str, default: float) -> float:
        v = sub.get(key)
        if v is None:
            return default
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
        print(
            f"coordd: invalid coord.yaml: coordd.{key}={v!r}; using default {default}",
            file=sys.stderr,
        )
        return default

    return (
        _coerce("stalled_sweep_interval_seconds", interval),
        _coerce("stalled_threshold_seconds", threshold),
    )


def _load_coord_yaml_doc(project_dir: Path) -> dict:
    """Best-effort coord.yaml parse — returns {} on any failure."""
    p = project_dir / "coord.yaml"
    if yaml is None or not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _mode_for_role(coord_yaml: dict, role_upper: str) -> str | None:
    """Return ``coord.yaml.windows[N].mode`` for the entry whose ``role``
    matches ``role_upper`` (case-insensitive). ``None`` if no matching
    entry — used by ``_stalled_agent_sweep`` to flag orphan heartbeats.
    """
    windows = coord_yaml.get("windows") if isinstance(coord_yaml, dict) else None
    if not isinstance(windows, list):
        return None
    for w in windows:
        if not isinstance(w, dict):
            continue
        rw = (w.get("role") or "").strip().upper()
        if rw == role_upper:
            return w.get("mode") or None
    return None


def _stalled_agent_sweep(
    project_dir: Path,
    coord_yaml: dict,
    threshold_seconds: float,
    verbose: bool = False,
) -> int:
    """One pass over heartbeats; nudge stalled loop-mode agents.

    Closes the rate-limit-stall hole: Anthropic server-side rate limits
    abort a tick before ``ScheduleWakeup`` is called, leaving the agent
    pid alive but heartbeat cold forever. This sweep detects that state
    and pokes the agent's input_sock to wake it up.

    Per-heartbeat behavior (see plan 0017 §Behavior contract):

      - fresh (age < threshold)  → skip silently
      - FAST suffix (``.fast``)  → skip (scenario-C chat session)
      - orphan (no coord.yaml entry) → skip with WARN
      - mode != "loop"            → skip silently (chat is USER-paced)
      - dead pid                  → skip (``greatminds restart`` territory)
      - missing registry / input_sock → skip silently
      - otherwise → ``push_to_role`` (reuses existing socket I/O,
        PUSH_FRESH_GUARD_SEC, alive-check, fallback chain — no
        duplicate socket code).

    Returns count of nudges sent.
    """
    coord = project_dir / "coordination"
    if not coord.is_dir():
        return 0
    nudge_count = 0
    now = time.time()
    for hb_path in sorted(coord.glob("heartbeat.*")):
        try:
            age = now - hb_path.stat().st_mtime
        except OSError:
            continue
        if age < threshold_seconds:
            continue
        # `heartbeat.<role-lower>` or `heartbeat.<role-lower>.fast`.
        suffix = hb_path.name[len("heartbeat."):]
        if suffix.endswith(".fast"):
            if verbose:
                print(
                    f"  stalled-sweep skip: {hb_path.name} (FAST/chat mode)",
                    file=sys.stderr,
                )
            continue
        role_lower = suffix
        role_upper = role_lower.upper()
        mode = _mode_for_role(coord_yaml, role_upper)
        if mode is None:
            if verbose:
                print(
                    f"coordd: WARN orphan heartbeat {hb_path.name} — "
                    f"no matching role in coord.yaml; skipping nudge",
                    file=sys.stderr,
                )
            continue
        if mode != "loop":
            if verbose:
                print(
                    f"  stalled-sweep skip: {role_upper} mode={mode!r} (not loop)",
                    file=sys.stderr,
                )
            continue
        # task 0051 iter-6 (REVIEWER changes_requested): route through
        # the unified press_enter primitive so AGENT_ENTER_KEYS, heartbeat
        # verification, trust-prompt detection, and SIGSTOP rejection
        # all reach the stalled-sweep path. Previously this called
        # push_to_role directly, which bypassed those guards. The sweep
        # has already filtered by `stalled_threshold_seconds` (heartbeat
        # is necessarily stale at this point), so press_enter's lack of
        # an explicit fresh-guard is correct — bypass-fresh semantics
        # are met by the sweep's own gating.
        session = coord_yaml.get("session")
        window = _window_for_role(coord_yaml, role_upper)
        registry_dir = coord / REGISTRY_DIR
        reg = read_registry(registry_dir, role_lower)
        agent_type = ((reg.get("tool") if isinstance(reg, dict) else None)
                      or "claude").lower()
        if session and window:
            try:
                from greatminds.cli._send_enter import press_enter
                ok, diag = press_enter(
                    coord, session, window, role_lower, agent_type,
                    mode="wake",
                    verify=True,
                )
                if ok:
                    nudge_count += 1
                    if verbose:
                        print(
                            f"  nudged stalled {role_upper} via press_enter "
                            f"(heartbeat {age:.0f}s old): {diag}",
                            file=sys.stderr,
                        )
                elif verbose:
                    print(
                        f"  stalled-sweep press_enter FAILED for {role_upper} "
                        f"(heartbeat {age:.0f}s old): {diag}",
                        file=sys.stderr,
                    )
            except Exception as exc:  # primitive itself is best-effort; don't crash sweep
                if verbose:
                    print(
                        f"  stalled-sweep press_enter raised for {role_upper}: "
                        f"{exc}; falling back to push_to_role",
                        file=sys.stderr,
                    )
                if push_to_role(coord, role_lower,
                                f"<stalled-sweep: heartbeat {age:.0f}s old>",
                                verbose, bypass_fresh_guard=True):
                    nudge_count += 1
        else:
            # No session/window in coord.yaml — press_enter can't address
            # the pane, so fall back to push_to_role (which only uses the
            # input_sock / tty channel; no tmux send-keys fallback).
            if push_to_role(coord, role_lower,
                            f"<stalled-sweep: heartbeat {age:.0f}s old>",
                            verbose, bypass_fresh_guard=True):
                nudge_count += 1
                if verbose:
                    print(
                        f"  nudged stalled {role_upper} via push_to_role "
                        f"fallback (heartbeat {age:.0f}s old; no session/window "
                        f"in coord.yaml)",
                        file=sys.stderr,
                    )
    return nudge_count


def _window_for_role(coord_yaml: dict, role_upper: str) -> str | None:
    """Look up the tmux window name for ``role_upper`` from coord.yaml.

    Returns ``None`` if coord.yaml has no windows list or no match.
    Used by ``_stalled_agent_sweep`` to address capture-pane / send-keys.
    """
    windows = coord_yaml.get("windows")
    if not isinstance(windows, list):
        return None
    for w in windows:
        if not isinstance(w, dict):
            continue
        if (w.get("role") or "").upper() == role_upper:
            name = w.get("name")
            if isinstance(name, str) and name:
                return name
    return None


# 0186: per-role per-process timestamp of the last tmux send-keys
# nudge. Rate-limits chat-mode wakes so a burst of inbox writes
# doesn't flood the claude pane with N "check inbox" lines within a
# few seconds. Coalesces to one nudge per ``event_wake.tmux_send_keys.
# rate_limit_seconds`` window.
_LAST_TMUX_NUDGE: dict[str, float] = {}


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
    mechanism = _wake_mechanism_for_tool(tool)
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
        tmux_send_keys_wake(coord, owner, verbose)
        if verbose:
            print(
                f"  0204: woke {owner} (tmux send-keys) for "
                f"{queue}/{filename}",
                file=sys.stderr,
            )
        return True
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


def tmux_send_keys_wake(coord: Path, role: str,
                       verbose: bool = False) -> bool:
    """0186: chat-mode wake via ``tmux send-keys`` for claude panes.

    Pre-0186 chat-mode roles (claude PLANNER + MAINTAINER) had NO
    event-wake mechanism — they sat in NO_KEYSTROKE_INJECT_ROLES so
    push_to_role was skipped, and coordd's sigint primitive can't
    reach a claude process the same way it can SIGINT a codex
    wrapper's sleep (claude is the user-facing tool, not a sleep-
    descendant of a bash wrapper). Result: inbox messages to PLANNER /
    MAINTAINER never auto-triggered a tick.

    Mechanism: read coord.yaml for this role's tmux window; send the
    literal keystroke ``check inbox and continue your tick`` + Enter
    into that pane via ``tmux send-keys``. Claude's UI buffers the
    text the same way as if the operator typed it, runs the tick on
    Enter.

    Rate-limited per role per coordd process so a burst of N inbox
    writes within ``rate_limit_seconds`` only nudges claude once.
    """
    project_dir = coord.parent
    coord_yaml = _read_coord_yaml(project_dir)
    located = _window_and_tool_for_role(coord_yaml, role)
    if located is None:
        if verbose:
            print(f"  tmux-wake: role={role} not declared in coord.yaml; skip",
                  file=sys.stderr)
        return False
    window, _tool = located
    if not window:
        if verbose:
            print(f"  tmux-wake: role={role} has empty window name; skip",
                  file=sys.stderr)
        return False
    session = (coord_yaml.get("session") or "").strip()
    if not session:
        if verbose:
            print(f"  tmux-wake: no session in coord.yaml; skip", file=sys.stderr)
        return False

    # Rate-limit per role per process.
    rate_limit = 5.0  # default if schema absent (defensive)
    try:
        schema = _read_event_wake_schema(coord)
        rate_limit = float(schema.get("rate_limit_seconds") or rate_limit)
    except (OSError, KeyError, TypeError, ValueError):
        pass

    now = time.time()
    last = _LAST_TMUX_NUDGE.get(role, 0.0)
    if now - last < rate_limit:
        if verbose:
            print(
                f"  tmux-wake: role={role} rate-limited "
                f"(last nudge {now - last:.1f}s ago < {rate_limit:.0f}s); skip",
                file=sys.stderr,
            )
        return False

    # 0237: split text + Enter into TWO send-keys calls with the
    # ``WAKE_GAP_SECONDS`` gap between them — same pattern codex's
    # push_to_role uses (lines 51–58 of this module). When text +
    # Enter arrive in one bytes-blast some TUIs (claude included)
    # classify it as a paste and DON'T fire the prompt-submit event
    # on the trailing CR. Splitting registers the Enter as a
    # discrete keypress, which is what triggers the agent's turn.
    target = f"{session}:{window}"
    cp = subprocess.run(
        ["tmux", "send-keys", "-t", target, WAKE_TEXT],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        if verbose:
            print(
                f"  tmux-wake: role={role} tmux send-keys text failed: "
                f"{cp.stderr.strip()[:200]}",
                file=sys.stderr,
            )
        return False
    time.sleep(WAKE_GAP_SECONDS)
    cp = subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        if verbose:
            print(
                f"  tmux-wake: role={role} tmux send-keys Enter failed: "
                f"{cp.stderr.strip()[:200]}",
                file=sys.stderr,
            )
        return False
    _LAST_TMUX_NUDGE[role] = now
    if verbose:
        print(
            f"  event-wake: role={role} tmux send-keys to "
            f"{session}:{window}",
            file=sys.stderr,
        )
    return True


def _read_event_wake_schema(coord: Path) -> dict:
    """0186: read ``event_wake:`` section from schema.yaml. Returns the
    ``tmux_send_keys`` sub-mapping or an empty dict on absence — the
    caller has hard-coded defaults so missing/malformed schema is
    non-fatal."""
    from greatminds.core.paths import find_canon_dir
    schema_path = find_canon_dir() / "schema.yaml"
    doc = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
    section = (doc.get("event_wake") or {}).get("tmux_send_keys") or {}
    return section if isinstance(section, dict) else {}


def _wake_mechanism_for_tool(tool: str) -> str:
    """0186: schema-driven dispatch table. ``coord.yaml``'s ``tool:``
    field per window selects which wake mechanism coordd uses for
    that role. Defaults preserve pre-0186 behavior: codex/cursor →
    sigint (the 0150 path); claude → tmux_send_keys (NEW). Other
    tools → None (no event wake, falls back to natural ScheduleWakeup
    / interval polling)."""
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

    ``bypass_fresh_guard=True`` (task 0017) — callers that have already
    made the staleness decision against their own threshold
    (``_stalled_agent_sweep`` with ``stalled_threshold_seconds``, which
    may be < 60s) skip this guard. The sweep already filtered by
    heartbeat age; double-checking with a fixed 60s ceiling would
    suppress every threshold below 60s. Pid liveness check still runs.
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
    if verbose:
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

    # Stalled-agent sweep (task 0017): periodic poke for loop-mode
    # agents whose heartbeat went cold past threshold. Config is read
    # once at startup; restart coordd to pick up coord.yaml changes.
    stalled_sweep_interval, stalled_threshold = _read_coordd_config(project_dir)
    last_stalled_sweep: float = 0.0
    if verbose:
        print(
            f"coordd: stalled-sweep enabled "
            f"(interval {stalled_sweep_interval:.0f}s, "
            f"threshold {stalled_threshold:.0f}s)",
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
    while not stop["flag"]:
        try:
            events = poll_or_event_wait(interval) or []

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
                mechanism = _wake_mechanism_for_tool(tool)
                if mechanism == "sigint_deepest_descendant":
                    sigint_sleeping_descendant(coord, role, verbose)
                    push_to_role(coord, role, path, verbose)
                elif mechanism == "tmux_send_keys":
                    # Chat-mode pane: nudge via tmux send-keys only.
                    # No SIGINT (claude isn't a bash-wrapper descendant)
                    # and no push_to_role keystroke (claude has no
                    # input_sock; the tmux pane IS the input channel).
                    tmux_send_keys_wake(coord, role, verbose)
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
                        verbose,
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
                        if verbose:
                            print(
                                f"  dead-report: role={role} pid={pid} → inbox/maintainer/",
                                file=sys.stderr,
                            )
                    except OSError as exc:
                        if verbose:
                            print(f"  dead-report failed for {role}: {exc}", file=sys.stderr)

            # Step 5: stalled-agent sweep (task 0017) — nudge loop-mode
            # agents whose heartbeat has gone cold past
            # `stalled_threshold_seconds`. Independent of Step 3's
            # opt-in stale-kick (which also requires pending work);
            # this sweep fires unconditionally on heartbeat age alone.
            if now_ts - last_stalled_sweep >= stalled_sweep_interval:
                last_stalled_sweep = now_ts
                coord_yaml_doc = _load_coord_yaml_doc(project_dir)
                try:
                    _stalled_agent_sweep(
                        project_dir, coord_yaml_doc,
                        stalled_threshold, verbose,
                    )
                except Exception as exc:  # noqa: BLE001 — sweep must never crash the loop
                    if verbose:
                        print(
                            f"coordd: stalled-sweep error: {exc}",
                            file=sys.stderr,
                        )

            # Step 6: PyPI version auto-check (task 0199). Throttled
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
