# Cutting a Release

Release mechanics depend on the project using greatminds. For greatminds
itself, the package release path is:

1. Ensure implementation, tests, docs, and review tasks are verified.
2. Run the full test suite.
3. Build the package.
4. Tag the release.
5. Publish through the configured package workflow.
6. Upgrade the fleet environment that runs agents.
7. Restart daemon and agent processes.

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
