"""start_agent — launch an agent for a coordination ROLE in a given TOOL.

Replaces the 353-line Bash original at /opt/coordination/bin/start_agent.
Same external contract; same env var surface; same exec semantics — the
tool process replaces this script via ``os.execvp``.

Usage::

    greatminds-start-agent <ROLE> <TOOL> [--mode loop|chat] [extra tool args...]

ROLE
    Role key from ``command_START.yaml`` (DEVELOPER, ARCHITECT-PLANNER,
    UI-DEVELOPER, EXPLORER, TESTER, …).

TOOL
    One of ``claude`` | ``codex`` | ``cursor``.

What it does:

1. Resolve project root from ``$GREATMINDS_PROJECT_DIR`` (or cwd).
2. Resolve canon (packaged ``greatminds.data``) via ``find_canon_dir``.
3. Export ``GREATMINDS_ROLE``, ``PROJECT_ROOT`` and source the optional
   ``$PROJECT/coordination/PROJECT.env`` (gitignored secrets file).
4. Manage the per-role registry under
   ``$PROJECT/coordination/.agent_registry/<role>.{json,session-id}``:
   refuse to start if another agent is alive (unless ``GREATMINDS_FORCE=1``);
   reuse the persistent session UUID for ``--resume`` semantics;
   rotate the UUID when ``GREATMINDS_FRESH=1``.
5. Render the bootstrap prompt via
   ``python -m greatminds.cli.render_role``. On resume, replace it with
   a short "continue your tick" nudge so the tool doesn't re-ingest the
   giant role spec.
6. Strip leading ``/loop`` for chat mode.
7. Set the terminal title (OSC 0) unless
   ``GREATMINDS_START_AGENT_NOTITLE=1``.
8. Per-tool branching:

   ``claude``
       Layered ``--plugin-dir`` (canon coordination-protocol → canon
       per-role → project overrides) and ``--mcp-config`` (canon →
       project local). New session: ``--session-id``. Resume:
       ``--resume``. ``--`` separator before the prompt so the variadic
       ``--mcp-config`` doesn't eat it.

   ``codex``
       Discovers the role's most recent rollout in
       ``~/.codex/sessions``; caches its UUID for ``codex resume``.
       Falls back to ``codex resume --last`` if no session found. Adds
       ``--profile-v2 <role-lower>`` when
       ``~/.codex/<role-lower>.config.toml`` exists.

   ``cursor``
       Wrapped in ``systemd-run --user --scope`` with memory/CPU caps
       (cursor-agent leaks memory in long sessions). Sets
       ``GREATMINDS_REGISTRY_TOOL=cursor`` so ``pty-launch`` records the
       logical tool, not ``systemd-run``.

Optional ``greatminds-pty-launch`` wraps the tool process in a pty we
control plus a unix socket so ``coordd`` can inject keystrokes from
outside the terminal emulator. Disabled via ``GREATMINDS_START_AGENT_NOPTY=1``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from greatminds.core.paths import find_canon_dir
from greatminds.core.util import die, now_iso


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """``True`` if ``pid`` exists; uses signal 0 (probe-only, no SIGKILL)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user — counts as alive.
        return True
    return True


def load_env_file(path: Path) -> None:
    """Source a simple ``KEY=value`` env file into ``os.environ``.

    Supports the subset Bash ``set -a; . file; set +a`` uses for
    coordination's PROJECT.env: shell-style ``KEY=value`` (one per line,
    optional ``export `` prefix), ``#`` comments, blank lines. Quotes
    around the value are stripped (single or double). Variable expansion
    is NOT performed — the values are recorded literally, matching the
    common dotenv convention.

    Silently no-ops if ``path`` is missing.
    """
    if not path.is_file():
        return
    line_re = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Strip a single surrounding quote pair, if present.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val


