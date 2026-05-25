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
       ``--profile <role-lower>`` when
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

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import click

from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir
from greatminds.core.util import now_iso


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
        raise GreatMindsError(f"render-role failed for role {role}", exit_code=2)
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
    """Compose ``codex [resume <SID>|] [profile] EXTRA PROMPT``.

    Two-branch design driven by ``codex_sid`` (the per-role codex rollout
    UUID), NOT by ``session_new`` (which is the claude session-id concept;
    irrelevant to codex's own session storage):

      - codex_sid found → ``codex resume <sid> [yolo] [profile] EXTRA PROMPT``
        (resume the prior conversation; PROMPT is the wake nudge).
      - codex_sid empty → ``codex [yolo] [profile] EXTRA PROMPT``
        (fresh session; PROMPT is the full bootstrap).

    Task 0043 — EXPLORER avatar dogfood on codex 0.133.0 caught a stale
    ``codex resume --last`` fallback that codex 0.133.0 silently rejected:
    ``--last`` was parsed as an unknown flag, then the bootstrap prompt
    text (next positional) was treated as the missing session-id, causing
    ``No saved session found with ID continue your tick as DEVELOPER…``.
    Removed. When codex_sid is missing we now start a fresh session and
    let the role's bootstrap prompt (the full role intro) re-establish
    context — the agent re-bootstraps from canon role files instead of
    relying on a session continuity that codex couldn't provide anyway.
    """
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
        codex_profile_args = ["--profile", role_lower]

    yolo = _yolo_args("codex")

    if codex_sid:
        codex_args = ["resume", codex_sid, *yolo, *codex_profile_args]
    else:
        # No prior rollout found (e.g. first launch, or codex storage was
        # rotated/cleared, or codex self-updated and dropped the cache).
        # Start a fresh session — the bootstrap prompt carries everything
        # the role needs to re-establish context.
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


def _print_dry_run_report(
    *,
    role: str,
    tool: str,
    mode: str,
    project_dir: Path,
    canon_dir: Path,
    session_id: str,
    session_new: bool,
    session_file: Path,
    registry_file: Path,
    project_env_file: Path,
    cmd: list[str],
    cmd_with_pty: list[str] | None,
    prompt: str,
) -> None:
    """Print the effective config of a would-be start-agent invocation.

    Strictly stdout, strictly read-only — caller must guarantee no
    registry/session files were written.
    """
    out = sys.stdout.write

    out("DRY RUN — no side effects, no exec.\n\n")
    out(f"role:         {role}\n")
    out(f"tool:         {tool}\n")
    out(f"mode:         {mode}\n")
    out(f"project_dir:  {project_dir}\n")
    out(f"canon_dir:    {canon_dir}\n")
    sess_label = "NEW (would write)" if session_new else "RESUME (read from file)"
    out(f"session_id:   {session_id}  [{sess_label}]\n")
    out(f"session_file: {session_file}\n")
    out(f"registry:     {registry_file}\n")
    out(f"PROJECT.env:  {project_env_file}"
        f"{'' if project_env_file.is_file() else '  (not present, skipped)'}\n")
    out("\n")

    out("env that would be exported:\n")
    out(f"  GREATMINDS_ROLE={role}\n")
    out(f"  PROJECT_ROOT={project_dir}\n")
    out("  (+ any KEY=value pairs sourced from PROJECT.env, if present)\n")
    out("\n")

    # Plugin / MCP layers — Claude-specific (codex and cursor don't use
    # --plugin-dir / --mcp-config flags).
    if tool == "claude":
        role_plugin_suffix = role.lower().replace("_", "-")
        plugin_layers = [
            ("coordination-protocol (canon)",
             canon_dir / "plugins" / "coordination-protocol"),
            (f"role-{role_plugin_suffix} (canon)",
             canon_dir / "plugins" / f"role-{role_plugin_suffix}"),
            ("project-overrides (project)",
             project_dir / "coordination" / "plugins.local" / "project-overrides"),
        ]
        out("claude plugin layers (--plugin-dir each, in order):\n")
        for label, p in plugin_layers:
            present = "" if p.is_dir() else "  (not present, skipped)"
            out(f"  [{label}] {p}{present}\n")
        out("\n")

        mcp_layers = [
            ("canon", canon_dir / "mcp" / "canon.json"),
            ("project", project_dir / "coordination" / "mcp.local.json"),
        ]
        out("mcp config layers (--mcp-config each, in order):\n")
        for label, p in mcp_layers:
            present = "" if p.is_file() else "  (not present, skipped)"
            out(f"  [{label}] {p}{present}\n")
        out("\n")

    out(f"prompt (first line, len={len(prompt)}):\n")
    preview = prompt.splitlines()[0] if prompt else ""
    if len(preview) > 200:
        preview = preview[:200] + "…"
    out(f"  {preview}\n\n")

    out("argv (would exec):\n")
    out("  " + " ".join(_shell_quote(a) for a in cmd) + "\n\n")

    if cmd_with_pty is not None:
        out("argv with pty wrapper (would exec instead, default):\n")
        out("  " + " ".join(_shell_quote(a) for a in cmd_with_pty) + "\n\n")
        out("  (set GREATMINDS_START_AGENT_NOPTY=1 to skip the pty wrapper)\n")
    else:
        out("pty wrapper disabled (GREATMINDS_START_AGENT_NOPTY=1).\n")


