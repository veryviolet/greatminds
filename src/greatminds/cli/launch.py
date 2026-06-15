"""greatminds launch — start the fleet (tmux | vscode | cursor-ide).

Reads ``<project>/coordination/coord.yaml``, detects the project's Python env
via ``greatminds.core.env``, and:

  ``--target tmux``        creates a tmux session with one window per
                           role. Each window pre-types the env activation
                           command (so bare ``greatminds start-agent``
                           works in the shell) followed by the actual
                           launcher line (without Enter — you review and
                           confirm).

  ``--target vscode``      writes ``.vscode/tasks.json`` + a workspace
                           file. Each task launches the agent in a
                           dedicated terminal panel; the task ``env``
                           block (for simple envs) or wrapping
                           ``bash -c`` (for pixi/conda) ensures
                           the unified ``greatminds`` CLI is on PATH.

  ``--target cursor-ide``  same generated files as ``vscode`` (Cursor
                           IDE reads ``.vscode/`` verbatim); prints
                           ``cursor`` as the open command.

Env auto-detection ladder:
  pixi.toml | uv.lock | poetry.lock | environment.yml | .venv/bin/activate
  → fallback to ``$VIRTUAL_ENV`` / ``$CONDA_DEFAULT_ENV`` from parent shell
  → system PATH only (e.g. pipx).

Pass ``--venv /path`` to force ``source <path>/bin/activate`` and skip
detection (useful when no marker files describe the env).
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import click
import yaml

from greatminds.core import env as gm_env
from greatminds.core.paths import coord_yaml_path, project_config_dir, project_env_file
from greatminds.cli._colors import err, header, info, ok, warn


VSCODE_PANEL_COLOURS = [
    "terminal.ansiBlue",
    "terminal.ansiGreen",
    "terminal.ansiYellow",
    "terminal.ansiMagenta",
    "terminal.ansiCyan",
    "terminal.ansiRed",
]


def _load_coord_yaml(cfg_path: Path) -> dict:
    if not cfg_path.is_file():
        err(f"coord.yaml not found at {cfg_path} (pass --config or run from project root)")
        raise click.exceptions.Exit(1)
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        err(f"coord.yaml parse error: {exc}")
        raise click.exceptions.Exit(1)
    if not isinstance(data, dict):
        err("coord.yaml root must be a mapping")
        raise click.exceptions.Exit(1)
    return data


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def _launch_command(launcher: str, role: str, tool: str, mode: str) -> str:
    """0308: shared builder for the start-agent command line.

    Both ``launch.py`` (initial fleet spin-up) and ``restart.py``
    (resurrection of a dead agent in an existing pane) call this so
    the two paths produce IDENTICAL command strings. Pre-0308
    ``restart`` sent only a bare Enter and relied on the
    ``_wrapper_loop`` resident in the pane to re-exec the agent;
    that wrapper is gone now, so restart must build the full
    command itself.
    """
    cmd = f"{launcher} {role} {tool}"
    if mode:
        cmd += f" --mode {mode}"
    return cmd


# 0308: the launcher no longer installs the wrapper-loop in each
# pane. ``_emit_tmux`` sends the launch_command + Enter directly
# (preceded by ``C-u`` to clear any leftover bash input).
# ``restart.py`` mirrors that sequence when resurrecting a dead
# agent. The constants + ``_wrapper_loop`` function are retained
# in dormant form for transitional fixtures and external scripts
# that still import them; future task moves the circuit-breaker
# semantics to ``restart.py`` / watchdog (per-role attempt-count
# tracking across invocations).
CIRCUIT_BREAKER_FAILS = 3
CIRCUIT_BREAKER_WINDOW_SEC = 30


def _wrapper_loop(launch_cmd: str, role: str) -> str:
    """0160 wrapper loop — DEPRECATED in 0308.

    Kept as a no-op stub so test fixtures + external scripts that
    still import the symbol don't break. The launcher no longer
    installs the wrapper in each pane; ``_emit_tmux`` sends
    ``launch_cmd`` + Enter directly.

    Pre-0160 docstring (preserved for historical context):

    Wrapper shape (one line, no embedded newlines)::

      while true; do
        printf 'press Enter to (re)start <ROLE>...';
        read -r _ </dev/tty;
        t0=$(date +%s);
        <launch_cmd>;
        rc=$?;
        if [ "$rc" -ne 0 ]; then
          # 0164 circuit breaker
          now=$(date +%s);
          if [ $((now - last_fail_window_start)) -gt 30 ]; then
            last_fail_window_start=$now; fails=0;
          fi;
          fails=$((fails + 1));
          if [ "$fails" -ge 3 ]; then
            printf 'agent failed %s times in %ss; STOPPING wrapper. '...;
            exit 1;
          fi;
        fi;
      done

    Pre-0160 ``_emit_tmux`` pre-typed ``<launch_cmd>`` with no Enter,
    and the operator typed Enter once to start the agent. When the
    agent exited, the pane reverted to a bare bash prompt with no
    command to re-execute, and ``greatminds restart``'s
    ``tmux send-keys Enter`` had nothing to trigger.

    0164: added a circuit-breaker that exits the loop after
    ``CIRCUIT_BREAKER_FAILS`` consecutive non-zero exits within a
    ``CIRCUIT_BREAKER_WINDOW_SEC`` window. Without this, a chronically
    broken agent (missing codex binary, stale session UUID before the
    discover_codex_session fix landed, etc.) spammed the pane and the
    operator's logs forever. With the breaker, the pane stops with a
    clear recovery hint after 3 rapid failures.

    Single line (no embedded newlines) so ``tmux send-keys`` delivers
    it as one keystroke sequence — bash then enters the loop on Enter.
    """
    n = CIRCUIT_BREAKER_FAILS
    w = CIRCUIT_BREAKER_WINDOW_SEC
    return (
        f"fails=0; last_fail_window_start=0; "
        f"while true; do "
        f"printf 'press Enter to (re)start {role}...'; "
        f"read -r _ </dev/tty; "
        f"{launch_cmd}; "
        f"rc=$?; "
        f"if [ \"$rc\" -ne 0 ]; then "
        f"now=$(date +%s); "
        f"if [ $((now - last_fail_window_start)) -gt {w} ]; then "
        f"last_fail_window_start=$now; fails=0; "
        f"fi; "
        f"fails=$((fails + 1)); "
        f"if [ \"$fails\" -ge {n} ]; then "
        f"printf 'agent {role} failed %s times in %ss; STOPPING wrapper. "
        f"To force fresh session: GREATMINDS_FRESH=1 greatminds start-agent "
        f"{role} <TOOL> --mode loop\\n' \"$fails\" \"{w}\"; "
        f"exit 1; "
        f"fi; "
        f"else "
        f"fails=0; "
        f"fi; "
        f"done"
    )


def _configure_status_line(session: str) -> None:
    """Per-session tmux status-line config, applied at launch so a fresh
    host's fleet looks right WITHOUT relying on the operator's global
    ~/.tmux.conf.

    Two things this fixes, both mandatory on deploy:
      - status-left-length: tmux defaults to 10, which truncates a
        session name like ``greatminds-dev`` (``[greatminds-dev] `` is
        17 chars) so the clipped title collides with the window list
        (``0:planner`` …). We size it to ``len(session) + 4`` so the
        full bracketed title fits exactly and windows start cleanly
        after it.
      - colors: the established fleet theme (purple bg / white fg, the
        current window bold+underscored). Set per-session so every fleet
        on the host looks identical regardless of personal tmux config.
    """
    left_len = len(session) + 4  # "[<session>] " is len+3; +1 margin
    for name, val in (
        ("status-left", "[#S] "),
        ("status-left-length", str(left_len)),
        ("status-style", "bg=colour54 fg=white"),
        ("window-status-current-style",
         "bg=colour54 fg=white,bold,underscore"),
    ):
        _tmux("set-option", "-t", session, name, val)


def _emit_tmux(project_dir: Path, cfg: dict, setup: gm_env.EnvSetup,
               recreate: bool) -> None:
    session = cfg.get("session") or "agents"
    all_windows = cfg.get("windows") or []
    if not isinstance(all_windows, list) or not all_windows:
        err("coord.yaml: windows must be a non-empty list")
        raise click.exceptions.Exit(1)

    # Driven roles have NO tmux pane; coordd runs each turn as a managed
    # subprocess (claude -p / codex app-server) and captures output to
    # .greatminds/.turns/. The tmux session is only the human-facing
    # resident panes: interactive roles, self-loop MAINTAINER, the
    # dashboard, and any bare bash window. Driven entries stay in
    # coord.yaml so coordd can read their tool — they just don't get a
    # window. (coord.yaml still drives coordd's per-role tool lookup.)
    windows = [w for w in all_windows
               if (w.get("mode") or "").lower() != "driven"]
    if not windows:
        err("coord.yaml: no non-driven (pane) windows to create")
        raise click.exceptions.Exit(1)

    # tmux is required on PATH.
    if not subprocess.run(["which", "tmux"], capture_output=True).returncode == 0:
        err("tmux not installed (apt install tmux)")
        raise click.exceptions.Exit(1)

    # Does a session already exist?
    has = _tmux("has-session", "-t", session)
    if has.returncode == 0:
        if recreate:
            _tmux("kill-session", "-t", session)
            info(f"killed existing session '{session}'")
        else:
            warn(f"session '{session}' already exists.")
            info(f"  attach:   tmux a -t {session}")
            info(f"  recreate: greatminds launch --target tmux --recreate")
            return

    launcher = "greatminds start-agent"

    first = True
    for w in windows:
        name = w.get("name") or ""
        role = (w.get("role") or "").upper()
        tool = w.get("tool") or "bash"
        mode = w.get("mode") or ""
        if not name:
            err(f"window without name: {w}")
            raise click.exceptions.Exit(1)

        # bash/no-role windows just open a project shell with env activated.
        # 0318 (0311 Phase 2d): driven roles (mode=driven) run NO
        # persistent agent — coordd spawns each turn via
        # ``claude --resume -p`` and the pane stays idle bash between
        # turns. So launch leaves the pane idle (no start-agent send);
        # the first inbox/queue event after launch triggers coordd's
        # driven dispatch (force-fresh session on the first turn).
        if mode == "dashboard":
            # The dashboard pane runs the read-only live status table
            # (agents / tasks / stand). Role-less, registers nothing —
            # a pure observer. Auto-run it + Enter like any resident pane.
            launch_cmd = "greatminds dashboard"
        elif mode == "logs":
            # Chronological, read-only driven-agent event stream. Role-less
            # observer pane; coordd writes the events, this command only tails.
            launch_cmd = "greatminds driven-log --follow"
        elif tool == "bash" or not role or mode == "driven":
            launch_cmd = ""
        elif mode == "staged":
            # Interactive, USER-started role (LIVE-DEVELOPER): pre-type the
            # chat-mode start-agent command but do NOT submit it (below) —
            # the USER starts/stops the session.
            launch_cmd = _launch_command(launcher, role, tool, "chat")
        else:
            launch_cmd = _launch_command(launcher, role, tool, mode)

        if first:
            cp = _tmux("new-session", "-d", "-s", session, "-n", name,
                       "-c", str(project_dir))
            first = False
        else:
            cp = _tmux("new-window", "-t", session, "-n", name,
                       "-c", str(project_dir))
        if cp.returncode != 0:
            err(f"tmux session/window create failed: {cp.stderr.strip()}")
            raise click.exceptions.Exit(1)

        # 1. GREATMINDS_ROLE export — so hooks and scripts identify the window.
        if role:
            _tmux("send-keys", "-t", f"{session}:{name}",
                  f"export GREATMINDS_ROLE={role}", "Enter")
        # 2. Activate the project's Python env so PATH has greatminds.
        if setup.activation:
            _tmux("send-keys", "-t", f"{session}:{name}",
                  setup.activation, "Enter")
        # 2.5. Source the captured machine auth/session env and PROJECT.env
        # into the window so interactive panes and driven daemon turns see
        # the same runtime inputs. PROJECT.env is fleet config; agent-env is
        # private per-session tool auth captured by `greatminds daemon`.
        # Windows open with -c project_dir so the relative PROJECT.env path
        # resolves.
        if role:
            agent_env = (
                Path.home() / ".config" / "greatminds" / "agent-env"
                / f"{session}.env"
            )
            _tmux("send-keys", "-t", f"{session}:{name}",
                  f"set -a; [ -f {shlex.quote(str(agent_env))} ] && "
                  f". {shlex.quote(str(agent_env))}; "
                  f"[ -f {shlex.quote(str(project_env_file(project_dir)))} ] && "
                  f". {shlex.quote(str(project_env_file(project_dir)))}; "
                  "set +a", "Enter")
        # 3. Emit the launch command directly. ``restart.py`` mirrors this
        # exact sequence when resurrecting a dead agent in an existing pane.
        if launch_cmd:
            _tmux("send-keys", "-t", f"{session}:{name}", "C-u")
            if mode == "staged":
                # Pre-type only — no Enter. The command sits in the pane
                # ready; the USER presses Enter to start the live session.
                _tmux("send-keys", "-t", f"{session}:{name}", launch_cmd)
            else:
                _tmux("send-keys", "-t", f"{session}:{name}",
                      launch_cmd, "Enter")

    _configure_status_line(session)
    _tmux("select-window", "-t", f"{session}:0")

    ok(f"\nsession '{session}' created with {len(windows)} windows.")
    info(f"  attach:   tmux a -t {session}")
    info(f"  detach:   Ctrl+B d")
    info(f"  switch:   Ctrl+B <num>   or   Ctrl+B w (list)")
    info(f"\neach window: env activated ({setup.env_type or 'system'}).")


def _emit_vscode(project_dir: Path, cfg: dict, setup: gm_env.EnvSetup,
                 ide_label: str) -> None:
    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        err("coord.yaml: windows must be a non-empty list")
        raise click.exceptions.Exit(1)

    vscode_dir = project_dir / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    tasks: list[dict] = []
    launcher = "greatminds start-agent"

    for i, w in enumerate(windows):
        name = w.get("name") or ""
        role = (w.get("role") or "").upper()
        tool = w.get("tool") or "bash"
        mode = w.get("mode") or ""
        if not name:
            err(f"window without name: {w}")
            raise click.exceptions.Exit(1)

        if tool == "bash" or not role:
            task = {
                "label": f"shell: {name}",
                "type": "shell",
                "command": "${env:SHELL}",
                "options": {
                    "cwd": "${workspaceFolder}",
                    "env": {"GREATMINDS_PROJECT_DIR": "${workspaceFolder}"},
                },
                "presentation": {
                    "echo": True, "reveal": "always", "panel": "dedicated",
                    "group": "agents", "showReuseMessage": False, "clear": False,
                },
                "problemMatcher": [],
            }
            tasks.append(task)
            continue

        bare_launch = _launch_command(launcher, role, tool, mode)
        # Wrap launcher in `bash -c '<activation>; <launcher>'` so the env is
        # active for the child shell (works uniformly across pixi/uv/poetry/
        # conda/venv/external/system).
        if setup.activation:
            full_cmd = f"bash -c '{setup.activation}; {bare_launch}'"
        else:
            full_cmd = f"bash -c '{bare_launch}'"

        colour = VSCODE_PANEL_COLOURS[i % len(VSCODE_PANEL_COLOURS)]
        task = {
            "label": f"agent: {name}",
            "detail": f"{role} via {tool}" + (f" ({mode})" if mode else ""),
            "type": "shell",
            "command": full_cmd,
            "options": {
                "cwd": "${workspaceFolder}",
                "env": {
                    "GREATMINDS_PROJECT_DIR": "${workspaceFolder}",
                    "GREATMINDS_ROLE": role,
                },
            },
            "presentation": {
                "echo": True, "reveal": "always", "panel": "dedicated",
                "group": "agents", "showReuseMessage": False, "clear": False,
            },
            "problemMatcher": [],
            "icon": {"id": "terminal", "color": colour},
        }
        tasks.append(task)

    tasks_doc = {"version": "2.0.0", "tasks": tasks}
    tasks_file = vscode_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")

    session_name = cfg.get("session") or "agents"
    workspace = {
        "folders": [{"path": "."}],
        "settings": {"window.title": f"{session_name} — greatminds fleet"},
    }
    ws_file = project_dir / f"{session_name}.code-workspace"
    ws_file.write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")

    ok(f"wrote {tasks_file.relative_to(project_dir)} ({len(tasks)} tasks)")
    ok(f"wrote {ws_file.relative_to(project_dir)}")
    info(f"\nopen in {ide_label}:")
    open_cmd = "code" if ide_label == "VS Code" else "cursor"
    info(f"    {open_cmd} {ws_file.name}")
    info("\nthen launch each agent terminal via:")
    info("    Cmd/Ctrl+Shift+P → Tasks: Run Task → agent: <name>")


@click.command(short_help="start the fleet (tmux | vscode | cursor-ide)",
               help=__doc__)
@click.option("--target", default="tmux",
              type=click.Choice(["tmux", "vscode", "cursor-ide"]),
              help="frontend (default: tmux)")
@click.option("--config", "config_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="path to coord.yaml (default: <project>/coordination/coord.yaml)")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="override config.project_dir / cwd")
@click.option("--venv", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="explicit venv to activate (skips auto-detect)")
@click.option("--recreate", is_flag=True,
              help="(tmux target only) kill existing session and rebuild")
def launch(target: str, config_path: Path | None, project_dir: Path | None,
           venv: Path | None, recreate: bool) -> None:
    # Locate coord.yaml.
    if config_path is None:
        p = coord_yaml_path(Path.cwd())
        if p.is_file():
            config_path = p
    if config_path is None or not config_path.is_file():
        err("coord.yaml not found (pass --config or run from project root)")
        raise click.exceptions.Exit(1)

    cfg = _load_coord_yaml(config_path)
    project_dir = (project_dir or Path(cfg.get("project_dir") or ".")).resolve()
    if not project_dir.is_dir():
        err(f"project_dir {project_dir} not found")
        raise click.exceptions.Exit(1)
    if not project_config_dir(project_dir).is_dir():
        err(f"{project_config_dir(project_dir)} not found (run greatminds setup first)")
        raise click.exceptions.Exit(1)

    # Detect env + verify greatminds-task reachable after activation.
    setup = gm_env.detect(project_dir, venv_override=venv)
    ok_, msg = gm_env.verify(setup)
    if not ok_:
        err(gm_env.fail_hint(project_dir, setup))
        err(f"\nverify error: {msg}")
        raise click.exceptions.Exit(1)

    header(f"detected env:  {setup.env_type or 'system'}  ({setup.source})")
    info(f"greatminds binary: {msg}")

    if target == "tmux":
        _emit_tmux(project_dir, cfg, setup, recreate)
    elif target == "vscode":
        _emit_vscode(project_dir, cfg, setup, ide_label="VS Code")
    elif target == "cursor-ide":
        _emit_vscode(project_dir, cfg, setup, ide_label="Cursor IDE")
    else:
        err(f"unknown target: {target}")
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    launch()
