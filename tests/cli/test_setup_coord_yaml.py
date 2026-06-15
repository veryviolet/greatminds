"""Tests for `greatminds setup` coord.yaml generation + session validation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import setup as setup_mod
from greatminds.cli import daemon as daemon_mod


# Canonical coord.yaml layout: five paned windows (the human-facing /
# resident seats) plus the eight driven workers. Driven roles get NO
# tmux pane — coordd runs each of their turns as a managed subprocess —
# but they are still listed so coordd knows each role's tool. PLANNER is
# interactive chat (codex), MAINTAINER is the self-loop watchdog
# (claude), `dashboard` and `logs` are read-only observer panes, and
# `live` is the USER-started staged pane for LIVE-DEVELOPER.
CANONICAL_WINDOWS = [
    # --- paned, human-facing / resident roles ---
    ("planner",    "ARCHITECT-PLANNER",  "codex",  "chat"),
    ("maintainer", "MAINTAINER",         "claude", "loop"),
    ("dashboard",  "",                   "bash",   "dashboard"),
    ("logs",       "",                   "bash",   "logs"),
    ("live",       "LIVE-DEVELOPER",     "claude", "staged"),
    # --- driven roles (no pane; coordd drives each turn) ---
    ("reviewer",   "ARCHITECT-REVIEWER", "codex",  "driven"),
    ("dev",        "DEVELOPER",          "claude", "driven"),
    ("ui",         "UI-DEVELOPER",       "claude", "driven"),
    ("writer",     "TECHNICAL-WRITER",   "codex",  "driven"),
    ("tester",     "TESTER",             "claude", "driven"),
    ("reader",     "READER",             "claude", "driven"),
    ("explorer",   "EXPLORER",           "codex",  "driven"),
    # 1.6.0: STAND-KEEPER retired — coordd deploys the stand itself.
]


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Redirect daemon-module registry/systemd paths so setup's daemon
    integration never touches the real ~/.config."""
    reg_dir = tmp_path / ".config" / "greatminds"
    sysd = tmp_path / ".config" / "systemd" / "user"
    monkeypatch.setattr(daemon_mod, "REGISTRY_DIR", reg_dir)
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH", reg_dir / "projects.json")
    monkeypatch.setattr(daemon_mod, "SYSTEMD_USER_DIR", sysd)


def _invoke(args: list[str]):
    return CliRunner().invoke(
        setup_mod.setup, args, catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Default session = basename(project_dir)
# ---------------------------------------------------------------------------


def test_fresh_setup_generates_coord_yaml_with_basename_session(tmp_path):
    project_dir = tmp_path / "foo-project"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir)])
    assert result.exit_code == 0, result.output

    coord_yaml = project_dir / "coord.yaml"
    assert coord_yaml.is_file()
    doc = yaml.safe_load(coord_yaml.read_text(encoding="utf-8"))
    assert doc["session"] == "foo-project"
    assert doc["project_dir"] == str(project_dir)


def test_fresh_setup_keeps_greatminds_canon_under_coordination(tmp_path):
    project_dir = tmp_path / "clean-root"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir)])
    assert result.exit_code == 0, result.output

    assert not (project_dir / "schema.yaml").exists()
    assert not (project_dir / "COORDINATE.md").exists()
    assert (project_dir / "coordination" / "schema.yaml").is_file()
    assert (project_dir / "coordination" / "COORDINATE.md").is_file()
    assert (project_dir / "coordination" / "bootstrap.md").is_file()


def test_setup_relocates_legacy_root_canon_files(tmp_path):
    project_dir = tmp_path / "legacy-root"
    project_dir.mkdir()
    (project_dir / "schema.yaml").write_text(
        "version: 999\ncustom: true\n", encoding="utf-8")
    (project_dir / "COORDINATE.md").write_text(
        "legacy coordinate notes\n", encoding="utf-8")

    result = _invoke(["--project-dir", str(project_dir)])
    assert result.exit_code == 0, result.output

    coord = project_dir / "coordination"
    assert not (project_dir / "schema.yaml").exists()
    assert not (project_dir / "COORDINATE.md").exists()
    assert (coord / "schema.yaml").is_file()
    assert (coord / "COORDINATE.md").is_file()
    assert (coord / ".backups" / "schema.yaml.root-legacy.bak").read_text(
        encoding="utf-8") == "version: 999\ncustom: true\n"
    assert (coord / ".backups" / "COORDINATE.md.root-legacy.bak").read_text(
        encoding="utf-8") == "legacy coordinate notes\n"


