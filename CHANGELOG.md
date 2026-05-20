# Changelog

All notable changes to **greatminds** are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; versions
follow [SemVer](https://semver.org/) once 1.0.0 ships.

## 0.1.1 — 2026-05-20

### Fixed

- `greatminds-coord-launch` and `greatminds-coord-tmux` now find a
  sibling `greatminds-start-agent` in the same venv via
  ``Path(sys.executable).parent`` BEFORE falling back to ``shutil.which``.
  Previously they relied on PATH only, which broke when the user invoked
  the binary by full path (e.g. ``./.venv/bin/greatminds-coord-launch``)
  without sourcing the venv. Also: ``.resolve()`` is intentionally NOT
  called on ``sys.executable`` because uv-managed venvs symlink it to the
  underlying Python install — resolving would skip past the venv's bin
  dir entirely.
- "each agent window has 'bin/start_agent <ROLE> <tool>' pre-typed"
  message updated to print the actual resolved launcher name.

## 0.1.0 — 2026-05-20

First public release.

### Added

- Python package `greatminds` with 19 console entry-points covering the
  full R8 pipeline:
  - **Task management**: `greatminds-task`, `greatminds-inbox`,
    `greatminds-stand`, `greatminds-plan`, `greatminds-migrate-task`.
  - **Pipeline introspection**: `greatminds-gate-check`,
    `greatminds-wake-check`, `greatminds-watchdog`,
    `greatminds-intent-clean`, `greatminds-lint-tokens`.
  - **Hooks (Claude Code)**: `greatminds-stop-decide`,
    `greatminds-notify-journal`.
  - **Project bootstrap**: `greatminds-coord-init`.
  - **Agent launcher / fleet**: `greatminds-start-agent`,
    `greatminds-pty-launch`, `greatminds-render-role`,
    `greatminds-coordd`, `greatminds-coord-tmux`,
    `greatminds-coord-launch` (tmux/vscode/cursor-ide targets).
- Package data shipped under `greatminds.data`: `schema.yaml`,
  `command_START.yaml`, `COORDINATE.md`, `PROJECT_VARIABLES.md`, 13 role
  specs under `roles/`, 8 layered Claude Code plugins under `plugins/`,
  `mcp/canon.json` (context7 + playwright), 3 codex profile-v2 configs
  under `codex/profiles/`, queue templates under `templates/`.
- Path resolution shared across all CLI modules via `greatminds.core`:
  - `find_coord_dir(start=None, *, strict=True)` — walks up from cwd or
    honours `$COORD_PROJECT_DIR`.
  - `find_canon_dir()` — `$COORD_CANON_DIR` override or
    `importlib.resources.files("greatminds.data")`.
  - `caller_role()`, `die`, `now_iso`, `prog_name`.
- GitHub Actions CI building wheel on py 3.11/3.12/3.13, smoke-testing
  every entry-point's `--help`, validating packaged-data resolution, and
  running `greatminds-coord-init` on a fresh tmpdir.

### Notes

- Apache-2.0 licensed.
- `coordd-install` (Bash, systemd unit installer) and the stress-test
  scripts are not packaged in 0.1.0 — they will return as separate
  entry-points once their dependencies on systemd / heavy load
  generation are revisited.
