"""Launch daemon and operator surfaces for ACP projects."""
import json
from pathlib import Path

import click

from greatminds.core.paths import find_project_dir
from greatminds.runtime.frontends import launch as launch_acp


@click.command(short_help="open ACP daemon and operator frontends")
@click.option("--target", default="tmux", type=click.Choice(["tmux", "vscode", "cursor-ide"]))
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--venv", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Python environment containing Greatminds; defaults to the current interpreter.")
def launch(target: str, project_dir: Path | None, venv: Path | None) -> None:
    project = find_project_dir(project_dir or Path.cwd(), strict=False, use_env=project_dir is None)
    click.echo(json.dumps(launch_acp(project, target=target, venv=venv), indent=2))


if __name__ == "__main__":
    launch()
