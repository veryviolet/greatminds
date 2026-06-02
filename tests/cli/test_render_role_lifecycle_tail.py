"""Regression: render-role must gate the inter-tick tail by lifecycle.

Root cause of the 0311 driven hang: the shared ``common`` block in
command_START.yaml injected the self-loop tail ("at the end of every
tick schedule a long sleep / Bash sleep 7200 / you are in an infinite
loop / never end your own session") into EVERY role unconditionally.
For a DRIVEN role that tail is fatal: a headless ``claude -p`` turn that
sleeps or schedules a wake never returns — it hangs the turn and freezes
coordd's run-lock. The driven migration was "avatar-validated" only for
spawn / run-lock / kill-recovery — none of which require a turn to
complete and EXIT — so the hang went undetected.

render-role now substitutes ``{{LIFECYCLE_TAIL}}`` with ``lifecycle_driven``
(driven roles: one tick, then EXIT, never sleep/loop) or ``lifecycle_loop``
(self-loop / chat roles: the sleep-7200 backoff). This test guards both
directions so the self-loop tail can never leak back into a driven role.
"""

from __future__ import annotations

from pathlib import Path

import greatminds
from click.testing import CliRunner

from greatminds.cli.render_role import render_role

CANON_DIR = Path(greatminds.__file__).parent / "data"

# Phrases that, in a headless driven turn, cause the process to never
# return (sleep/loop/never-exit). They MUST NOT appear in a driven render.
SELF_LOOP_MARKERS = [
    "sleep 7200",
    "Bash sleep",
    "infinite loop",
    "Never end your own session",
    "while true; do tick",
    "schedule a long sleep",
]
# Markers proving the driven tail is present.
DRIVEN_MARKERS = ["DRIVEN agent", "Do EXACTLY ONE tick"]


def _render(role: str) -> str:
    runner = CliRunner()
    result = runner.invoke(
        render_role,
        [role, "--canon-dir", str(CANON_DIR), "--project-dir", "/tmp"],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_driven_role_has_no_self_loop_tail():
    out = _render("DEVELOPER")
    for marker in SELF_LOOP_MARKERS:
        assert marker not in out, (
            f"driven DEVELOPER render contains self-loop marker {marker!r} "
            f"— a headless claude -p turn would hang on it"
        )
    for marker in DRIVEN_MARKERS:
        assert marker in out, f"driven render missing driven-tail marker {marker!r}"


def test_all_driven_roles_clean():
    for role in (
        "DEVELOPER", "UI-DEVELOPER", "TESTER", "READER", "STAND-KEEPER",
        "TECHNICAL-WRITER", "EXPLORER", "ARCHITECT-REVIEWER",
    ):
        out = _render(role)
        for marker in SELF_LOOP_MARKERS:
            assert marker not in out, f"{role}: leaked self-loop marker {marker!r}"
        assert "Do EXACTLY ONE tick" in out, f"{role}: missing driven tail"


def test_self_loop_role_keeps_sleep_tail():
    out = _render("MAINTAINER")
    assert "sleep 7200" in out or "infinite loop" in out, (
        "self-loop MAINTAINER must keep the sleep-7200 backoff tail"
    )
    assert "Do EXACTLY ONE tick" not in out, (
        "self-loop role must NOT get the driven one-tick-then-exit tail"
    )


def test_placeholder_fully_substituted():
    for role in ("DEVELOPER", "MAINTAINER", "ARCHITECT-PLANNER"):
        out = _render(role)
        assert "{{LIFECYCLE_TAIL}}" not in out, f"{role}: placeholder left literal"
        assert "{{COMMON}}" not in out, f"{role}: COMMON placeholder left literal"
