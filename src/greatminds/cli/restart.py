"""greatminds restart — idempotent fleet restart.

Replaces the temporary ``restart_fleet.sh`` bash crutch with a tested
Python implementation. Same external behavior, same order:

  1. ``systemctl --user is-active --quiet coordd``; if not, start it.
  2. ``tmux has-session -t <session>``; if missing, shell out to
     ``greatminds launch --target tmux``.
  3. For each window in ``coord.yaml`` with a non-empty ``role``,
     resolve ``coordination/.agent_registry/<role-lowercase>.json``:
       - missing / pid dead / no pid → ``tmux send-keys Enter`` to
         (re)launch the agent. ``greatminds launch`` (since 0160)
         installs a bash wrapper-loop in each pane that prints
         ``press Enter to (re)start <ROLE>...`` and blocks at
         ``read -r _ </dev/tty`` after each agent exit; ``restart``'s
         Enter lands on that ``read`` and the wrapper re-execs the
         agent. Pre-0160 ``launch.py`` pre-typed the start-agent
         command with no Enter and ``restart`` was a no-op for any
         pane whose agent had already exited.
       - alive → skip.
  4. Wait 10s, then re-read each registry. A role passes if the file
     exists, ``pid`` is alive (``os.kill(pid, 0)``), and ``input_sock``
     is present. Exit 0 if all roles pass; exit 1 otherwise.

Usage::

    greatminds restart [--config <coord.yaml>] [--project-dir <dir>]

Linux + systemd-user only, same as the bash version.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import click
import yaml

from greatminds.cli._colors import err, info


VERIFY_WAIT_SEC = 10


def _resolve_session_default(project_dir: Path) -> str:
    """Generic fallback session name when coord.yaml is missing or has no
    ``session:`` key. Prefer ``basename(project_dir)`` for uniqueness across
    projects on the same machine; degrade to ``"agents"`` (the convention used
    elsewhere in this CLI, see ``launch.py``) only if basename resolves empty.
    """
    try:
        name = project_dir.resolve().name
    except (OSError, RuntimeError):
        name = ""
    return name or "agents"


def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    info(f"[{ts}] {msg}")


def _load_coord_yaml(path: Path) -> dict:
    if not path.is_file():
        err(f"coord.yaml not found at {path}")
        raise click.exceptions.Exit(1)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(f"coord.yaml parse error: {exc}")
        raise click.exceptions.Exit(1)
    if not isinstance(data, dict):
        err("coord.yaml root must be a mapping")
        raise click.exceptions.Exit(1)
    return data


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, text=True,
    )


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# task 0048: trust-prompt detection. Fresh-deploy claude/codex windows can
# stop at a "Do you trust this folder?" dialog before the role contract
# starts. The window LOOKS alive (tool process running, registry has pid)
# but injected wakes go to the dialog/shell, not to the role. We scan the
# tmux pane content for the known prompt strings and surface the state.
# Scope (per plan 0048): detect + report. Wake-blocking and auto-trust
# config are explicit follow-ups, not in this task's surface.
# ---------------------------------------------------------------------------

TRUST_PROMPT_PATTERNS: tuple[str, ...] = (
    "Do you trust the files in this folder",
    "Do you trust this folder",
    "Trust this workspace",
    "Trust this directory",
    # codex's variants — exact wording may vary by version; keep loose
    # substring matches that won't false-positive on normal agent output.
    "Allow Codex to run",
    "trust this project",
)


def _capture_pane(session: str, window: str) -> str:
    """Return current tmux pane text for ``<session>:<window>``.

    Empty string on tmux failure (window gone, session missing, etc.).
    """
    cp = _tmux("capture-pane", "-t", f"{session}:{window}", "-p", "-J")
    if cp.returncode != 0:
        return ""
    return cp.stdout or ""


def _detect_trust_state(pane_text: str) -> str:
    """Classify a pane: ``"pending-trust"`` if any known trust-prompt
    string appears, else ``"ready"``."""
    if not pane_text:
        return "ready"
    for needle in TRUST_PROMPT_PATTERNS:
        if needle in pane_text:
            return "pending-trust"
    return "ready"


def _systemd_unit_file_exists(unit: str) -> bool:
    """True if systemctl --user has a unit FILE matching ``unit``. Used
    to detect installed template units (e.g. ``greatminds-daemon@.service``)
    and legacy singletons (``coordd.service``)."""
    cp = _systemctl("list-unit-files", "--no-legend", unit)
    if cp.returncode != 0:
        return False
    return bool(cp.stdout.strip())


# task 0055: candidates the per-project daemon mechanism can be served
# by. Each entry is ``(template_unit_to_check, runtime_unit_to_start)``.
# - per-project template: list-unit-files shows ``greatminds-daemon@.service``
#   (NOT the instance — systemd's template unit is the file on disk);
#   start the instance ``greatminds-daemon@<session>.service`` which
#   systemd instantiates from the template at runtime. (REVIEWER caught
#   the iter-1 mismatch here.)
# - legacy singleton: list-unit-files AND start use the same name.
def _daemon_candidates(session: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if session:
        out.append(("greatminds-daemon@.service",
                    f"greatminds-daemon@{session}.service"))
    out.append(("coordd.service", "coordd.service"))
    return out


def _ensure_coordd(session: str | None = None) -> None:
    """Ensure the coordd daemon is running for this project.

    Per-project units (template ``greatminds-daemon@.service`` →
    instance ``greatminds-daemon@<session>.service``, task 0008) are
    preferred. Legacy singleton ``coordd.service`` is a fallback.
    **Critical (task 0055):** if no daemon unit file exists at all,
    log a WARN and RETURN — restart's primary value is the tmux
    Enter recovery path, which must still fire. Earlier behavior
    (exit 1 on missing coordd.service) was a regression from the
    1.2.0 daemon refactor.
    """
    _log("==> coordd: ensure running")
    candidates = _daemon_candidates(session)

    # First pass: any candidate's runtime instance already active?
    for _template, runtime in candidates:
        if _systemctl("is-active", "--quiet", runtime).returncode == 0:
            main_pid = _systemctl(
                "show", "-p", "MainPID", "--value", runtime,
            ).stdout.strip()
            _log(f"    {runtime}: active ({main_pid})")
            return

    # Second pass: for each candidate whose UNIT FILE exists on disk,
    # try to start its runtime instance. Template units don't carry
    # instance-name in their filename — list-unit-files reports the
    # template (e.g. ``greatminds-daemon@.service``) while ``systemctl
    # start`` takes the instance name (``greatminds-daemon@<session>.service``).
    for template, runtime in candidates:
        if not _systemd_unit_file_exists(template):
            continue
        _log(f"    {runtime}: not active, starting from template "
             f"{template}...")
        started = _systemctl("start", runtime)
        if started.returncode != 0:
            _log(
                f"    WARN: systemctl --user start {runtime} failed: "
                f"{started.stderr.strip()}"
            )
            continue
        time.sleep(1)
        if _systemctl("is-active", "--quiet", runtime).returncode == 0:
            main_pid = _systemctl(
                "show", "-p", "MainPID", "--value", runtime,
            ).stdout.strip()
            _log(f"    {runtime}: active ({main_pid})")
            return
        _log(
            f"    WARN: {runtime} failed to come up — "
            f"inspect `journalctl --user -u {runtime}`"
        )

    # No daemon unit available — degrade gracefully. tmux Enter recovery
    # below is the user-visible value of `greatminds restart`; the daemon
    # is for nudges, not required for restart to do its primary job.
    _log(
        "    WARN: no coordd unit installed for this project. "
        "Continuing with tmux Enter recovery. Install via: "
        "`greatminds daemon install`"
    )


def _ensure_tmux_session(session: str, project_dir: Path) -> None:
    _log(f"==> tmux session {session}: ensure exists")
    if _tmux("has-session", "-t", session).returncode == 0:
        _log(f"    session {session} already exists")
        return
    _log("    session missing, calling greatminds launch --target tmux")
    launched = subprocess.run(
        ["greatminds", "launch", "--target", "tmux"],
        cwd=str(project_dir),
        capture_output=True, text=True,
    )
    if launched.returncode != 0:
        err(
            "    ERROR: greatminds launch --target tmux failed: "
            f"{launched.stderr.strip()}"
        )
        raise click.exceptions.Exit(1)
    time.sleep(1)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _load_registry(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _iter_role_windows(windows: list[dict]) -> list[tuple[str, str]]:
    """Yield (window_name, role_lower) pairs, skipping windows with empty role."""
    out: list[tuple[str, str]] = []
    for w in windows:
        if not isinstance(w, dict):
            continue
        name = (w.get("name") or "").strip()
        role = (w.get("role") or "").strip()
        if not name or not role:
            continue
        out.append((name, role.lower()))
    return out


def _soft_inject_render_role(
    session: str,
    window: str,
    role: str,
) -> tuple[bool, str]:
    """0147 ``--bootstrap`` (soft): inject the role's freshly-rendered
    canon into the live tmux pane and submit with Enter. The agent
    keeps running; its next reply incorporates the new canon.
    Session-id files are NOT touched; the agent's pid is NOT killed;
    claude ``--resume`` / codex resume continuity is preserved.

    Mechanism: shell out to ``greatminds render-role <ROLE>`` to
    capture the rendered prompt (the same text the agent saw on its
    original bootstrap), load it into a uniquely-named tmux buffer,
    and ``paste-buffer -p`` (bracketed paste) into the pane. Then
    ``send-keys Enter`` submits. Bracketed paste preserves multi-line
    text as a single paste — without ``-p`` each embedded newline
    might be interpreted as the agent's submit key, fragmenting the
    canon across many turns.

    Returns ``(ok, diag)``. Soft-inject failures are non-fatal — the
    caller logs and continues to the next role; this is operator
    convenience, not a correctness gate.
    """
    proc = subprocess.run(
        ["greatminds", "render-role", role],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return False, (
            f"render-role {role} failed: rc={proc.returncode} "
            f"stderr={proc.stderr.strip()[:120]}"
        )
    rendered = proc.stdout.rstrip() + "\n"
    buf_name = f"gm-bootstrap-{role.lower()}"
    load = subprocess.run(
        ["tmux", "load-buffer", "-b", buf_name, "-"],
        input=rendered, text=True, capture_output=True,
    )
    if load.returncode != 0:
        return False, f"tmux load-buffer failed: {load.stderr.strip()[:120]}"
    paste = subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name, "-t",
         f"{session}:{window}", "-p"],
        capture_output=True, text=True,
    )
    if paste.returncode != 0:
        return False, f"tmux paste-buffer failed: {paste.stderr.strip()[:120]}"
    submit = subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:{window}", "Enter"],
        capture_output=True, text=True,
    )
    if submit.returncode != 0:
        return False, f"tmux send-keys Enter failed: {submit.stderr.strip()[:120]}"
    return True, f"render-role injected ({len(rendered)} chars) + Enter"


def _sigterm_alive(pid: int) -> None:
    """0147 ``--reset`` (the destructive path, formerly ``--bootstrap``
    in 0137): kill an alive agent so its tmux wrapper returns to the
    start-agent prompt.

    Only meaningful for alive pids; the 1s sleep gives the wrapper
    time to notice the child died. The verify step's 10s wait absorbs
    any residual lag.
    """
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    time.sleep(1.0)


def _clear_session_files_for_bootstrap(
    registry_dir: Path,
    role_lc: str,
) -> None:
    """0147 ``--reset`` (the destructive path, formerly ``--bootstrap``
    in 0137): clear the role's pid registry AND tool-specific
    session-id files unconditionally for every role being relaunched.

    With these cleared, the next ``start_agent`` reads no prior
    session UUID for claude and no prior rollout for codex, so the
    next launch goes through the fresh-session code path (no
    ``--resume``, no ``codex resume <sid>``). The soft ``--bootstrap``
    path never calls this — soft preserves session continuity.
    """
    for fname in (
        f"{role_lc}.json",                # pid + input_sock
        f"{role_lc}.session-id",          # claude session UUID
        f"{role_lc}.codex-session-id",    # codex rollout UUID
    ):
        try:
            (registry_dir / fname).unlink()
        except OSError:
            pass


def _restart_dead_agents(
    registry_dir: Path,
    windows: list[dict],
    session: str,
    bootstrap: bool = False,
    reset: bool = False,
) -> None:
    _log("==> agents: check + restart dead ones")
    coord_dir = registry_dir.parent
    # task 0051: per-window Enter goes through the unified primitive in
    # _send_enter.press_enter so the same code path / capture-pane verify
    # / per-agent-type key sequence is shared with coordd's stalled-sweep
    # and journal-event wakes. The agent_type comes from coord.yaml's
    # window definition (tool: claude|codex|cursor).
    from greatminds.cli._send_enter import press_enter  # local import: avoid cycle
    window_tool = {w.get("name"): (w.get("tool") or "claude")
                   for w in windows if isinstance(w, dict) and w.get("name")}
    for name, role_lc in _iter_role_windows(windows):
        reg_path = registry_dir / f"{role_lc}.json"
        data = _load_registry(reg_path)
        pid = 0
        needs_start = False
        if data is None:
            needs_start = True
        else:
            try:
                pid = int(data.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if not _pid_alive(pid):
                try:
                    reg_path.unlink()
                except OSError:
                    pass
                needs_start = True
            elif bootstrap:
                # 0147 ``--bootstrap`` (soft): the alive agent is NOT
                # killed. Inject the freshly-rendered role canon into
                # the live tmux pane and submit; the agent's next
                # reply reads the new canon. Session-id files stay
                # put → claude --resume / codex resume continuity
                # preserved across this operation. The role with this
                # branch does NOT need_start (the agent is already
                # running; we just nudged it).
                ok, diag = _soft_inject_render_role(session, name,
                                                   role_lc.upper())
                if ok:
                    _log(f"    {name} ({role_lc}): pid={pid} alive — "
                         f"--bootstrap (soft): {diag}")
                else:
                    _log(f"    {name} ({role_lc}): pid={pid} alive — "
                         f"--bootstrap (soft) FAILED: {diag}")
                continue
            elif reset:
                # 0147 ``--reset`` (the destructive path, formerly
                # 0137 ``--bootstrap``): SIGTERM the alive pid; the
                # session-file clearing below + Enter relaunch gives a
                # genuinely fresh agent. Use case: agent context
                # unrecoverably corrupt, canon-format-incompatible
                # version bump, intentional state-bust. NOT the
                # default upgrade procedure — that's --bootstrap.
                _log(f"    {name} ({role_lc}): pid={pid} alive — "
                     f"--reset: SIGTERM + fresh re-launch")
                _sigterm_alive(pid)
                pid = 0
                needs_start = True

        # 0147: --reset (destructive) is the only path that clears
        # session-id files. --bootstrap (soft) preserves them by
        # design — the alive branch above returns before reaching
        # here, and the dead/missing-registry branches inside
        # --bootstrap don't need fresh sessions either (the agent is
        # already gone; the next launch just bootstraps as it would
        # for any newly-started role, using whatever session-id
        # already exists or rolling a new one if absent).
        if needs_start and reset:
            _clear_session_files_for_bootstrap(registry_dir, role_lc)
        if needs_start:
            agent_type = (window_tool.get(name) or "claude").lower()
            _log(f"    {name} ({role_lc}): pressing Enter to (re)start "
                 f"[agent_type={agent_type}]")
            # mode="bare-enter": tmux ran the launcher shell with a
            # pre-filled `greatminds start-agent` line; a bare Enter
            # accepts it. No role heartbeat to poll — the agent isn't
            # running yet. verify=False because the dedicated _verify()
            # step below does the full pid+sock+trust check.
            ok, diag = press_enter(
                coord_dir, session, name, role_lc, agent_type,
                mode="bare-enter",
                verify=False,
            )
            _log(f"      {diag}")
            time.sleep(0.5)
        else:
            _log(f"    {name} ({role_lc}): pid={pid} alive, skip")


def _verify(
    registry_dir: Path,
    windows: list[dict],
    wait_sec: int,
    session: str,
) -> int:
    _log(f"==> waiting {wait_sec}s for pty_launch sockets to bind...")
    time.sleep(wait_sec)
    _log("==> final registry state:")
    fail = 0
    total = 0
    pending_trust: list[str] = []
    for name, role_lc in _iter_role_windows(windows):
        total += 1
        reg_path = registry_dir / f"{role_lc}.json"
        data = _load_registry(reg_path)
        if data is None:
            click.echo(f"    {role_lc:<22} MISSING")
            fail += 1
            continue
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        has_sock = "YES" if "input_sock" in data else "NO"
        alive = "alive" if _pid_alive(pid) else "DEAD"

        # task 0048: a window with pid + sock can still be sitting at the
        # tool's "Do you trust this folder?" dialog with the role contract
        # NOT yet running. Scan the pane content to surface this state —
        # registry alone lies about readiness in that case.
        trust_state = _detect_trust_state(_capture_pane(session, name))
        trust_col = "TRUST?" if trust_state == "pending-trust" else "ok"

        click.echo(
            f"    {role_lc:<22} pid={pid:<7} {alive:<6} "
            f"input_sock={has_sock} trust={trust_col}"
        )
        if has_sock != "YES":
            fail += 1
        if alive != "alive":
            fail += 1
        if trust_state == "pending-trust":
            fail += 1
            pending_trust.append(f"{session}:{name} ({role_lc})")

    click.echo()
    if fail == 0:
        _log(f"==> ALL {total} agents up with input_sock bound. Fleet ready.")
        _log(f"    attach with: tmux a -t {session}")
        return 0
    if pending_trust:
        _log(
            f"==> {len(pending_trust)} role(s) stuck at tool-trust prompt — "
            f"open the window in tmux and confirm the dialog:"
        )
        for w in pending_trust:
            _log(f"      {w}")
        _log(f"    tmux a -t {session}   # then walk through each, confirm trust, /resume")
    _log(f"==> {fail} role(s) failed to come up clean. Inspect:")
    _log(f"    tmux a -t {session}")
    _log("    journalctl --user -u coordd | tail -30")
    return 1


@click.command(
    "restart",
    short_help="idempotent fleet restart (coordd + tmux session + agents)",
    help=__doc__,
)
@click.option(
    "--config", "config_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="path to coord.yaml (default: <project>/coord.yaml)",
)
@click.option(
    "--project-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="override config.project_dir / cwd",
)
@click.option(
    "--bootstrap",
    is_flag=True,
    default=False,
    help=("soft canon refresh for alive agents (0147). Renders the "
          "role canon via `greatminds render-role` and pastes it into "
          "the live tmux pane via bracketed paste, then submits with "
          "Enter. The agent keeps running — session-id files are NOT "
          "touched, pid is NOT killed, claude --resume / codex resume "
          "continuity is preserved. This is the canonical post-PyPI-"
          "upgrade procedure: `pip install -U greatminds && greatminds "
          "restart --bootstrap`. Mutually exclusive with --reset."),
)
@click.option(
    "--reset",
    is_flag=True,
    default=False,
    help=("destructive re-launch (0147, formerly 0137 --bootstrap). "
          "SIGTERMs each alive pid and clears claude/codex session-id "
          "files so the next start-agent goes through the fresh-session "
          "path. Use when the agent's context is unrecoverably corrupt "
          "or a canon-format-incompatible version bump requires a "
          "genuine state-bust. NOT the default upgrade procedure — "
          "that's --bootstrap. Mutually exclusive with --bootstrap."),
)
def restart(
    config_path: Path | None,
    project_dir: Path | None,
    bootstrap: bool,
    reset: bool,
) -> None:
    if config_path is None:
        for p in (Path.cwd() / "coord.yaml",
                  Path.cwd() / "coordination" / "coord.yaml"):
            if p.is_file():
                config_path = p
                break
    if config_path is None or not config_path.is_file():
        err("coord.yaml not found (pass --config or run from project root)")
        raise click.exceptions.Exit(1)

    cfg = _load_coord_yaml(config_path)
    project_dir = (
        project_dir or Path(cfg.get("project_dir") or ".")
    ).resolve()
    if not project_dir.is_dir():
        err(f"project_dir {project_dir} not found")
        raise click.exceptions.Exit(1)

    session = cfg.get("session") or _resolve_session_default(project_dir)
    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        err("coord.yaml: windows must be a non-empty list")
        raise click.exceptions.Exit(1)

    registry_dir = project_dir / "coordination" / ".agent_registry"

    if bootstrap and reset:
        raise click.UsageError(
            "--bootstrap and --reset are mutually exclusive. Pick one: "
            "--bootstrap (soft canon refresh, preserves session) or "
            "--reset (destructive re-launch, drops session).",
        )

    _ensure_coordd(session)
    _ensure_tmux_session(session, project_dir)
    _restart_dead_agents(registry_dir, windows, session,
                         bootstrap=bootstrap, reset=reset)
    rc = _verify(registry_dir, windows, VERIFY_WAIT_SEC, session)
    if rc != 0:
        raise click.exceptions.Exit(rc)


if __name__ == "__main__":
    restart()
