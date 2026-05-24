"""Tests for the version-drift detection helper (task 0009)."""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from greatminds import __version__ as GM_VERSION
from greatminds import _version_check as vc


def _write_daemon_version(project_dir: Path, version: str) -> None:
    coord = project_dir / "coordination"
    coord.mkdir(parents=True, exist_ok=True)
    (coord / vc.DAEMON_VERSION_FILENAME).write_text(
        f"{version}\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# check_drift
# ---------------------------------------------------------------------------


def test_cli_eq_daemon_returns_none(tmp_path):
    _write_daemon_version(tmp_path, GM_VERSION)
    assert vc.check_drift(tmp_path) is None


def test_cli_neq_daemon_returns_tuple(tmp_path):
    _write_daemon_version(tmp_path, "0.0.1")
    result = vc.check_drift(tmp_path)
    assert result == (GM_VERSION, "0.0.1")


def test_missing_daemon_version_returns_tuple_with_none(tmp_path):
    """No `.daemon_version` file → tuple where the daemon side is None."""
    (tmp_path / "coordination").mkdir()
    result = vc.check_drift(tmp_path)
    assert result == (GM_VERSION, None)


def test_daemon_version_with_trailing_whitespace_is_normalized(tmp_path):
    """The producer writes one line + newline; we strip it for comparison."""
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True)
    (coord / vc.DAEMON_VERSION_FILENAME).write_text(
        f"  {GM_VERSION}  \n\n", encoding="utf-8",
    )
    assert vc.check_drift(tmp_path) is None


# ---------------------------------------------------------------------------
# emit_drift_warning — agent path is silent
# ---------------------------------------------------------------------------


def test_drift_warning_silent_when_role_env_set(monkeypatch, tmp_path):
    """Agent path: silent on stderr — paper trail goes to journal.ndjson (see
    test_drift_agent_path_writes_journal_warn for the producer side)."""
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    (tmp_path / "coordination").mkdir()
    buf = io.StringIO()
    vc.emit_drift_warning("1.2.1", "1.2.0", file=buf, project_dir=tmp_path)
    assert buf.getvalue() == "", "agent path must emit nothing to stderr"


def test_drift_warning_printed_when_role_env_unset(monkeypatch):
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    buf = io.StringIO()
    vc.emit_drift_warning("1.2.1", "1.2.0", file=buf)
    out = buf.getvalue()
    assert "WARNING" in out
    assert "1.2.1" in out
    assert "1.2.0" in out
    assert "greatminds update --post-pip" in out


def test_drift_warning_missing_daemon_version(monkeypatch):
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    buf = io.StringIO()
    vc.emit_drift_warning("1.2.1", None, file=buf)
    out = buf.getvalue()
    assert "daemon version unknown" in out
    assert "greatminds update --post-pip" in out


# ---------------------------------------------------------------------------
# maybe_warn — composition, no-raise guarantee
# ---------------------------------------------------------------------------


def test_maybe_warn_when_no_drift_is_a_no_op(tmp_path, monkeypatch, capsys):
    _write_daemon_version(tmp_path, GM_VERSION)
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    vc.maybe_warn(tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_maybe_warn_when_drift_prints_to_stderr(tmp_path, monkeypatch, capsys):
    _write_daemon_version(tmp_path, "0.0.1")
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    vc.maybe_warn(tmp_path)
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert captured.out == ""


def test_maybe_warn_when_drift_is_silent_for_agent(tmp_path, monkeypatch, capsys):
    _write_daemon_version(tmp_path, "0.0.1")
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    vc.maybe_warn(tmp_path)
    captured = capsys.readouterr()
    assert captured.err == "", "agent path must not pollute stderr"


# ---------------------------------------------------------------------------
# Agent path: writes a kind=warn record to coordination/journal.ndjson
# ---------------------------------------------------------------------------


def test_drift_agent_path_writes_journal_warn(tmp_path, monkeypatch, capsys):
    """Reviewer-flagged regression: agent path must leave a paper trail in
    coordination/journal.ndjson with kind=warn even though it's silent on
    stderr."""
    _write_daemon_version(tmp_path, "0.0.1")
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    vc.maybe_warn(tmp_path)

    # Silent on tty.
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""

    # journal.ndjson exists with one warn record.
    journal = tmp_path / "coordination" / vc.JOURNAL_FILENAME
    assert journal.is_file()
    lines = journal.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json as _json
    rec = _json.loads(lines[0])
    assert rec["kind"] == "warn"
    assert rec["reason"] == "version_drift"
    assert rec["actor"] == "DEVELOPER"
    assert rec["cli_version"] == GM_VERSION
    assert rec["daemon_version"] == "0.0.1"
    assert "greatminds update --post-pip" in rec["hint"]


def test_drift_user_path_does_not_write_journal(tmp_path, monkeypatch, capsys):
    """USER path: warning goes to stderr; journal.ndjson must NOT be written
    (it would conflate transient user drift with agent paper-trail data)."""
    _write_daemon_version(tmp_path, "0.0.1")
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    vc.maybe_warn(tmp_path)

    journal = tmp_path / "coordination" / vc.JOURNAL_FILENAME
    assert not journal.is_file(), \
        "USER path must NOT touch journal.ndjson; warning goes to stderr only"
    captured = capsys.readouterr()
    assert "WARNING" in captured.err


# ---------------------------------------------------------------------------
# Project-scope guard: maybe_warn skips non-greatminds directories
# ---------------------------------------------------------------------------


def test_maybe_warn_silent_outside_greatminds_project(tmp_path, monkeypatch, capsys):
    """`cd ~ && greatminds task list` must not warn about missing
    .daemon_version — cwd isn't a greatminds project (no `coordination/`)."""
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    # tmp_path has NO `coordination/` subdir.
    vc.maybe_warn(tmp_path)
    captured = capsys.readouterr()
    assert captured.err == "", "non-project cwd must not nag about daemon-version"
    assert captured.out == ""


def test_maybe_warn_swallows_unexpected_errors(monkeypatch):
    """Drift check must NEVER bubble exceptions to the parent CLI."""
    def boom(_):
        raise RuntimeError("simulated crash inside check_drift")
    monkeypatch.setattr(vc, "check_drift", boom)
    # Should not raise.
    vc.maybe_warn(Path("/nonexistent/project"))
