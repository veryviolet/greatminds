# Cutting a Release

Release mechanics depend on the project using greatminds. For greatminds
itself, the package release path is:

1. Ensure implementation, tests, docs, and review tasks are verified.
2. Edit `pyproject.toml` `[project].version` to the release version.
3. Run the full test suite.
4. Build the package.
5. Tag the release.
6. Publish through the configured package workflow.
7. Upgrade the installed Greatminds tool environment.
8. Restart web servers to load updated assets. Check active work before restarting
   daemons that need runtime changes; do not interrupt working agents blindly.

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

## Required documentation review before every tag

README.md is also the PyPI project description. Audit it and all public docs
against the release's implementation before publishing, every time:

- Follow the installation and existing-project onboarding commands. Check runtime
  requirements, isolated tool environments and project interpreter separation.
- Compare web labels, controls, screenshots and workflows with the current UI.
- Check executor compatibility claims against the recorded acceptance evidence.
- Verify daemon start/stop behavior, configuration examples and upgrade guidance.
- Check links and remove stale version claims. README links must be absolute so
  they work on PyPI. Record relevant documentation changes in the changelog.

Run the public documentation tests, generated reference check and strict site build:

```bash
python -m pytest tests/data/test_docs_no_bin_refs.py tests/cli/test_public_docs_consistency.py
python tools/generate_contract_reference.py --check
mkdocs build --strict
```

The publish workflow runs these checks before uploading packages. They catch
structural inconsistencies; a full semantic review is still required. After
publication, compare the PyPI JSON description with README.md and verify the
published documentation. Cached search previews are not release evidence.