def test_explicit_session_flag_overrides_default(tmp_path):
    project_dir = tmp_path / "foo"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir), "--session", "alpha"])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load((project_dir / "coord.yaml").read_text())
    assert doc["session"] == "alpha"


# ---------------------------------------------------------------------------
# Init-style: existing coord.yaml is never overwritten
# ---------------------------------------------------------------------------


def test_existing_coord_yaml_is_left_untouched(tmp_path):
    project_dir = tmp_path / "with-existing"
    project_dir.mkdir()
    custom = "session: hand-rolled\nproject_dir: /irrelevant\nwindows: []\n"
    (project_dir / "coord.yaml").write_text(custom, encoding="utf-8")

    result = _invoke(["--project-dir", str(project_dir),
                      "--session", "would-be-overwritten"])
    assert result.exit_code == 0, result.output
    assert (project_dir / "coord.yaml").read_text() == custom
    assert "exists, skipping" in result.output


def test_force_flag_does_not_overwrite_existing_coord_yaml(tmp_path):
    """REVIEWER iter-3 blocker: --force advertised overwrite but
    setup deliberately makes coord.yaml init-style. --force MUST NOT
    overwrite an existing coord.yaml."""
    project_dir = tmp_path / "with-existing-force"
    project_dir.mkdir()
    custom = "session: handrolled\nproject_dir: /irrelevant\nwindows: []\n"
    (project_dir / "coord.yaml").write_text(custom, encoding="utf-8")

    result = _invoke(["--project-dir", str(project_dir),
                      "--session", "would-be-overwritten",
                      "--force"])
    assert result.exit_code == 0, result.output
    # Even with --force, coord.yaml is preserved.
    assert (project_dir / "coord.yaml").read_text() == custom
    assert "exists, skipping" in result.output


def test_force_flag_help_text_does_not_promise_coord_yaml_overwrite():
    """The CLI help for --force must NOT claim it overwrites coord.yaml,
    since that's misleading (init-style behavior)."""
    runner = CliRunner()
    result = runner.invoke(setup_mod.setup, ["--help"])
    assert result.exit_code == 0
    help_text = result.output
    assert "--force" in help_text
    # Find the --force section: should NOT promise coord.yaml overwrite,
    # OR should explicitly state coord.yaml is excluded.
    # Acceptable: either no mention of coord.yaml in the --force help, or
    # explicit "never overwritten" / "delete it first" phrasing.
    # Extract --force line + continuation (Click wraps at column width).
    lines = help_text.splitlines()
    force_text = ""
    capture = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--force"):
            capture = True
        elif capture and (stripped.startswith("--") or stripped.startswith("-h")):
            break
        if capture:
            force_text += " " + stripped
    force_text = force_text.lower()
    if "coord.yaml" in force_text:
        # Must explicitly disclaim overwrite.
        assert ("never overwritten" in force_text
                or "delete it first" in force_text
                or "not apply to coord.yaml" in force_text), (
            f"--force help mentions coord.yaml without disclaimer: {force_text!r}"
        )


# ---------------------------------------------------------------------------
# Generated YAML shape + canonical windows
# ---------------------------------------------------------------------------


def test_generated_coord_yaml_has_canonical_windows(tmp_path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir)])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load((project_dir / "coord.yaml").read_text())

    assert isinstance(doc["windows"], list)
    assert len(doc["windows"]) == len(CANONICAL_WINDOWS)

    for window, (name, role, tool, mode) in zip(
        doc["windows"], CANONICAL_WINDOWS,
    ):
        assert window["name"] == name
        assert window["role"] == role
        assert window["tool"] == tool
        if mode is None:
            assert "mode" not in window or window.get("mode") is None
        else:
            assert window["mode"] == mode


