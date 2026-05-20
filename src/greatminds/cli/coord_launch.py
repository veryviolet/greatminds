"""coord-launch — multi-frontend launcher for the coordination fleet.

Currently supports:

  ``tmux``           Same as ``greatminds-coord-tmux`` — build a tmux session,
                     one window per role, agent launcher pre-typed. Use that
                     binary directly if you only need the tmux target.

  ``vscode``         Generate ``.vscode/tasks.json`` and a workspace file at
                     the project root. One task per agent role, each agent
                     runs in its own dedicated terminal panel. User opens
                     the project with ``code .`` then launches tasks via
                     ``Cmd+Shift+P → Tasks: Run Task → agent: <role>``.

  ``cursor-ide``     Same generated files as ``vscode`` (Cursor IDE is a
                     VS Code fork and reads the same ``.vscode/`` config).
                     Prints ``cursor .`` as the open command instead.

The IDE targets are written as a foundation — terminals work, env vars
propagate, but advanced features like auto-attach to existing sessions or
panel grouping are best-effort. PRs welcome.

Usage::

    greatminds-coord-launch --target {tmux,vscode,cursor-ide} [--config <coord.yaml>] [--project-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import yaml

from greatminds.core.util import die


# Status colour for the agent terminals in VS Code (cycled per agent so they
# stand out side-by-side). Names from VS Code's standard theme colour set.
VSCODE_PANEL_COLOURS = [
    "terminal.ansiBlue",
    "terminal.ansiGreen",
    "terminal.ansiYellow",
    "terminal.ansiMagenta",
    "terminal.ansiCyan",
    "terminal.ansiRed",
]


def load_coord_yaml(cfg_path: Path) -> dict:
    """Parse a coord.yaml file or die with a clear error."""
    if not cfg_path.is_file():
        die(1, f"coord.yaml not found at {cfg_path} "
               "(pass --config or run from the project root)")
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        die(1, f"coord.yaml parse error: {exc}")
        raise SystemExit
    if not isinstance(data, dict):
        die(1, "coord.yaml root must be a mapping")
        raise SystemExit
    return data


def resolve_launcher() -> str:
    """Return the absolute path to ``greatminds-start-agent`` or die.

    Lookup order:
      1. Sibling of the current Python entry-point (same venv) — robust
         when the user invokes us by full path without sourcing the venv.
      2. PATH lookup via ``shutil.which`` (pipx / system install).

    IDE-target tasks reference this command verbatim, so we resolve it
    now to give a clear error rather than letting VS Code's task runner
    fail opaquely later.
    """
    # Don't ``.resolve()``: sys.executable in a uv-managed venv is a symlink
    # to the underlying Python interpreter; resolving it would skip past the
    # venv's bin dir entirely.
    sibling = Path(sys.executable).parent / "greatminds-start-agent"
    if sibling.is_file():
        return str(sibling)
    p = shutil.which("greatminds-start-agent")
    if p is not None:
        return p
    die(1, f"greatminds-start-agent not found next to {sys.executable} "
           "and not on PATH. Install greatminds (pip/pipx) first.")
    raise SystemExit


def emit_vscode(project_dir: Path, cfg: dict, *, ide_label: str) -> None:
    """Write ``.vscode/tasks.json`` (and a workspace file) for VS Code / Cursor IDE."""
    launcher = resolve_launcher()

    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        die(1, "coord.yaml: windows must be a non-empty list")

    vscode_dir = project_dir / ".vscode"
    vscode_dir.mkdir(exist_ok=True)

    tasks: list[dict] = []
    colour_idx = 0
    for w in windows:
        name = w.get("name") or ""
        role = (w.get("role") or "").upper()
        tool = w.get("tool") or "bash"
        mode = w.get("mode") or ""
        if not name:
            die(1, f"window without name: {w}")

        # Bash / no-role windows: just open a project shell, no agent launch.
        if tool == "bash" or not role:
            task = {
                "label": f"shell: {name}",
                "type": "shell",
                "command": "${env:SHELL}",
                "presentation": {
                    "echo": True,
                    "reveal": "always",
                    "panel": "dedicated",
                    "group": "agents",
                    "showReuseMessage": False,
                    "clear": False,
                },
                "options": {
                    "cwd": "${workspaceFolder}",
                    "env": {
                        "COORD_PROJECT_DIR": "${workspaceFolder}",
                    },
                },
                "problemMatcher": [],
                "runOptions": {"runOn": "default"},
            }
            tasks.append(task)
            continue

        cmd_args = [role, tool]
        if mode:
            cmd_args += ["--mode", mode]
        cmd_line = launcher + " " + " ".join(cmd_args)

        colour = VSCODE_PANEL_COLOURS[colour_idx % len(VSCODE_PANEL_COLOURS)]
        colour_idx += 1

        task = {
            "label": f"agent: {name}",
            "detail": f"{role} via {tool}" + (f" ({mode})" if mode else ""),
            "type": "shell",
            "command": cmd_line,
            "presentation": {
                "echo": True,
                "reveal": "always",
                "panel": "dedicated",
                "group": "agents",
                "showReuseMessage": False,
                "clear": False,
            },
            "options": {
                "cwd": "${workspaceFolder}",
                "env": {
                    "COORD_PROJECT_DIR": "${workspaceFolder}",
                    "COORD_ROLE": role,
                },
            },
            "problemMatcher": [],
            "runOptions": {"runOn": "default"},
            # VS Code allows colorising the terminal status bar per task.
            "icon": {"id": "terminal", "color": colour},
        }
        tasks.append(task)

    tasks_doc = {
        "version": "2.0.0",
        "tasks": tasks,
    }
    tasks_file = vscode_dir / "tasks.json"
    tasks_file.write_text(json.dumps(tasks_doc, indent=2) + "\n", encoding="utf-8")

    # Workspace file — opens VS Code with the project root and an attractive title.
    session_name = cfg.get("session") or "agents"
    workspace = {
        "folders": [{"path": "."}],
        "settings": {
            "window.title": f"{session_name} — greatminds fleet",
        },
    }
    ws_file = project_dir / f"{session_name}.code-workspace"
    ws_file.write_text(json.dumps(workspace, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {tasks_file.relative_to(project_dir)} "
          f"({len(tasks)} tasks)")
    print(f"wrote {ws_file.relative_to(project_dir)}")
    print()
    print(f"open in {ide_label}:")
    open_cmd = "code" if ide_label == "VS Code" else "cursor"
    print(f"    {open_cmd} {ws_file.name}")
    print()
    print("then launch each agent terminal via:")
    print("    Cmd/Ctrl+Shift+P → Tasks: Run Task → agent: <name>")
    print()
    print("each task opens a dedicated terminal panel with COORD_ROLE and "
          "COORD_PROJECT_DIR pre-exported.")


def emit_tmux_via_existing(args: argparse.Namespace) -> int:
    """Delegate to greatminds-coord-tmux (the existing tmux target)."""
    from greatminds.cli.coord_tmux import main as tmux_main

    fwd: list[str] = []
    if args.config is not None:
        fwd += ["--config", str(args.config)]
    if args.project_dir is not None:
        fwd += ["--project-dir", str(args.project_dir)]
    if args.recreate:
        fwd.append("--recreate")
    return tmux_main(fwd)


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-coord-launch`` in pyproject.toml."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--target", choices=["tmux", "vscode", "cursor-ide"],
                    default="tmux",
                    help="frontend to launch the fleet in (default: tmux)")
    ap.add_argument("--config", type=Path,
                    help="path to coord.yaml (default: <project>/coord.yaml)")
    ap.add_argument("--project-dir", type=Path,
                    help="override config.project_dir / cwd")
    ap.add_argument("--recreate", action="store_true",
                    help="(tmux target only) kill existing session and rebuild")
    args = ap.parse_args(argv)

    if args.target == "tmux":
        return emit_tmux_via_existing(args)

    # vscode / cursor-ide: locate coord.yaml.
    cfg_path = args.config
    if cfg_path is None:
        for p in (Path.cwd() / "coord.yaml",
                  Path.cwd() / "coordination" / "coord.yaml"):
            if p.is_file():
                cfg_path = p
                break
    if cfg_path is None or not cfg_path.is_file():
        die(1, "coord.yaml not found (pass --config or run from project root)")
        return 1

    cfg = load_coord_yaml(cfg_path)
    project_dir = (args.project_dir or Path(cfg.get("project_dir") or ".")).resolve()
    if not project_dir.is_dir():
        die(1, f"project_dir {project_dir} not found")
        return 1
    if not (project_dir / "coordination").is_dir():
        die(1, f"{project_dir}/coordination/ not found "
               "(run greatminds-coord-init first)")
        return 1

    ide_label = "VS Code" if args.target == "vscode" else "Cursor IDE"
    emit_vscode(project_dir, cfg, ide_label=ide_label)
    return 0


if __name__ == "__main__":
    sys.exit(main())
