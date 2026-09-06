"""CLI entrypoint for the common ACP execution daemon."""
from pathlib import Path
import click
from greatminds.core.paths import find_project_dir


@click.command(name="coordd", short_help="run the ACP execution daemon")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--project", "project_name", default=None,
              help="Project name registered by greatminds daemon install.")
@click.option("--interval-sec", type=click.FloatRange(min=0.2), default=1.0)
@click.option("--verbose", "-v", is_flag=True)
@click.option("--once", is_flag=True, help="Reconcile and execute one dispatch batch, then exit.")
def coordd(project_dir: Path | None, project_name: str | None,
           interval_sec: float, verbose: bool, once: bool = False) -> None:
    if project_dir is None and project_name:
        from greatminds.cli.daemon import lookup_project_dir
        project_dir = lookup_project_dir(project_name)
        if project_dir is None:
            raise click.ClickException(f"no project registered as {project_name!r}")
    project_dir = find_project_dir(project_dir or Path.cwd(), strict=False,
                                   use_env=project_dir is None)
    import asyncio
    from greatminds.runtime.daemon import serve
    asyncio.run(serve(project_dir, interval=interval_sec, once=once))


if __name__ == "__main__":
    coordd()