def _shell_quote(arg: str) -> str:
    """Minimal shell-quoting for human-readable dry-run argv output."""
    if not arg or any(c in arg for c in " \t\n\"'\\$`#&|<>(){};*?[]"):
        return "'" + arg.replace("'", "'\\''") + "'"
    return arg


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
@click.option("--dry-run", is_flag=True, default=False,
              help="print the effective config (role, tool, plugin dirs, "
                   "mcp layers, final argv) and exit 0 without writing "
                   "to .agent_registry/ and without exec'ing the tool.")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def start_agent(role: str, tool: str, mode: str,
                dry_run: bool, extra: tuple[str, ...]) -> None:
    extra = list(extra)
    project_dir = Path(os.environ.get("GREATMINDS_PROJECT_DIR") or os.getcwd()).resolve()
    canon_dir = find_canon_dir()

    # Export role identity so hooks (Stop / PostToolUse / …) can pick it up.
    # Even in --dry-run we set these in-process: they don't escape this
    # python process (no exec) and the env block is shown in the report.
    os.environ["GREATMINDS_ROLE"] = role
    # Export PROJECT_ROOT so MCP servers (e.g. git ${PROJECT_ROOT}) resolve
    # project-relative paths via env-var substitution.
    os.environ["PROJECT_ROOT"] = str(project_dir)

    # Source per-project env vars (gitignored secrets file; optional).
    project_env_file = project_dir / "coordination" / "PROJECT.env"
    if not dry_run:
        load_env_file(project_env_file)

    # Registry per role — coordd uses this to find our tty/socket. In
    # --dry-run we compute paths but never mkdir or write.
    role_lower = role.lower()
    registry_dir = project_dir / "coordination" / ".agent_registry"
    if not dry_run:
        registry_dir.mkdir(parents=True, exist_ok=True)
    registry_file = registry_dir / f"{role_lower}.json"

    # Refuse to start if another agent is alive — unless GREATMINDS_FORCE=1.
    # In --dry-run we skip the check entirely: dry-run is for inspection,
    # not coordination, and we never write the registry anyway.
    if not dry_run and registry_file.is_file() and registry_file.stat().st_size > 0:
        try:
            old = json.loads(registry_file.read_text(encoding="utf-8"))
            old_pid = old.get("pid")
            if isinstance(old_pid, int) and pid_alive(old_pid):
                if os.environ.get("GREATMINDS_FORCE", "0") != "1":
                    raise GreatMindsError(
                        f"{role} is already running (pid {old_pid}). "
                        f"Close that terminal or run:  kill {old_pid}\n"
                        f"Then re-run this command. "
                        f"(GREATMINDS_FORCE=1 to override.)"
                    )
                sys.stderr.write(
                    "  GREATMINDS_FORCE=1 set — proceeding anyway. "
                    "Tool may still error.\n"
                )
        except (OSError, json.JSONDecodeError):
            pass

    # Persistent session id per role. First start → new UUID; subsequent
    # starts → reuse (so the tool can --resume the same conversation).
    # GREATMINDS_FRESH=1 rotates the UUID unconditionally.
    # In --dry-run we READ an existing session id if present (so the
    # report shows the real resume path), but NEVER write a new UUID.
    session_file = registry_dir / f"{role_lower}.session-id"
    session_new = True
    if session_file.is_file() and session_file.stat().st_size > 0:
        session_id = session_file.read_text(encoding="utf-8").strip()
        session_new = False
    else:
        session_id = str(uuid.uuid4())
        if not dry_run:
            session_file.write_text(session_id + "\n", encoding="utf-8")
    if os.environ.get("GREATMINDS_FRESH", "0") == "1":
        session_id = str(uuid.uuid4())
        if not dry_run:
            session_file.write_text(session_id + "\n", encoding="utf-8")
        session_new = True

    def _cleanup_registry(*_args) -> None:
        try:
            registry_file.unlink()
        except OSError:
            pass

    if not dry_run:
        signal.signal(signal.SIGINT,
                      lambda *_: (_cleanup_registry(), sys.exit(130)))
        signal.signal(signal.SIGTERM,
                      lambda *_: (_cleanup_registry(), sys.exit(143)))

    # Pre-pty registry: minimal record so a concurrent start_agent's
    # pid_alive() check above will refuse a duplicate launch. ``pty_launch``
    # (if wrapping) rewrites this file after pty.fork() with the actual
    # tool's pid AND ``input_sock``. If we skip pty wrapping
    # (GREATMINDS_START_AGENT_NOPTY=1), this minimal record is what coordd
    # sees — and coordd will fall back to /dev/pts writes (display only,
    # NOT input — wake keystrokes won't reach the agent, which is the
    # known limitation of running without pty-launch).
    if not dry_run:
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

    # Render the bootstrap prompt — on resume, replace with a short nudge.
    prompt = render_prompt(role, project_dir)
    if not session_new:
        prompt = f"continue your tick as {role} — you already know the contract"
    if mode == "chat" and prompt.startswith("/loop "):
        prompt = prompt[len("/loop "):]

    # Terminal title.
    if not dry_run and os.environ.get("GREATMINDS_START_AGENT_NOTITLE", "0") != "1":
        set_terminal_title(role)

    # pty-launch wrapper — exposes a unix socket that coordd writes to.
    # Invoked via ``python -m greatminds.cli.pty_launch`` rather than a
    # console-script binary: the 1.0.0 umbrella migration consolidated the
    # console scripts down to a single ``greatminds`` entry-point, so the
    # historical ``greatminds-pty-launch`` binary doesn't exist on PATH.
    # Same pattern is used for render-role above. Set
    # GREATMINDS_START_AGENT_NOPTY=1 to opt out (loses coordd keystroke
    # injection — wake messages then sit in inbox until the agent's own
    # ScheduleWakeup brings it back).
    use_pty = os.environ.get("GREATMINDS_START_AGENT_NOPTY", "0") != "1"

    # Build the per-tool command.
    if tool == "claude":
        cmd = build_claude_argv(role, canon_dir, project_dir, session_id,
                                session_new, extra, prompt)
    elif tool == "codex":
        cmd = build_codex_argv(role, registry_dir, session_new, extra, prompt)
    elif tool == "cursor":
        cmd = build_cursor_argv(session_new, extra, prompt)
        # cursor-agent operates on cwd — change into the project root.
        if not dry_run:
            os.chdir(project_dir)
    else:
        # click.Choice already validates; defensive guard for type-checkers.
        raise GreatMindsError(f"unknown TOOL: {tool}", exit_code=2)

    cmd_with_pty = (
        [sys.executable, "-m", "greatminds.cli.pty_launch", role, *cmd]
        if use_pty else None
    )

    if dry_run:
        _print_dry_run_report(
            role=role, tool=tool, mode=mode,
            project_dir=project_dir, canon_dir=canon_dir,
            session_id=session_id, session_new=session_new,
            session_file=session_file, registry_file=registry_file,
            project_env_file=project_env_file,
            cmd=cmd, cmd_with_pty=cmd_with_pty, prompt=prompt,
        )
        return

    if cmd_with_pty is not None:
        cmd = cmd_with_pty

    # exec — replace this process image with the tool. atexit/finally won't
    # fire past this point; the registry file is left in place and reaped
    # by the next start_agent's pid check.
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        _cleanup_registry()
        raise GreatMindsError(f"{cmd[0]}: command not found", exit_code=127)


if __name__ == "__main__":
    start_agent()