def test_generated_coord_yaml_roundtrips_through_yaml_safe_load(tmp_path):
    project_dir = tmp_path / "rt"
    project_dir.mkdir()
    _invoke(["--project-dir", str(project_dir)])
    text = (project_dir / "coord.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    # Roundtrip yaml→text→yaml.
    assert yaml.safe_load(yaml.safe_dump(doc)) == doc


# ---------------------------------------------------------------------------
# Daemon-registry integration (task 0015 restoration — was punted in 0008
# iter-2 + 0010 iter-2 to land cleanly after both upstream tasks verified)
# ---------------------------------------------------------------------------


def test_registry_populated_when_generating_coord_yaml(tmp_path):
    """Fresh setup → registry entry `{session_name: project_dir.resolve()}`."""
    project_dir = tmp_path / "alpha"
    project_dir.mkdir()
    _invoke(["--project-dir", str(project_dir), "--session", "alpha"])

    reg = daemon_mod.load_registry()
    assert reg.get("alpha") == str(project_dir.resolve())


def test_registry_populated_when_coord_yaml_pre_exists(tmp_path):
    """Skip-existing path also writes the registry (idempotent — entry
    refreshed in case the project_dir was moved/renamed)."""
    project_dir = tmp_path / "beta"
    project_dir.mkdir()
    (project_dir / "coord.yaml").write_text(
        yaml.safe_dump({"session": "beta-existing", "windows": []}),
        encoding="utf-8",
    )
    _invoke(["--project-dir", str(project_dir)])

    reg = daemon_mod.load_registry()
    assert reg.get("beta-existing") == str(project_dir.resolve())


def test_setup_degrades_gracefully_when_register_project_fails(
    tmp_path, monkeypatch,
):
    """If daemon.register_project raises, setup MUST exit 0 with a warning."""
    project_dir = tmp_path / "graceful"
    project_dir.mkdir()

    def boom(*_a, **_kw):
        raise RuntimeError("simulated systemd-user not available")

    monkeypatch.setattr(daemon_mod, "register_project", boom)

    result = _invoke(["--project-dir", str(project_dir), "--session", "graceful"])
    assert result.exit_code == 0, result.output
    assert "could not register" in result.output
    # coord.yaml still generated; only the registry step degraded.
    assert (project_dir / "coord.yaml").is_file()


# ---------------------------------------------------------------------------
# Session-name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "",
    "has spaces",
    "with/slash",
    "with:colon",
    "with@at",
    "x" * 65,
])
def test_invalid_session_names_are_rejected(tmp_path, bad):
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir), "--session", bad])
    assert result.exit_code == 2
    assert "session name must match" in result.output


@pytest.mark.parametrize("good", [
    "x",
    "abc",
    "alpha.beta",
    "alpha-beta",
    "alpha_beta",
    "X" * 64,
    "a1b2c3",
])
def test_valid_session_names_are_accepted(tmp_path, good):
    project_dir = tmp_path / "p"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir), "--session", good])
    assert result.exit_code == 0
    doc = yaml.safe_load((project_dir / "coord.yaml").read_text())
    assert doc["session"] == good


# ---------------------------------------------------------------------------
# Default-name corner cases
# ---------------------------------------------------------------------------


def test_default_session_strips_leading_dot(tmp_path):
    project_dir = tmp_path / ".dotted"
    project_dir.mkdir()
    result = _invoke(["--project-dir", str(project_dir)])
    assert result.exit_code == 0
    doc = yaml.safe_load((project_dir / "coord.yaml").read_text())
    assert doc["session"] == "dotted"


def test_default_session_for_root_falls_back_to_agents():
    """Pure helper test: Path('/') should return 'agents' as fallback."""
    assert setup_mod._default_session_name(Path("/")) == "agents"