def render_prompt(role: str, project_dir: Path) -> str:
    """Run ``greatminds-render-role`` in a child process and return its stdout."""
    proc = subprocess.run(
        [
            sys.executable, "-m", "greatminds.cli.render_role",
            role, "--project-dir", str(project_dir),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        die(2, f"render-role failed for role {role}")
    return proc.stdout.rstrip()


def set_terminal_title(role: str) -> None:
    """Set the OSC 0 terminal title to ``role``; safe no-op if no tty."""
    try:
        with open("/dev/tty", "w") as fp:
            fp.write(f"\033]0;{role}\007")
            fp.flush()
    except OSError:
        pass


def tty_path() -> str:
    """Return the controlling tty's path, or ``"none"`` if not on a tty."""
    try:
        return os.ttyname(0)
    except OSError:
        return "none"


def discover_codex_session(role: str) -> str:
    """Find the most recent ``~/.codex/sessions/.../rollout-*.jsonl`` whose
    head contains ``"You are <ROLE> agent"`` — that's the bootstrap-intro phrase
    we send on first launch, so any file containing it belongs to this role.

    Returns the rollout's session UUID (extracted from the filename), or an
    empty string if none found.
    """
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return ""
    needle = f"You are {role} agent".encode("utf-8")
    name_re = re.compile(r"rollout-[0-9T-]+-([0-9a-f-]+)\.jsonl$")
    best_mtime = 0.0
    best_sid = ""
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            m = name_re.search(fname)
            if not m:
                continue
            fp = Path(dirpath) / fname
            try:
                with fp.open("rb") as f:
                    head = f.read(131072)
                if needle not in head:
                    continue
                mt = fp.stat().st_mtime
            except OSError:
                continue
            if mt > best_mtime:
                best_mtime = mt
                best_sid = m.group(1)
    return best_sid


# ---------------------------------------------------------------------------
# Tool-specific argv builders — return (cmd, env_overrides) where env_overrides
# is applied to os.environ before exec. Each builder may mutate cwd if needed.
# ---------------------------------------------------------------------------


def _yolo_args(tool: str) -> list[str]:
    if os.environ.get("GREATMINDS_START_AGENT_SAFE", "0") == "1":
        return []
    return {
        "claude": ["--permission-mode", "auto"],
        "codex":  ["-a", "never", "-s", "danger-full-access"],
        "cursor": ["--yolo", "--approve-mcps"],
    }.get(tool, [])


def build_claude_argv(
    role: str,
    canon_dir: Path,
    project_dir: Path,
    session_id: str,
    session_new: bool,
    extra: list[str],
    prompt: str,
) -> list[str]:
    """Compose ``claude --name R --session-id|--resume X [plugins] [mcp] -- PROMPT``."""
    # Plugin dirs are kebab-case. GREATMINDS_ROLE in this project is already
    # kebab-case (e.g. ARCHITECT-PLANNER, UI-DEVELOPER), so just lowercase.
    role_plugin_suffix = role.lower().replace("_", "-")
    plugin_dirs = [canon_dir / "plugins" / "coordination-protocol"]
    role_plugin = canon_dir / "plugins" / f"role-{role_plugin_suffix}"
    if role_plugin.is_dir():
        plugin_dirs.append(role_plugin)
    proj_overrides = project_dir / "coordination" / "plugins.local" / "project-overrides"
    if proj_overrides.is_dir():
        plugin_dirs.append(proj_overrides)

    mcp_files = [canon_dir / "mcp" / "canon.json"]
    mcp_local = project_dir / "coordination" / "mcp.local.json"
    if mcp_local.is_file():
        mcp_files.append(mcp_local)

    canon_args: list[str] = []
    for d in plugin_dirs:
        canon_args += ["--plugin-dir", str(d)]
    # claude's --mcp-config is variadic — it consumes following positional
    # args until the next --flag. We append a ``--`` separator below before
    # the prompt so PROMPT isn't misinterpreted as another config file.
    for f in mcp_files:
        canon_args += ["--mcp-config", str(f)]

    yolo = _yolo_args("claude")

    if session_new:
        session_args = ["--session-id", session_id]
    else:
        session_args = ["--resume", session_id]

    # The ``--`` separator goes AFTER user-supplied extra args so they
    # remain positional to claude itself; PROMPT then sits cleanly past it.
    return ["claude", "--name", role, *session_args, *yolo, *canon_args, *extra, "--", prompt]


def build_codex_argv(
    role: str,
    registry_dir: Path,
    session_new: bool,
    extra: list[str],
    prompt: str,
) -> list[str]:
    """Compose ``codex [resume <SID>|resume --last|] [profile-v2] EXTRA PROMPT``."""
    role_lower = role.lower()
    codex_session_file = registry_dir / f"{role_lower}.codex-session-id"

    codex_sid = ""
    if codex_session_file.is_file() and codex_session_file.stat().st_size > 0:
        codex_sid = codex_session_file.read_text(encoding="utf-8").strip()
    else:
        codex_sid = discover_codex_session(role)
        if codex_sid:
            codex_session_file.write_text(codex_sid + "\n", encoding="utf-8")

    codex_profile_args: list[str] = []
    profile_path = Path.home() / ".codex" / f"{role_lower}.config.toml"
    if profile_path.is_file():
        codex_profile_args = ["--profile-v2", role_lower]

    yolo = _yolo_args("codex")

    if codex_sid and not session_new:
        codex_args = ["resume", codex_sid, *yolo, *codex_profile_args]
    elif not session_new:
        # We're resuming but couldn't find a session id — fall back to --last
        # (the latest codex session in this cwd).
        codex_args = ["resume", "--last", *yolo, *codex_profile_args]
    else:
        codex_args = [*yolo, *codex_profile_args]

    return ["codex", *codex_args, *extra, prompt]


def build_cursor_argv(
    session_new: bool,
    extra: list[str],
    prompt: str,
) -> list[str]:
    """Compose ``systemd-run … cursor-agent [--continue] --model M EXTRA PROMPT``.

    The systemd-run wrapper isolates cursor's memory/CPU so its known
    long-session leaks OOM-kill only itself, not the host.
    """
    yolo = _yolo_args("cursor")
    cursor_model = os.environ.get("GREATMINDS_CURSOR_MODEL", "composer-2.5-fast")
    if session_new:
        cursor_args = ["--model", cursor_model, *yolo]
    else:
        cursor_args = ["--continue", "--model", cursor_model, *yolo]

    # pty-launch sees argv[2]=systemd-run; tell it the logical tool so the
    # registry says cursor (coordd picks the cursor submit sequence).
    os.environ["GREATMINDS_REGISTRY_TOOL"] = "cursor"

    sdr = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "-p", f"MemoryHigh={os.environ.get('GREATMINDS_CURSOR_MEM_HIGH', '3G')}",
        "-p", f"MemoryMax={os.environ.get('GREATMINDS_CURSOR_MEM_MAX', '4G')}",
        "-p", f"CPUQuota={os.environ.get('GREATMINDS_CURSOR_CPU', '300%')}",
    ]
    return [*sdr, "cursor-agent", *cursor_args, *extra, prompt]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


import click


@click.command(
    name="start-agent",
    short_help="launch ROLE as a TOOL agent (claude|codex|cursor)",
    help=__doc__,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("role")
@click.argument("tool", type=click.Choice(["claude", "codex", "cursor"]))
@click.option("--mode", default="loop", type=click.Choice(["loop", "chat"]),
              help="loop = self-driving tick loop; chat = interactive")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def start_agent(role: str, tool: str, mode: str, extra: tuple[str, ...]) -> None:
    extra = list(extra)
    project_dir = Path(os.environ.get("GREATMINDS_PROJECT_DIR") or os.getcwd()).resolve()
    canon_dir = find_canon_dir()

    # Export role identity so hooks (Stop / PostToolUse / …) can pick it up.
    os.environ["GREATMINDS_ROLE"] = role
    # Export PROJECT_ROOT so MCP servers (e.g. git ${PROJECT_ROOT}) resolve
    # project-relative paths via env-var substitution.
    os.environ["PROJECT_ROOT"] = str(project_dir)

    # Source per-project env vars (gitignored secrets file; optional).
    load_env_file(project_dir / "coordination" / "PROJECT.env")

    # Registry per role — coordd uses this to find our tty/socket.
    role_lower = role.lower()
    registry_dir = project_dir / "coordination" / ".agent_registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = registry_dir / f"{role_lower}.json"

    # Refuse to start if another agent is alive — unless GREATMINDS_FORCE=1.
    if registry_file.is_file() and registry_file.stat().st_size > 0:
        try:
            old = json.loads(registry_file.read_text(encoding="utf-8"))
            old_pid = old.get("pid")
            if isinstance(old_pid, int) and pid_alive(old_pid):
                if os.environ.get("GREATMINDS_FORCE", "0") != "1":
                    sys.stderr.write(
                        f"error: {role} is already running (pid {old_pid}).\n"
                        f"Close that terminal or run:  kill {old_pid}\n"
                        f"Then re-run this command. (GREATMINDS_FORCE=1 to override.)\n"
                    )
                    return 1
                sys.stderr.write("  GREATMINDS_FORCE=1 set — proceeding anyway. Tool may still error.\n")
        except (OSError, json.JSONDecodeError):
            pass

    # Persistent session id per role. First start → new UUID; subsequent
    # starts → reuse (so the tool can --resume the same conversation).
    # GREATMINDS_FRESH=1 rotates the UUID unconditionally.
    session_file = registry_dir / f"{role_lower}.session-id"
    session_new = True
    if session_file.is_file() and session_file.stat().st_size > 0:
        session_id = session_file.read_text(encoding="utf-8").strip()
        session_new = False
    else:
        session_id = str(uuid.uuid4())
        session_file.write_text(session_id + "\n", encoding="utf-8")
    if os.environ.get("GREATMINDS_FRESH", "0") == "1":
        session_id = str(uuid.uuid4())
        session_file.write_text(session_id + "\n", encoding="utf-8")
        session_new = True

    # Write registry. Cleanup on signal — best-effort; if we ``execvp`` the
    # tool, this process is replaced and the file outlives us. The next
    # start_agent call will detect the stale entry via pid_alive() above.
    registry_payload = {
        "role": role,
        "tool": tool,
        "pid": os.getpid(),
        "tty": tty_path(),
        "started_at": now_iso(),
        "session_id": session_id,
        "session_new": 1 if session_new else 0,
    }
    registry_file.write_text(json.dumps(registry_payload), encoding="utf-8")

    def _cleanup_registry(*_args) -> None:
        try:
            registry_file.unlink()
        except OSError:
            pass

    signal.signal(signal.SIGINT, lambda *_: (_cleanup_registry(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (_cleanup_registry(), sys.exit(143)))

    # Render the bootstrap prompt — on resume, replace with a short nudge.
    prompt = render_prompt(role, project_dir)
    if not session_new:
        prompt = f"continue your tick as {role} — you already know the contract"
    if mode == "chat" and prompt.startswith("/loop "):
        prompt = prompt[len("/loop "):]

    # Terminal title.
    if os.environ.get("GREATMINDS_START_AGENT_NOTITLE", "0") != "1":
        set_terminal_title(role)

    # pty-launch wrapper — exposes a unix socket that coordd writes to.
    use_pty = os.environ.get("GREATMINDS_START_AGENT_NOPTY", "0") != "1"
    pty_bin = shutil.which("greatminds-pty-launch") if use_pty else None
    if use_pty and pty_bin is None:
        # No PTY wrapper available — fall back to direct exec. coordd
        # keystroke injection won't work, but the agent itself runs fine.
        use_pty = False

    # Build the per-tool command.
    if tool == "claude":
        cmd = build_claude_argv(role, canon_dir, project_dir, session_id,
                                session_new, extra, prompt)
    elif tool == "codex":
        cmd = build_codex_argv(role, registry_dir, session_new, extra, prompt)
    elif tool == "cursor":
        cmd = build_cursor_argv(session_new, extra, prompt)
        # cursor-agent operates on cwd — change into the project root.
        os.chdir(project_dir)
    else:
        # argparse already validates choices; defensive guard for type-checkers.
        die(2, f"unknown TOOL: {tool}")
        return 2

    if use_pty:
        cmd = [pty_bin, role, *cmd]

    # exec — replace this process image with the tool. atexit/finally won't
    # fire past this point; the registry file is left in place and reaped
    # by the next start_agent's pid check.
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        _cleanup_registry()
        die(127, f"{cmd[0]}: command not found")


if __name__ == "__main__":
    start_agent()
