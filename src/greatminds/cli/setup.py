"""Initialize an ACP project through the shared deterministic bootstrap."""
from pathlib import Path
import json
import click


@click.command(short_help="initialize an ACP project")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--execution-config", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Explicit ACP manifests and role bindings; otherwise create an empty contract.")
def setup(project_dir: Path | None, execution_config: Path | None = None) -> None:
    from greatminds.runtime.bootstrap import bootstrap
    project = (project_dir or Path.cwd()).resolve()
    result = bootstrap(project, execution_config)
    result['next_step'] = 'Configure agents and bindings in coordination/execution.yaml, then run greatminds coordd.'
    click.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    setup()
