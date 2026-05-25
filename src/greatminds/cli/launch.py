"""greatminds launch — start the fleet (tmux | vscode | cursor-ide).

Reads ``<project>/coord.yaml``, detects the project's Python env via
``greatminds.core.env``, and:

  ``--target tmux``        creates a tmux session with one window per
                           role. Each window pre-types the env activation
                           command (so bare ``greatminds-start-agent``
                           works in the shell) followed by the actual
                           launcher line (without Enter — you review and
                           confirm).

  ``--target vscode``      writes ``.vscode/tasks.json`` + a workspace
                           file. Each task launches the agent in a
                           dedicated terminal panel; the task ``env``
                           block (for simple envs) or wrapping
                           ``bash -c`` (for pixi/conda) ensures
                           greatminds-* is on PATH.

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
import subprocess
import sys
from pathlib import Path

import click
import yaml

from greatminds.core import env as gm_env
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
    cmd = f"{launcher} {role} {tool}"
    if mode:
        cmd += f" --mode {mode}"
    return cmd


def _wrapper_loop(launch_cmd: str, role: str) -> str:
    """0160: bash one-liner that loops on Enter and re-execs the agent.

    Wrapper shape::

      while true; do printf 'press Enter to (re)start <ROLE>...';
        read -r _ </dev/tty; <launch_cmd>; done

    Pre-0160 ``_emit_tmux`` pre-typed ``<launch_cmd>`` with no Enter,
    and the operator typed Enter once to start the agent. When the
    agent exited, the pane reverted to a bare bash prompt with no
    command to re-execute, and ``greatminds restart``'s
    ``tmux send-keys Enter`` had nothing to trigger.

    With this wrapper installed, ``restart``'s Enter into the pane
    lands at the wrapper's ``read``, and the next iteration of the
    loop re-runs the agent. Operator-visible: pane shows
    ``press Enter to (re)start <ROLE>...`` after every agent exit.

    Single line (no embedded newlines) so ``tmux send-keys`` delivers
    it as one keystroke sequence — bash then enters the loop on Enter.
    """
    return (
        f"while true; do "
        f"printf 'press Enter to (re)start {role}...'; "
        f"read -r _ </dev/tty; "
        f"{launch_cmd}; "
        f"done"
    )


def _emit_tmux(project_dir: Path, cfg: dict, setup: gm_env.EnvSetup,
               recreate: bool) -> None:
    session = cfg.get("session") or "agents"
    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        err("coord.yaml: windows must be a non-empty list")
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
        if tool == "bash" or not role:
            wrapper = ""
        else:
            launch_cmd = _launch_command(launcher, role, tool, mode)
            wrapper = _wrapper_loop(launch_cmd, role)

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
        # 3. 0160: install the wrapper loop and start it (with trailing
        # Enter). The wrapper prints ``press Enter to (re)start ...``
        # and blocks at ``read -r _ </dev/tty``. Operator's first Enter
        # launches the first agent; subsequent Enter (manual OR from
        # ``greatminds restart``'s send-keys) launches each successive
        # instance after the previous one exits. Pre-0160 this path
        # pre-typed ``launch_cmd`` with NO trailing Enter — which made
        # ``restart`` a no-op for any pane whose agent had already
        # exited (the pane reverted to a bare bash prompt with no
        # command to re-execute).
        if wrapper:
            _tmux("send-keys", "-t", f"{session}:{name}", wrapper, "Enter")

    _tmux("select-window", "-t", f"{session}:0")

    ok(f"\nsession '{session}' created with {len(windows)} windows.")
    info(f"  attach:   tmux a -t {session}")
    info(f"  detach:   Ctrl+B d")
    info(f"  switch:   Ctrl+B <num>   or   Ctrl+B w (list)")
    info(f"\neach window: env activated ({setup.env_type or 'system'}), "
         f"wrapper-loop installed that re-runs '{launcher} <ROLE> <tool>' "
         f"on Enter.")
    info("press Enter in each window to start the first agent. "
         "Subsequent agent exits print 'press Enter to (re)start ...' "
         "and wait — `greatminds restart` lands on that prompt.")


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
              default=None, help="path to coord.yaml (default: <project>/coord.yaml)")
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
        for p in (Path.cwd() / "coord.yaml",
                  Path.cwd() / "coordination" / "coord.yaml"):
            if p.is_file():
                config_path = p
                break
    if config_path is None or not config_path.is_file():
        err("coord.yaml not found (pass --config or run from project root)")
        raise click.exceptions.Exit(1)

    cfg = _load_coord_yaml(config_path)
    project_dir = (project_dir or Path(cfg.get("project_dir") or ".")).resolve()
    if not project_dir.is_dir():
        err(f"project_dir {project_dir} not found")
        raise click.exceptions.Exit(1)
    if not (project_dir / "coordination").is_dir():
        err(f"{project_dir}/coordination/ not found (run greatminds setup first)")
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
