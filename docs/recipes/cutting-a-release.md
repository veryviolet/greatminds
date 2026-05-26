# Cutting a Release

Release mechanics depend on the project using greatminds. For greatminds
itself, the package release path is:

1. Ensure implementation, tests, docs, and review tasks are verified.
2. Edit `pyproject.toml` `[project].version` to the release version.
3. Run the full test suite.
4. Build the package.
5. Tag the release.
6. Publish through the configured package workflow.
7. Upgrade the fleet environment that runs agents.
8. Restart daemon and agent processes.

`greatminds.__version__` is derived from installed package metadata with
`importlib.metadata.version("greatminds")`. Do not edit
`src/greatminds/__init__.py` during a release bump; the package version source of
truth is `pyproject.toml`.

Useful commands:

```bash
python -m pytest
python -m build
greatminds --version
greatminds daemon status
greatminds watchdog
```

Do not publish docs from this task path manually if a docs deploy workflow owns
GitHub Pages publication.
