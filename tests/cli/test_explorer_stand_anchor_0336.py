"""Tests for task 0336: EXPLORER canon correctness — revert the 0331
black-box/no-SSH inversion AND firmly state the stand-anchor invariant.

EXPLORER ALWAYS works ON THE STAND as a real user, WHATEVER the stand's
shape (web → HTTP/browser/curl; host → SSH; local-deployed → where
deployed). The access method follows the product shape; the stand-anchor
is absolute. EXPLORER is strictly forbidden from validating off-stand /
local substitutes. Web-only framing (curl <UI_DEV_URLS>/<BACKEND_URLS>
as THE method) is removed.
"""
from __future__ import annotations

import yaml

from greatminds.core.paths import find_canon_dir


def _schema_explorer() -> dict:
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    return doc["roles"]["EXPLORER"]


# ---------- 0331 inversion is gone ----------


_BANNED_0331 = (
    "review_session_surface", "host_destructive_validation_owner",
    "black_box_cli_rest_only", "ssh_into_stand_hosts",
    "probe_host_filesystem", "validate_logout_login_host_survival",
    "probe_only_via_black_box_cli_and_rest",
    "run_scenarios_against_deployed_stand_black_box_only",
    "author_explorer_review_sessions_black_box_only",
    "emit_host_destructive_steps_to_explorer_review_sessions",
)


def test_schema_has_no_0331_blackbox_fields() -> None:
    raw = (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    for token in _BANNED_0331:
        assert token not in raw, f"0336: 0331 token {token!r} must be gone"


# ---------- stand-anchor invariant (machine-readable) ----------


def test_schema_explorer_stand_anchor_responsibility() -> None:
    resp = set(_schema_explorer().get("responsibilities") or [])
    assert "run_review_sessions_on_live_stand" in resp
    assert "operate_on_the_stand_as_real_user_whatever_its_shape" in resp


def test_schema_explorer_forbids_off_stand_validation() -> None:
    forb = set(_schema_explorer().get("forbidden_actions") or [])
    assert "validate_off_stand_or_local_substitutes" in forb
    # The inverted 0331 host-forbiddens must NOT be present.
    assert "ssh_into_stand_hosts" not in forb


