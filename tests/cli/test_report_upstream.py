"""Tests for `greatminds report-upstream`.

External effects (``webbrowser.open``, ``subprocess.run`` for gh,
``urllib.request.urlopen`` for the api-token mode) are mocked. No real
GitHub traffic.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from click.testing import CliRunner

from greatminds.cli import report_upstream as ru_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_project(
    tmp_path: Path,
    *,
    report_cfg: dict | None = None,
    project_env: dict[str, str] | None = None,
    journal_lines: int = 0,
    coord_yaml_extra: dict | None = None,
) -> Path:
    coord = tmp_path / "coordination"
    coord.mkdir(parents=True, exist_ok=True)

    cfg: dict = {"session": "x", "project_dir": str(tmp_path),
                 "windows": [{"name": "dev", "role": "DEVELOPER"}]}
    if report_cfg is not None:
        cfg["report"] = report_cfg
    if coord_yaml_extra:
        cfg.update(coord_yaml_extra)
    (tmp_path / "coord.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    if project_env:
        with (coord / "PROJECT.env").open("w", encoding="utf-8") as f:
            for k, v in project_env.items():
                f.write(f"{k}={v}\n")

    if journal_lines:
        with (coord / "journal.ndjson").open("w", encoding="utf-8") as f:
            for i in range(journal_lines):
                f.write(json.dumps({"t": "2026-05-24T00:00:00Z",
                                    "actor": "TEST", "task": f"t{i}",
                                    "from": "a", "to": "b",
                                    "reason": "test", "intent_id": "u"}) + "\n")
    return tmp_path


def _invoke(tmp_path: Path, *args: str, env: dict[str, str] | None = None):
    runner = CliRunner()
    full_args = ["--project-dir", str(tmp_path), *args]
    return runner.invoke(
        ru_mod.report_upstream, full_args,
        env=env, catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_body_and_no_submission(tmp_path, monkeypatch):
    """--dry-run prints the body and skips every submission path.

    We can't blanket-mock subprocess (platform.platform() calls
    check_output internally), so we check the side-effect-free
    submission paths via observable output: no URL line, no API
    call (urlopen would raise), no webbrowser call (would raise).
    """
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})

    def _fail_webbrowser(*_a, **_kw):
        raise AssertionError("webbrowser.open must NOT be called in dry-run")
    monkeypatch.setattr(ru_mod.webbrowser, "open", _fail_webbrowser)

    def _fail_urlopen(*_a, **_kw):
        raise AssertionError("urlopen must NOT be called in dry-run")
    monkeypatch.setattr(ru_mod.urllib.request, "urlopen", _fail_urlopen)

    result = _invoke(tmp_path, "--title", "boom", "--body", "broke",
                     "--dry-run")
    assert result.exit_code == 0, result.output
    assert "## Symptom" in result.output
    assert "broke" in result.output
    assert "github.com" not in result.output, \
        "dry-run must not emit a github.com URL"


# ---------------------------------------------------------------------------
# url mode
# ---------------------------------------------------------------------------


def test_url_mode_builds_correct_url_and_opens_browser(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "alice/proj"})

    opened: list[str] = []
    monkeypatch.setattr(ru_mod.webbrowser, "open",
                        lambda url: opened.append(url))

    result = _invoke(tmp_path, "--title", "Hi there",
                     "--body", "broke",
                     "--label", "bug", "--label", "from-fleet",
                     "--mode", "url")
    assert result.exit_code == 0
    assert len(opened) == 1
    url = opened[0]
    assert url.startswith("https://github.com/alice/proj/issues/new?")
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    assert qs["title"] == ["Hi there"]
    assert "## Symptom" in qs["body"][0]
    assert qs["labels"] == ["bug,from-fleet"]


def test_body_cap_enforced_when_symptom_alone_exceeds_cap(tmp_path):
    """Reviewer-flagged blocker: oversized Symptom alone must still fit
    under BODY_SIZE_CAP. Repro: user body of `'x' * 8000` produced an
    8687-char body under the old impl. The fix must truncate the Symptom
    section itself once journal/coord.yaml/heartbeats markers can't save
    enough budget, and write the full pre-truncation body to /tmp.
    """
    coord = tmp_path / "coordination"
    coord.mkdir()
    (tmp_path / "coord.yaml").write_text("session: x\nwindows: []\n",
                                         encoding="utf-8")

    huge_user_body = "x" * 8000
    body, full_path = ru_mod._build_body(
        title="t",
        severity="normal",
        user_body=huge_user_body,
        project_root=tmp_path,
        coord_dir=coord,
        include_diagnostics=True,
    )
    assert len(body) <= ru_mod.BODY_SIZE_CAP, \
        f"body len {len(body)} > cap {ru_mod.BODY_SIZE_CAP}"
    # Some of the symptom prefix must still be present.
    assert "xxxxxxx" in body, "symptom prefix should be preserved"
    # A truncation marker must point at the full local copy.
    assert "truncated" in body
    # Full pre-truncation body must exist on disk and contain the whole symptom.
    assert full_path is not None and full_path.is_file()
    full_text = full_path.read_text(encoding="utf-8")
    assert "x" * 8000 in full_text


def test_url_mode_oversize_body_truncates_journal_first(tmp_path, monkeypatch):
    """Body > cap → journal tail truncated first, marker present,
    symptom section intact, full copy written to /tmp."""
    _seed_project(
        tmp_path,
        report_cfg={"upstream_repo": "owner/repo"},
        journal_lines=50,
    )
    # Lower the cap so a normal-sized body forces truncation.
    monkeypatch.setattr(ru_mod, "BODY_SIZE_CAP", 1500)

    opened: list[str] = []
    monkeypatch.setattr(ru_mod.webbrowser, "open",
                        lambda url: opened.append(url))

    result = _invoke(tmp_path, "--title", "x",
                     "--body", "unique-symptom-token",
                     "--mode", "url")
    assert result.exit_code == 0, result.output
    assert len(opened) == 1
    parts = urlsplit(opened[0])
    body = parse_qs(parts.query)["body"][0]
    assert len(body) <= ru_mod.BODY_SIZE_CAP
    # Symptom preserved.
    assert "unique-symptom-token" in body
    # Truncation marker present.
    assert "[truncated, full local copy at" in body
    # Marker mentions a real /tmp path with the FULL pre-truncation body.
    m = re.search(r"\[truncated, full local copy at ([^\]]+)\]", body)
    assert m, body
    full_path = Path(m.group(1))
    assert full_path.is_file()
    full_text = full_path.read_text(encoding="utf-8")
    assert "unique-symptom-token" in full_text
    # Journal section is truncated FIRST. Verify by checking the
    # original journal content (line t0..t49) is absent from the
    # emitted body but present in the full local copy.
    assert '"task": "t49"' not in body
    assert '"task": "t49"' in full_text


# ---------------------------------------------------------------------------
# gh mode
# ---------------------------------------------------------------------------


def test_gh_mode_missing_gh_binary_exits_2(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})
    monkeypatch.setattr(ru_mod.shutil, "which", lambda name: None)

    result = _invoke(tmp_path, "--title", "x", "--body", "y",
                     "--mode", "gh")
    assert result.exit_code == 2
    assert "gh CLI not on PATH" in result.output


def test_gh_mode_invokes_gh_issue_create_with_expected_args(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})
    monkeypatch.setattr(ru_mod.shutil, "which", lambda name: "/usr/bin/gh")

    calls: list[list[str]] = []

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return subprocess.CompletedProcess(
                list(cmd), 0,
                "https://github.com/owner/repo/issues/42\n", "",
            )
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(ru_mod.subprocess, "run", fake_run)

    result = _invoke(tmp_path, "--title", "Boom", "--body", "details",
                     "--label", "bug", "--mode", "gh")
    assert result.exit_code == 0, result.output
    create_calls = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    c = create_calls[0]
    assert "--repo" in c and c[c.index("--repo") + 1] == "owner/repo"
    assert "--title" in c and c[c.index("--title") + 1] == "Boom"
    assert "--body-file" in c
    assert "--label" in c and c[c.index("--label") + 1] == "bug"


# ---------------------------------------------------------------------------
# api-token mode
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_api_token_mode_with_env_token_posts_and_prints_html_url(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})

    captured: dict = {}

    def fake_urlopen(req, *_a, **_kw):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(
            201,
            json.dumps({"html_url": "https://github.com/owner/repo/issues/7"})
            .encode("utf-8"),
        )

    monkeypatch.setattr(ru_mod.urllib.request, "urlopen", fake_urlopen)

    result = _invoke(
        tmp_path,
        "--title", "x", "--body", "y", "--mode", "api-token",
        env={"GREATMINDS_UPSTREAM_TOKEN": "secret-token"},
    )
    assert result.exit_code == 0, result.output
    assert "https://github.com/owner/repo/issues/7" in result.output
    assert captured["url"] == "https://api.github.com/repos/owner/repo/issues"
    assert captured["method"] == "POST"
    # Header capitalization varies; check case-insensitively.
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "token secret-token"
    assert headers_lower["content-type"] == "application/json"
    assert captured["body"]["title"] == "x"


def test_api_token_mode_token_from_project_env(tmp_path, monkeypatch):
    _seed_project(
        tmp_path,
        report_cfg={"upstream_repo": "owner/repo"},
        project_env={"GREATMINDS_UPSTREAM_TOKEN": "from-project-env"},
    )

    captured: dict = {}

    def fake_urlopen(req, *_a, **_kw):
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(
            201,
            json.dumps({"html_url": "https://github.com/owner/repo/issues/1"})
            .encode("utf-8"),
        )

    monkeypatch.setattr(ru_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("GREATMINDS_UPSTREAM_TOKEN", raising=False)

    result = _invoke(tmp_path, "--title", "x", "--body", "y",
                     "--mode", "api-token")
    assert result.exit_code == 0, result.output
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "token from-project-env"


def test_api_token_mode_env_wins_over_project_env(tmp_path, monkeypatch):
    _seed_project(
        tmp_path,
        report_cfg={"upstream_repo": "owner/repo"},
        project_env={"GREATMINDS_UPSTREAM_TOKEN": "from-file"},
    )

    captured: dict = {}

    def fake_urlopen(req, *_a, **_kw):
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(
            201,
            json.dumps({"html_url": "ok"}).encode("utf-8"),
        )

    monkeypatch.setattr(ru_mod.urllib.request, "urlopen", fake_urlopen)

    result = _invoke(
        tmp_path, "--title", "x", "--body", "y", "--mode", "api-token",
        env={"GREATMINDS_UPSTREAM_TOKEN": "from-os-env"},
    )
    assert result.exit_code == 0, result.output
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "token from-os-env"


def test_api_token_mode_no_token_exits_2(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})
    monkeypatch.delenv("GREATMINDS_UPSTREAM_TOKEN", raising=False)

    result = _invoke(tmp_path, "--title", "x", "--body", "y",
                     "--mode", "api-token")
    assert result.exit_code == 2
    assert "no token found" in result.output


# ---------------------------------------------------------------------------
# upstream_repo placeholder + mode resolution
# ---------------------------------------------------------------------------


def test_unset_upstream_repo_placeholder_errors(tmp_path):
    _seed_project(tmp_path,
                  report_cfg={"upstream_repo": "<UNSET-UPSTREAM-REPO>"})
    result = _invoke(tmp_path, "--title", "x", "--body", "y",
                     "--mode", "url")
    assert result.exit_code == 2
    assert "upstream_repo not configured" in result.output


def test_mode_flag_overrides_coord_yaml_report_mode(tmp_path, monkeypatch):
    _seed_project(tmp_path,
                  report_cfg={"upstream_repo": "owner/repo", "mode": "gh"})

    opened: list[str] = []
    monkeypatch.setattr(ru_mod.webbrowser, "open",
                        lambda url: opened.append(url))
    # If `gh` mode were chosen, this would be invoked; we assert it isn't.
    sub_calls: list = []
    monkeypatch.setattr(ru_mod.subprocess, "run",
                        lambda *a, **kw: sub_calls.append(a) or
                        subprocess.CompletedProcess([], 0, "", ""))

    result = _invoke(tmp_path, "--title", "x", "--body", "y",
                     "--mode", "url")
    assert result.exit_code == 0
    assert len(opened) == 1
    assert sub_calls == []


def test_default_mode_is_url_when_no_flag_and_no_coord_setting(tmp_path, monkeypatch):
    _seed_project(tmp_path, report_cfg={"upstream_repo": "owner/repo"})

    opened: list[str] = []
    monkeypatch.setattr(ru_mod.webbrowser, "open",
                        lambda url: opened.append(url))

    result = _invoke(tmp_path, "--title", "x", "--body", "y")
    assert result.exit_code == 0
    assert len(opened) == 1


# ---------------------------------------------------------------------------
# task 0034: venv layout reports install kind (PEP 610 direct_url.json) —
# editable vs PyPI wheel vs local file/wheel/sdist vs VCS.
# ---------------------------------------------------------------------------


def _fake_venv(root: Path, dist_info_files: dict[str, str] | None) -> Path:
    """Build a minimal fake venv layout: bin/python + a
    greatminds-X.Y.dist-info/ containing the named files. Pass ``None``
    for ``dist_info_files`` to make greatminds absent (venv with no
    package); omit ``direct_url.json`` to model a registry install.
    """
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "python").write_text("#!shebang\n", encoding="utf-8")
    sp = root / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True, exist_ok=True)
    if dist_info_files is None:
        return root
    dist_info = sp / "greatminds-1.2.0.dist-info"
    dist_info.mkdir()
    for name, content in dist_info_files.items():
        (dist_info / name).write_text(content, encoding="utf-8")
    return root


def test_venv_install_kind_pypi_when_no_direct_url(tmp_path):
    """Modern pip/uv only write direct_url.json for direct-URL installs;
    its absence is the PyPI-registry-install signal."""
    venv = _fake_venv(tmp_path / "v", {"METADATA": "Name: greatminds\n"})
    assert ru_mod._venv_install_kind(venv) == "PyPI wheel"


def test_venv_install_kind_editable_when_direct_url_says_so(tmp_path):
    venv = _fake_venv(tmp_path / "v", {
        "METADATA": "Name: greatminds\n",
        "direct_url.json": json.dumps({
            "url": "file:///home/dev/greatminds",
            "dir_info": {"editable": True},
        }),
    })
    out = ru_mod._venv_install_kind(venv)
    assert out.startswith("editable"), out
    assert "file:///home/dev/greatminds" in out


def test_venv_install_kind_local_wheel(tmp_path):
    venv = _fake_venv(tmp_path / "v", {
        "METADATA": "Name: greatminds\n",
        "direct_url.json": json.dumps({
            "url": "file:///tmp/greatminds-1.2.0-py3-none-any.whl",
            "archive_info": {"hash": "sha256=abc"},
        }),
    })
    assert ru_mod._venv_install_kind(venv) == "local wheel/sdist"


def test_venv_install_kind_vcs(tmp_path):
    venv = _fake_venv(tmp_path / "v", {
        "METADATA": "Name: greatminds\n",
        "direct_url.json": json.dumps({
            "url": "https://github.com/veryviolet/greatminds",
            "vcs_info": {"vcs": "git", "commit_id": "abc"},
        }),
    })
    assert ru_mod._venv_install_kind(venv) == "VCS"


def test_venv_install_kind_absent_when_no_python(tmp_path):
    """Empty dir / venv without bin/python = absent (not 'not installed')."""
    (tmp_path / "empty").mkdir()
    assert ru_mod._venv_install_kind(tmp_path / "empty") == "absent"


def test_venv_install_kind_not_installed_when_no_dist_info(tmp_path):
    """Venv exists, python present, but greatminds is not installed."""
    venv = _fake_venv(tmp_path / "v", None)
    assert ru_mod._venv_install_kind(venv) == "greatminds not installed"


def test_venv_install_kind_bad_direct_url_returns_unknown(tmp_path):
    venv = _fake_venv(tmp_path / "v", {
        "METADATA": "Name: greatminds\n",
        "direct_url.json": "{this is not valid json",
    })
    assert ru_mod._venv_install_kind(venv) == "unknown"


def test_venv_layout_mixed_two_venvs(tmp_path):
    """Two-venv layout: .venv editable + .venv-coord PyPI — common dev shape."""
    _fake_venv(tmp_path / ".venv", {
        "METADATA": "Name: greatminds\n",
        "direct_url.json": json.dumps({
            "url": "file:///opt/greatminds",
            "dir_info": {"editable": True},
        }),
    })
    _fake_venv(tmp_path / ".venv-coord", {"METADATA": "Name: greatminds\n"})
    out = ru_mod._venv_layout(tmp_path)
    assert ".venv/ (editable" in out
    assert ".venv-coord/ (PyPI wheel)" in out


def test_venv_layout_pypi_only_no_longer_says_editable(tmp_path):
    """Regression: the 1.2.0 bug labelled a PyPI uv-installed .venv as
    'editable only'. After the fix it must say 'PyPI wheel'."""
    _fake_venv(tmp_path / ".venv", {"METADATA": "Name: greatminds\n"})
    out = ru_mod._venv_layout(tmp_path)
    assert "PyPI wheel" in out
    assert "editable" not in out


def test_venv_layout_no_venvs(tmp_path):
    assert ru_mod._venv_layout(tmp_path) == "no project-local venv detected"


