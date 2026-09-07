"""Tests for `greatminds update`."""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from greatminds.cli import update as upd


# ---------------------------------------------------------------------------
# Fakes: PyPI fetch, subprocess.run, os.execv
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_pypi(monkeypatch):
    """Mock PyPI fetch to return a configurable latest version."""
    # Keep update scenarios independent of the version being released.
    monkeypatch.setattr(upd, "__version__", "2.0.0")
    state = {"latest": "2.99.0"}

    def fake_fetch():
        if state["latest"] is None:
            raise click_exit_2("PyPI mock returned no version")
        return state["latest"]

    def click_exit_2(msg):
        import click as _c
        raise _c.exceptions.Exit(2)

    monkeypatch.setattr(upd, "_fetch_latest_pypi_version", fake_fetch)
    return state


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Capture all subprocess.run calls; default rc=0."""
    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def fake_execv(monkeypatch):
    """Replace os.execv with a recorder that raises a sentinel exception so
    the test can verify the args without ACTUALLY replacing the process."""
    class ExecvCalled(Exception):
        pass

    calls: list[tuple] = []

    def fake(path, argv):
        calls.append((path, list(argv)))
        raise ExecvCalled

    monkeypatch.setattr(upd.os, "execv", fake)
    return SimpleNamespace(calls=calls, sentinel=ExecvCalled)


def _invoke(args: list[str]):
    return CliRunner().invoke(upd.update, args, catch_exceptions=False)


# ---------------------------------------------------------------------------
# --check / --dry-run
# ---------------------------------------------------------------------------


def test_check_when_newer_pypi_version_available(fake_pypi):
    fake_pypi["latest"] = "2.99.0"
    result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "current: greatminds" in result.output
    assert "latest on PyPI: 2.99.0" in result.output
    assert "would upgrade" in result.output


def test_check_already_up_to_date(fake_pypi):
    fake_pypi["latest"] = upd.__version__
    result = _invoke(["--check"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output


def test_dry_run_is_alias_of_check(fake_pypi):
    fake_pypi["latest"] = "2.99.0"
    a = _invoke(["--check"])
    b = _invoke(["--dry-run"])
    # Both report the would-be upgrade.
    assert "would upgrade" in a.output
    assert "would upgrade" in b.output


def test_check_major_bump_without_flag_warns(fake_pypi):
    # Force a major-version bump (e.g. 1.x → 9.x).
    fake_pypi["latest"] = "9.0.0"
    result = _invoke(["--check"])
    assert result.exit_code == 0
    assert "would refuse: major bump" in result.output
    assert "--major" in result.output


def test_check_major_bump_with_flag_proceeds(fake_pypi):
    fake_pypi["latest"] = "9.0.0"
    result = _invoke(["--check", "--major"])
    assert result.exit_code == 0
    assert "would upgrade" in result.output


# ---------------------------------------------------------------------------
# Full update: pip + self-replace
# ---------------------------------------------------------------------------


def test_full_update_calls_pip_then_self_replaces(fake_pypi, fake_subprocess,
                                                    fake_execv,
                                                    monkeypatch):
    fake_pypi["latest"] = "2.99.0"

    # 0299: stub the env detector to ``venv`` so the legacy pip
    # path is exercised here. The new env branching (uv/poetry/pixi
    # /conda) is covered exhaustively by
    # ``test_update_env_branching_0299.py``; this test pins the
    # pip happy path that survives 0299 as the no-lockfile
    # fallback.
    from greatminds.core.env import EnvSetup
    monkeypatch.setattr(
        "greatminds.cli.update.detect_env_setup",
        lambda *_a, **_k: EnvSetup(
            env_type="venv", activation="", source="(test stub)",
        ),
        raising=False,
    )
    # The import path inside ``_step_pip_upgrade`` is the actual
    # call site; patch there too.
    from greatminds.core import env as _env
    monkeypatch.setattr(
        _env, "detect",
        lambda *_a, **_k: EnvSetup(
            env_type="venv", activation="", source="(test stub)",
        ),
    )
    # The upgrade subprocess is faked (no real install), so stub the
    # post-upgrade verify to report the upgrade landed → self-replace fires.
    monkeypatch.setattr(upd, "_installed_version_fresh", lambda: "2.99.0")

    with pytest.raises(fake_execv.sentinel):
        # The fake execv raises; we let the exception escape so the test
        # can inspect what happened up to that point.
        upd.update.callback(  # type: ignore[misc]
            post_pip=False, check=False, dry_run=False,
            major=True,  # accept the major bump for this test
            project_name=None,
        )

    # pip install command was issued (env=venv → legacy pip path).
    pip_calls = [c for c in fake_subprocess if "pip" in c and "install" in c]
    assert len(pip_calls) == 1
    assert "--upgrade" in pip_calls[0]
    assert "greatminds" in pip_calls[0]

    # os.execv was called exactly once with the freshly-installed binary.
    assert len(fake_execv.calls) == 1
    path, argv = fake_execv.calls[0]
    assert argv[-2:] == ["update", "--post-pip"]


def test_full_update_refuses_major_without_flag(fake_pypi, fake_subprocess,
                                                  fake_execv):
    fake_pypi["latest"] = "9.0.0"
    result = _invoke([])
    assert result.exit_code == 2
    assert "major upgrade" in result.output
    # pip + execv never invoked.
    assert not any("pip" in c and "install" in c for c in fake_subprocess)
    assert fake_execv.calls == []




# ---------------------------------------------------------------------------
# --post-pip: migration + daemon + agents
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# PyPI unreachable
# ---------------------------------------------------------------------------


def test_check_pypi_unreachable_clean_error(monkeypatch):
    def boom():
        import click as _c
        # Simulate _fetch_latest_pypi_version's own error handling path.
        import greatminds.cli._colors as _co
        _co.err("could not reach PyPI: simulated network failure")
        raise _c.exceptions.Exit(2)

    monkeypatch.setattr(upd, "_fetch_latest_pypi_version", boom)
    result = _invoke(["--check"])
    assert result.exit_code == 2
    assert "could not reach PyPI" in result.output


@pytest.mark.parametrize('current,latest,verdict', [
    ('2.6.0rc1', '2.6.0', 'upgrade'),
    ('2.6.0', '2.6.0.post1', 'upgrade'),
    ('2.6.dev1', '2.6a1', 'upgrade'),
    ('2.6.0.post1', '2.6.0', 'current'),
    ('2.6+local', '2.6.0', 'current'),
    ('2.6', '2.6.0', 'current'),
    ('2.6', '1!2.6', 'major'),
    ('2.6', 'invalid', 'invalid'),
    ('invalid', '2.6', 'invalid'),
])
def test_preview_and_upgrade_share_python_package_ordering(
        tmp_path, monkeypatch, fake_pypi, fake_subprocess, current, latest, verdict):
    from greatminds.core import env
    import click
    monkeypatch.setattr(upd, '__version__', current)
    fake_pypi['latest'] = latest
    monkeypatch.setattr(env, 'detect', lambda *a, **kw: SimpleNamespace(env_type='venv', project_dir=tmp_path, source='fixture'))
    monkeypatch.setattr(upd, '_installed_version_fresh', lambda: latest)
    preview = _invoke(['--check'])
    assert preview.exit_code == (2 if verdict == 'invalid' else 0)
    text = {'upgrade': 'would upgrade', 'current': 'already up to date',
            'major': 'would refuse: major bump', 'invalid': 'cannot compare package versions'}[verdict]
    assert text in preview.output
    assert fake_subprocess == []  # Preview and malformed versions cannot change installation.
    if verdict in {'major', 'invalid'}:
        with pytest.raises(click.exceptions.Exit) as error:
            upd._step_pip_upgrade(major=False)
        assert error.value.exit_code == 2 and fake_subprocess == []
    else:
        assert upd._step_pip_upgrade(major=False) == (verdict == 'upgrade')
        assert bool(fake_subprocess) == (verdict == 'upgrade')


@pytest.mark.parametrize('actual,success', [('2.7', True), ('2.7.0', True), (None, False), ('invalid', False)])
def test_installed_version_verification_accepts_only_equivalent_package_versions(
        tmp_path, monkeypatch, fake_pypi, fake_subprocess, actual, success):
    from greatminds.core import env
    import click
    monkeypatch.setattr(upd, '__version__', '2.6.0')
    fake_pypi['latest'] = '2.7.0'
    monkeypatch.setattr(env, 'detect', lambda *a, **kw: SimpleNamespace(env_type='venv', project_dir=tmp_path, source='fixture'))
    monkeypatch.setattr(upd, '_installed_version_fresh', lambda: actual)
    if success:
        assert upd._step_pip_upgrade(major=False)
    else:
        with pytest.raises(click.exceptions.Exit) as error:
            upd._step_pip_upgrade(major=False)
        assert error.value.exit_code == 1
