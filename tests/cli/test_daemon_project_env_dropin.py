"""PROJECT.env → daemon environment via a systemd EnvironmentFile drop-in.

The clean injection point: one per-instance drop-in gives coordd — and
every driven agent it spawns (they inherit its process env) — the fleet's
PROJECT.env as real environment variables.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from greatminds.cli import daemon as dm


def test_install_project_dropin_writes_environmentfile(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)

    wrote = dm.install_project_dropin("toy", project)
    assert wrote is True

    conf = (tmp_path / "systemd"
            / "greatminds-daemon@toy.service.d" / "10-project-env.conf")
    body = conf.read_text(encoding="utf-8")
    # Optional (leading `-`) EnvironmentFile pointing at the fleet PROJECT.env.
    expected = str(project / "coordination" / "PROJECT.env")
    assert f"EnvironmentFile=-{expected}" in body
    agent_env = str(tmp_path / "greatminds" / "agent-env" / "toy.env")
    assert f"EnvironmentFile=-{agent_env}" in body
    assert "[Service]" in body


def test_install_project_dropin_is_idempotent(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)

    assert dm.install_project_dropin("toy", project) is True
    # Same inputs → no rewrite (no gratuitous daemon-reload churn).
    assert dm.install_project_dropin("toy", project) is False


def test_dropin_optional_dash_tolerates_missing_env_file(
    tmp_path: Path, monkeypatch,
) -> None:
    # The `-` prefix means a fleet with no PROJECT.env yet still gets a
    # valid unit (systemd silently skips the missing file).
    monkeypatch.setattr(dm, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)
    dm.install_project_dropin("toy", project)
    conf = (tmp_path / "systemd"
            / "greatminds-daemon@toy.service.d" / "10-project-env.conf")
    assert "EnvironmentFile=-" in conf.read_text(encoding="utf-8")


def test_capture_agent_env_writes_private_allowlisted_env(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "REGISTRY_DIR", tmp_path / "greatminds")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")

    assert dm.capture_agent_env("toy") is True

    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    body = target.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY='secret value'" in body
    assert "UNRELATED_SECRET" not in body
    assert target.stat().st_mode & 0o777 == 0o600


def test_capture_agent_env_follows_claude_host_auth_pointer(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "REGISTRY_DIR", tmp_path / "greatminds")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    monkeypatch.setenv("CLAUDE_CODE_HOST_AUTH_ENV_VAR", "HOST_AUTH_TOKEN")
    monkeypatch.setenv("HOST_AUTH_TOKEN", "secret host token")

    assert dm.capture_agent_env("toy") is True

    body = (tmp_path / "greatminds" / "agent-env" / "toy.env").read_text(
        encoding="utf-8")
    assert "CLAUDE_CODE_HOST_AUTH_ENV_VAR=HOST_AUTH_TOKEN" in body
    assert "HOST_AUTH_TOKEN='secret host token'" in body


def test_capture_agent_env_empty_shell_preserves_existing_file(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "REGISTRY_DIR", tmp_path / "greatminds")
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    target.parent.mkdir(parents=True)
    target.write_text("ANTHROPIC_API_KEY=old\n", encoding="utf-8")
    target.chmod(0o600)
    for name in dm.AGENT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    assert dm.capture_agent_env("toy") is False
    assert target.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=old\n"


def test_daemon_candidate_env_layers_project_then_agent_env(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "from-shell")
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)
    (project / "coordination" / "PROJECT.env").write_text(
        "ANTHROPIC_BASE_URL=from-project\n"
        "PROJECT_ONLY='two words'\n",
        encoding="utf-8",
    )
    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    target.parent.mkdir(parents=True)
    target.write_text(
        "ANTHROPIC_BASE_URL=from-agent\n"
        "CLAUDE_CODE_OAUTH_TOKEN='secret token'\n",
        encoding="utf-8",
    )

    env = dm._daemon_candidate_env("toy", project)

    assert env["ANTHROPIC_BASE_URL"] == "from-agent"
    assert env["PROJECT_ONLY"] == "two words"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "secret token"


def test_has_driven_claude_roles_detects_only_schema_driven_roles(
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    (project / "coordination").mkdir(parents=True)
    (project / "coord.yaml").write_text(
        "windows:\n"
        "  - role: DEVELOPER\n"
        "    tool: claude\n",
        encoding="utf-8",
    )
    (project / "coordination" / "schema.yaml").write_text(
        "roles:\n"
        "  DEVELOPER:\n"
        "    lifecycle: driven\n",
        encoding="utf-8",
    )

    assert dm.has_driven_claude_roles(project) is True


def test_claude_headless_probe_reports_auth_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["bash", "-lc"]
        assert "claude -p" in argv[2]
        assert "--output-format json" in argv[2]
        return subprocess.CompletedProcess(
            argv, 1,
            stdout=json.dumps({
                "is_error": True,
                "api_error_status": 401,
                "result": "Failed to authenticate. API Error: 401",
            }),
            stderr="",
        )

    monkeypatch.setattr(dm.subprocess, "run", fake_run)

    ok, detail = dm._claude_headless_probe("toy", project, 5)

    assert ok is False
    assert "Claude auth failed" in detail
    assert "status=401" in detail


def test_claude_headless_probe_drops_stale_snapshot_oauth(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(dm, "AGENT_ENV_DIR",
                        tmp_path / "greatminds" / "agent-env")
    target = tmp_path / "greatminds" / "agent-env" / "toy.env"
    target.parent.mkdir(parents=True)
    target.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=old\n"
        "CLAUDE_CODE_HOST_AUTH_ENV_VAR=HOST_AUTH\n"
        "HOST_AUTH=old-host\n",
        encoding="utf-8",
    )

    def fake_run(argv, **kwargs):
        env = kwargs["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert "CLAUDE_CODE_HOST_AUTH_ENV_VAR" not in env
        assert "HOST_AUTH" not in env
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=json.dumps({"is_error": False, "result": "OK"}),
            stderr="",
        )

    monkeypatch.setattr(dm.subprocess, "run", fake_run)

    ok, detail = dm._claude_headless_probe("toy", project, 5)

    assert ok is True
    assert "succeeded" in detail


def test_claude_headless_probe_reports_expired_oauth_without_refresh(
    tmp_path: Path, monkeypatch,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "expired",
            "refreshToken": "",
            "expiresAt": 1,
        }
    }), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1,
            stdout=json.dumps({
                "is_error": True,
                "api_error_status": 401,
                "result": "Failed to authenticate. API Error: 401",
            }),
            stderr="",
        )

    monkeypatch.setattr(dm.subprocess, "run", fake_run)

    ok, detail = dm._claude_headless_probe("toy", project, 5)

    assert ok is False
    assert "expired and have no refresh token" in detail
    assert "claude setup-token" in detail
