"""Tests for task 0198: ``greatminds report-upstream`` role-gated to
MAINTAINER per ``schema.report_upstream.permissions.invoke``.

Pre-0198 any role in a /loop session could call ``report-upstream``,
risking duplicate / low-quality upstream issue floods. 0198 closes
the gate: agent context with a non-allowed GREATMINDS_ROLE → refuse.
Operator-mode (env unset) bypasses — the gate is for agent safety,
not a hard lock.
"""
from __future__ import annotations

import pytest
import yaml

from greatminds.cli import report_upstream as ru_mod
from greatminds.core.errors import GreatMindsError
from greatminds.core.paths import find_canon_dir


# ---------- schema pin ----------


def test_schema_has_report_upstream_section() -> None:
    """0198 schema pin: ``schema.report_upstream.permissions.invoke``
    lists exactly MAINTAINER."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    ru = doc.get("report_upstream")
    assert ru is not None, "0198: schema missing 'report_upstream:' section"
    invoke = (ru.get("permissions") or {}).get("invoke") or []
    assert "MAINTAINER" in invoke
    assert ru.get("triage_inbox_target") == "MAINTAINER"
    assert ru.get("upstream_repo_required") is True


# ---------- _check_role_permission ----------


def test_check_allows_maintainer(monkeypatch) -> None:
    """Happy path: GREATMINDS_ROLE=MAINTAINER → no exception."""
    monkeypatch.setenv("GREATMINDS_ROLE", "MAINTAINER")
    ru_mod._check_role_permission()  # no raise


def test_check_allows_maintainer_case_insensitive(monkeypatch) -> None:
    """Case-insensitive role match — agent launchers use various
    case conventions for env vars."""
    monkeypatch.setenv("GREATMINDS_ROLE", "maintainer")
    ru_mod._check_role_permission()  # no raise


def test_check_refuses_developer(monkeypatch) -> None:
    """0198 contract: any non-MAINTAINER agent role is rejected."""
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    with pytest.raises(GreatMindsError) as exc:
        ru_mod._check_role_permission()
    msg = str(exc.value)
    assert "DEVELOPER" in msg
    assert "MAINTAINER" in msg
    # Actionable hint: tell the user the right path.
    assert "inbox send MAINTAINER" in msg


def test_check_refuses_other_implementer_roles(monkeypatch) -> None:
    """Pin: TESTER, UI-DEVELOPER, TECHNICAL-WRITER, etc. all blocked."""
    for role in ("TESTER", "UI-DEVELOPER", "TECHNICAL-WRITER",
                 "READER", "EXPLORER", "ARCHITECT-PLANNER",
                 "ARCHITECT-REVIEWER", "STAND-KEEPER"):
        monkeypatch.setenv("GREATMINDS_ROLE", role)
        with pytest.raises(GreatMindsError):
            ru_mod._check_role_permission()


def test_check_allows_unset_env(monkeypatch) -> None:
    """Operator-mode: no GREATMINDS_ROLE → bypass the gate."""
    monkeypatch.delenv("GREATMINDS_ROLE", raising=False)
    ru_mod._check_role_permission()  # no raise


def test_check_allows_empty_env(monkeypatch) -> None:
    """Empty-string GREATMINDS_ROLE behaves like unset
    (some launchers set env vars to '' for absent values)."""
    monkeypatch.setenv("GREATMINDS_ROLE", "")
    ru_mod._check_role_permission()  # no raise


def test_check_fails_open_on_missing_schema_section(monkeypatch,
                                                      tmp_path) -> None:
    """Defensive: if a downstream project's schema lacks the
    report_upstream section, fail OPEN (allow). CLI shouldn't block
    on misconfigured downstream."""
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setattr(
        "greatminds.core.paths.find_canon_dir",
        lambda: canon,
    )
    ru_mod._check_role_permission()  # no raise


# ---------- integration: report-upstream CLI invocation ----------


def test_cli_invocation_blocked_for_developer(monkeypatch) -> None:
    """End-to-end: invoking the click command as DEVELOPER → non-zero
    exit + actionable error. Pin against accidental gate-removal."""
    from click.testing import CliRunner
    monkeypatch.setenv("GREATMINDS_ROLE", "DEVELOPER")
    runner = CliRunner()
    result = runner.invoke(
        ru_mod.report_upstream,
        ["--title", "test", "--body", "x", "--dry-run"],
    )
    assert result.exit_code != 0
    assert "MAINTAINER" in result.output or "MAINTAINER" in (
        str(result.exception) if result.exception else ""
    )
