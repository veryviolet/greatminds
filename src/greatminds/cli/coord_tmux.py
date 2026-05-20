#!/usr/bin/env python3
"""coord-tmux — build a tmux session from coord.yaml.

Reads <project>/coord.yaml (or path passed via --config), creates the
session, one window per role with COORD_ROLE exported, project_dir set
as cwd, and the start_agent command pre-filled (without Enter — you
review and press Enter yourself).

Idempotent: if the session already exists, prints attach instructions
and exits 0. Use --recreate to kill and rebuild.

Usage:
  coord-tmux [--config <path>] [--project-dir <dir>] [--recreate]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from greatminds.core.util import die


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    """Entry point — wired up as ``greatminds-coord-tmux`` in pyproject.toml."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path,
                    help="path to coord.yaml (default: <project>/coord.yaml)")
    ap.add_argument("--project-dir", type=Path,
                    help="override config.project_dir")
    ap.add_argument("--recreate", action="store_true",
                    help="kill existing session and rebuild")
    args = ap.parse_args(argv)

    cfg_path = args.config
    if cfg_path is None:
        # try cwd/coord.yaml, then <cwd>/coordination/coord.yaml
        for p in (Path.cwd() / "coord.yaml",
                  Path.cwd() / "coordination" / "coord.yaml"):
            if p.is_file():
                cfg_path = p
                break
    if cfg_path is None or not cfg_path.is_file():
        die(1, "coord.yaml not found (pass --config or run from project root)")

    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        die(1, f"yaml parse error: {exc}")
    if not isinstance(cfg, dict):
        die(1, "coord.yaml root must be a mapping")

    session = cfg.get("session") or "agents"
    project_dir = Path(args.project_dir or cfg.get("project_dir") or ".").resolve()
    if not (project_dir / "coordination").is_dir():
        die(1, f"{project_dir}/coordination/ not found; run bin/coord-init first")

    windows = cfg.get("windows") or []
    if not isinstance(windows, list) or not windows:
        die(1, "coord.yaml: windows must be a non-empty list")

    # Locate the launcher command.
    # 1. Project-local override: <project>/bin/start_agent (legacy shim).
    # 2. greatminds-start-agent installed in PATH (preferred — pip/pipx install).
    project_start = project_dir / "bin" / "start_agent"
    if project_start.is_file():
        start_cmd = str(project_start)
    else:
        on_path = shutil.which("greatminds-start-agent")
        if on_path:
            start_cmd = on_path
        else:
            die(1,
                "no launcher found: neither <project>/bin/start_agent nor "
                "greatminds-start-agent on PATH. "
                "Install greatminds (pip/pipx) or provide a project shim.")
            return 1  # unreachable

    # Existing session?
    cp = tmux("has-session", "-t", session)
    if cp.returncode == 0:
        if args.recreate:
            tmux("kill-session", "-t", session)
            print(f"killed existing session '{session}'")
        else:
            print(f"session '{session}' already exists.")
            print(f"  attach:   tmux a -t {session}")
            print(f"  recreate: {sys.argv[0]} --recreate")
            return 0

    first = True
    for w in windows:
        name = w.get("name") or ""
        role = (w.get("role") or "").upper()
        tool = w.get("tool") or "bash"
        mode = w.get("mode") or ""
        if not name:
            die(1, f"window without name: {w}")
        # Build the command we pre-type into the shell (no Enter).
        if tool == "bash" or not role:
            prefill = ""
        else:
            # Use the resolved launcher path so the user can see exactly
            # which binary will run (project-local shim vs. PATH-installed
            # greatminds-start-agent).
            prefill = f"{start_cmd} {role} {tool}"
            if mode:
                prefill += f" --mode {mode}"

        if first:
            cp = tmux("new-session", "-d", "-s", session, "-n", name,
                      "-c", str(project_dir))
            if cp.returncode != 0:
                die(1, f"tmux new-session failed: {cp.stderr.strip()}")
            first = False
        else:
            cp = tmux("new-window", "-t", session, "-n", name,
                      "-c", str(project_dir))
            if cp.returncode != 0:
                die(1, f"tmux new-window failed: {cp.stderr.strip()}")

        # Export COORD_ROLE (and press Enter)
        if role:
            tmux("send-keys", "-t", f"{session}:{name}",
                 f"export COORD_ROLE={role}", "Enter")
        if prefill:
            # pre-type without Enter — user reviews and confirms
            tmux("send-keys", "-t", f"{session}:{name}", prefill)

    tmux("select-window", "-t", f"{session}:0")

    print(f"session '{session}' created with {len(windows)} windows.")
    print(f"  attach:   tmux a -t {session}")
    print(f"  detach:   Ctrl+B d")
    print(f"  switch:   Ctrl+B <num>   or   Ctrl+B w (interactive list)")
    print()
    print("each agent window has 'bin/start_agent <ROLE> <tool>' pre-typed.")
    print("review, tweak tool if needed, press Enter to start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
