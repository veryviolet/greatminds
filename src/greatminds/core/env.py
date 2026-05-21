"""Python-env-manager detection + activation for the launcher targets.

When ``greatminds launch`` creates a tmux session or VS Code tasks
file, every window/task needs the ``greatminds`` binary reachable on
its PATH. That requires activating the project's Python env before
the launcher invocation.

This module:

1. **Detects** which env manager the project uses, by walking a priority
   ladder of filesystem markers (most-specific first):

       pixi.toml / pixi.lock  → pixi
       uv.lock                → uv
       poetry.lock            → poetry
       environment.y[a]ml     → conda  (name from ``name:`` in yaml)
       .venv/bin/activate     → venv (plain ``python -m venv``)
       venv/bin/activate      → venv

   Then falls back to inherited parent-shell env vars:

       $VIRTUAL_ENV set       → external-venv  (user already sourced one)
       $CONDA_PREFIX +
       $CONDA_DEFAULT_ENV     → external-conda (user already activated)

   Else returns ``None`` — system-only PATH (e.g. ``pipx install``).

2. **Renders** a shell activation command for each detected env. The
   command is sent verbatim into each tmux window before the pre-typed
   launcher line, or wrapped around the launcher in VS Code task
   ``command`` strings via ``bash -c '<act>; <launcher>'``.

3. **Verifies** the setup actually puts ``greatminds`` on PATH —
   runs ``bash -c "<act>; command -v greatminds"`` and reports
   the resolved binary path (or a structured failure for the hint
   printer).

External override: callers may pass an explicit venv directory which
short-circuits detection and forces ``source <venv>/bin/activate``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class EnvSetup:
    """Result of detect+activate for one project.

    ``env_type`` is the detected env-manager (or ``None`` for system).
    ``activation`` is the shell snippet that activates it (empty string
    for system / when no activation is needed). ``source`` is a human-
    readable diagnostic of what triggered the detection.
    """
    env_type: str | None
    activation: str
    source: str


def _detect(project_dir: Path, venv_override: Path | None) -> EnvSetup:
    """Auto-detect the env manager for ``project_dir`` or honour an explicit
    ``venv_override`` (typically from a ``--venv`` CLI flag).
    """
    if venv_override is not None:
        venv = venv_override.resolve()
        activate = venv / "bin" / "activate"
        if not activate.is_file():
            return EnvSetup(
                env_type="override",
                activation="",
                source=f"--venv {venv} (BUT {activate} not found)",
            )
        return EnvSetup(
            env_type="override",
            activation=f"source {venv}/bin/activate",
            source=f"--venv {venv}",
        )

    p = project_dir
    if (p / "pixi.toml").is_file() or (p / "pixi.lock").is_file():
        return EnvSetup(
            env_type="pixi",
            activation=f'eval "$(pixi shell-hook --manifest-path {p}/pixi.toml)"',
            source=f"{p}/pixi.toml or pixi.lock present",
        )
    if (p / "uv.lock").is_file():
        return EnvSetup(
            env_type="uv",
            activation=f"source {p}/.venv/bin/activate",
            source=f"{p}/uv.lock present",
        )
    if (p / "poetry.lock").is_file():
        # -C <dir> works whether or not virtualenvs.in-project is set.
        return EnvSetup(
            env_type="poetry",
            activation=f'source "$(poetry env info -C {p} --path)/bin/activate"',
            source=f"{p}/poetry.lock present",
        )
    for yml_name in ("environment.yml", "environment.yaml"):
        env_yml = p / yml_name
        if env_yml.is_file():
            try:
                data = yaml.safe_load(env_yml.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                data = {}
            name = (data.get("name") or "base").strip() or "base"
            return EnvSetup(
                env_type="conda",
                activation=(
                    'source "$(conda info --base)/etc/profile.d/conda.sh" && '
                    f'conda activate {name}'
                ),
                source=f"{env_yml} (env name: {name})",
            )
    for venv_name in (".venv", "venv"):
        if (p / venv_name / "bin" / "activate").is_file():
            return EnvSetup(
                env_type="venv",
                activation=f"source {p}/{venv_name}/bin/activate",
                source=f"{p}/{venv_name}/bin/activate present (no lock files)",
            )

    # No project-local markers — fall back to parent-shell env vars so that
    # ``source ~/some/external/.venv/bin/activate`` propagates into tmux
    # windows / VS Code tasks even if the tmux server was started earlier.
    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env:
        return EnvSetup(
            env_type="external-venv",
            activation=f"source {venv_env}/bin/activate",
            source=f"$VIRTUAL_ENV={venv_env}",
        )
    conda_prefix = os.environ.get("CONDA_PREFIX")
    conda_name = os.environ.get("CONDA_DEFAULT_ENV")
    if conda_prefix and conda_name:
        return EnvSetup(
            env_type="external-conda",
            activation=(
                'source "$(conda info --base)/etc/profile.d/conda.sh" && '
                f'conda activate {conda_name}'
            ),
            source=f"$CONDA_DEFAULT_ENV={conda_name}",
        )

    return EnvSetup(
        env_type=None,
        activation="",
        source="no project markers, no $VIRTUAL_ENV, no $CONDA_*",
    )


def detect(project_dir: Path, venv_override: Path | None = None) -> EnvSetup:
    """Public API: detect env for ``project_dir``, honour ``venv_override``."""
    return _detect(project_dir, venv_override)


def verify(setup: EnvSetup) -> tuple[bool, str]:
    """Run a child shell with ``setup.activation`` and check that
    ``greatminds`` is on PATH afterward.

    Returns ``(ok, message)``:
      - ok == True  → message is the absolute path to greatminds.
      - ok == False → message describes why it was not found.
    """
    cmd = (
        f"{setup.activation}; command -v greatminds"
        if setup.activation
        else "command -v greatminds"
    )
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    path = proc.stdout.strip()
    if proc.returncode == 0 and path:
        return True, path
    return False, proc.stderr.strip() or "greatminds not found"


def fail_hint(project_dir: Path, setup: EnvSetup) -> str:
    """Format a helpful error message when ``verify`` failed.

    Shows what was detected, what env vars are set, and the recovery options.
    """
    lines = [
        "no python env detected and greatminds not on PATH.",
        "",
        "To fix, choose one:",
        "  --venv /path/to/.venv         pass an explicit venv (must have bin/greatminds)",
        "  --env-manager pixi|uv|...     force a specific manager (TODO: not yet implemented)",
        "  pipx install greatminds       install globally so PATH always has it",
        "  source /path/to/.venv/bin/activate    before running this command",
        "",
        f"Detection result for {project_dir}:",
        f"  env_type:  {setup.env_type or '(system / none)'}",
        f"  source:    {setup.source}",
        "",
        "Parent-shell env:",
        f"  VIRTUAL_ENV:       {os.environ.get('VIRTUAL_ENV') or '(not set)'}",
        f"  CONDA_DEFAULT_ENV: {os.environ.get('CONDA_DEFAULT_ENV') or '(not set)'}",
    ]
    return "\n".join(lines)
