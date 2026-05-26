"""Tests for task 0199: coordd PyPI auto-version check.

Pre-0199 MAINTAINER discovered new greatminds releases only when
PLANNER inbox-asked or USER said «upgrade». 0199 adds a coordd-side
periodic PyPI poll (default 4h per ``schema.auto_update``) that
files one inbox info-message to MAINTAINER per detected new version.
Notify-only — MAINTAINER decides when to actually run
``greatminds update``.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import coordd as coordd_mod
from greatminds.core.paths import find_canon_dir


# ---------- schema pin ----------


def test_schema_has_auto_update_section() -> None:
    """0199 schema pin: ``schema.auto_update`` carries the four
    configuration fields (interval, target, mode, source)."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    au = doc.get("auto_update")
    assert au is not None, "0199: schema missing 'auto_update:' section"
    assert au.get("check_interval_seconds") == 14400
    assert au.get("notify_target") == "MAINTAINER"
    assert au.get("mode") == "notify_only"
    assert au.get("source") == "pypi"


# ---------- _load_auto_update_config ----------


def test_load_auto_update_config_returns_schema_values() -> None:
    """Loader returns the canonical defaults from real canon."""
    cfg = coordd_mod._load_auto_update_config(find_canon_dir())
    assert cfg["check_interval_seconds"] == 14400.0
    assert cfg["notify_target"] == "MAINTAINER"
    assert cfg["mode"] == "notify_only"
    assert cfg["source"] == "pypi"


def test_load_auto_update_config_defaults_on_missing_schema(
    tmp_path: Path,
) -> None:
    """Missing schema.yaml → defaults that match canonical values."""
    canon = tmp_path / "canon"
    canon.mkdir()
    cfg = coordd_mod._load_auto_update_config(canon)
    assert cfg["check_interval_seconds"] == 14400.0
    assert cfg["notify_target"] == "MAINTAINER"


# ---------- _installed_greatminds_version ----------


def test_installed_version_returns_string() -> None:
    """The local install must report a version (we're running from
    a source-installed wheel)."""
    v = coordd_mod._installed_greatminds_version()
    assert isinstance(v, str) and v


# ---------- _fetch_pypi_latest_version ----------


def test_fetch_pypi_uses_urllib_and_returns_version(monkeypatch) -> None:
    """Happy path: mock urllib.request.urlopen to return the JSON
    PyPI gives. The helper extracts ``info.version``."""
    captured = {}

    class FakeResp:
        def __init__(self, body): self._body = body
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return self._body.encode("utf-8")

    def fake_urlopen(url, timeout):
        captured["url"] = url
        return FakeResp(json.dumps({"info": {"version": "9.9.9"}}))

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    v = coordd_mod._fetch_pypi_latest_version("https://example/pypi.json")
    assert v == "9.9.9"
    assert captured["url"] == "https://example/pypi.json"


def test_fetch_pypi_returns_none_on_network_error(monkeypatch) -> None:
    """Defensive: PyPI offline / DNS broken → None. Loop must never
    crash on the auto-update tick."""
    import urllib.error
    import urllib.request

    def fake_urlopen(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert coordd_mod._fetch_pypi_latest_version("https://x") is None


def test_fetch_pypi_returns_none_on_malformed_json(monkeypatch) -> None:
    """Garbage response body → None, not crash."""
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return b"not json"

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout: FakeResp())
    assert coordd_mod._fetch_pypi_latest_version("https://x") is None


# ---------- _is_newer_version ----------


def test_is_newer_version_pep440() -> None:
    assert coordd_mod._is_newer_version("1.2.10", "1.2.9") is True
    assert coordd_mod._is_newer_version("1.3.0", "1.2.9") is True
    assert coordd_mod._is_newer_version("1.2.9", "1.2.9") is False
    assert coordd_mod._is_newer_version("1.2.8", "1.2.9") is False


def test_is_newer_version_multi_digit_segments() -> None:
    """0199 iter-2 regression pin (stand_done/0200 partial):
    lexicographic compare wrongly says '1.2.10' < '1.2.9'. Without
    ``packaging`` as a real dep the helper fell through to its
    lex-fallback branch and 3/7 stand cases failed. Pin against
    the regression by exercising the cases that trip lex compare."""
    assert coordd_mod._is_newer_version("1.2.10", "1.2.9") is True
    assert coordd_mod._is_newer_version("1.10.0", "1.9.0") is True
    assert coordd_mod._is_newer_version("2.0.0", "1.99.99") is True
    assert coordd_mod._is_newer_version("0.10.0", "0.9.0") is True


def test_is_newer_handles_prerelease() -> None:
    """PEP 440: 1.3.0a0 < 1.3.0. The helper must use the proper
    version comparison, not lexicographic."""
    assert coordd_mod._is_newer_version("1.3.0a0", "1.2.9") is True
    assert coordd_mod._is_newer_version("1.3.0", "1.3.0a0") is True


# ---------- _notify_maintainer_of_new_version ----------


def test_notify_maintainer_invokes_inbox_send(monkeypatch,
                                                 tmp_path: Path) -> None:
    """0199: helper shells out to ``greatminds inbox send MAINTAINER
    --kind info ...`` with the version + URL in the body."""
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    coord = tmp_path / "project" / "coordination"
    coord.mkdir(parents=True)

    ok = coordd_mod._notify_maintainer_of_new_version(
        coord, "MAINTAINER", "1.3.0", "1.2.9", verbose=False,
    )
    assert ok is True
    assert calls, "0199: inbox send was never invoked"
    cmd = calls[0]
    # Command shape: python -m greatminds.cli.main inbox send MAINTAINER
    # --kind info --body "..."
    assert "inbox" in cmd
    assert "send" in cmd
    assert "MAINTAINER" in cmd
    assert "info" in cmd
    body_idx = cmd.index("--body") + 1
    assert "1.3.0" in cmd[body_idx]
    assert "1.2.9" in cmd[body_idx]


def test_notify_maintainer_returns_false_on_inbox_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    """Best-effort: if inbox-send fails (e.g. coordination/ dir
    missing in test), helper returns False but doesn't raise."""
    def fake_run(cmd, *a, **kw):
        return subprocess.CompletedProcess(list(cmd), 2, "", "boom")

    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    coord = tmp_path / "project" / "coordination"
    coord.mkdir(parents=True)
    ok = coordd_mod._notify_maintainer_of_new_version(
        coord, "MAINTAINER", "1.3.0", "1.2.9", verbose=False,
    )
    assert ok is False


def test_notify_maintainer_swallows_subprocess_errors(
    monkeypatch, tmp_path: Path,
) -> None:
    """OSError / TimeoutExpired → return False, don't propagate."""
    def fake_run(*a, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(coordd_mod.subprocess, "run", fake_run)
    coord = tmp_path / "project" / "coordination"
    coord.mkdir(parents=True)
    ok = coordd_mod._notify_maintainer_of_new_version(
        coord, "MAINTAINER", "1.3.0", "1.2.9", verbose=False,
    )
    assert ok is False
