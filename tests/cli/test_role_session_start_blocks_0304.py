"""Tests for task 0304: every implementer/service role doc must
carry a ``## Session start`` block at the top, mirroring the
MAINTAINER pattern.

Pre-0304 only MAINTAINER had this explicit block; DEVELOPER /
STAND-KEEPER / TESTER / READER / TECHNICAL-WRITER / EXPLORER /
UI-DEVELOPER lacked it. Fresh-install agents could (and did, per
upstream issue #4) act without re-reading COORDINATE.md or schema.
0304 puts the canon-read step in their faces.
"""
from __future__ import annotations

from greatminds.core.paths import find_canon_dir


# Roles that 0304 extends. MAINTAINER's existing format is the
# reference; the new blocks mirror it for these roles.
ROLES = (
    "DEVELOPER",
    "UI-DEVELOPER",
    "TESTER",
    "READER",
    "TECHNICAL-WRITER",
    "STAND-KEEPER",
    "EXPLORER",
)


def _role_text(role: str) -> str:
    return (find_canon_dir() / "roles" / f"{role}.md").read_text(
        encoding="utf-8")


# ---------- presence of the block ----------


def test_all_roles_have_session_start_section() -> None:
    """Each of the 8 roles must declare a ``## Session start (0304)``
    section. Missing the section is a contract violation."""
    for role in ROLES:
        text = _role_text(role)
        assert "## Session start (0304)" in text, (
            f"0304: {role}.md missing '## Session start (0304)' section"
        )


def test_session_start_lists_canonical_reads() -> None:
    """The numbered steps must reference the three canon docs
    (COORDINATE.md, schema.yaml, PROJECT.md) so the agent knows
    where to look."""
    for role in ROLES:
        text = _role_text(role)
        # Slice the Session start section to keep the search focused.
        head = text.split("## Session start (0304)", 1)[1].split(
            "\n## ", 1)[0]
        assert "COORDINATE.md" in head, (
            f"0304: {role}.md Session start must reference "
            f"COORDINATE.md"
        )
        assert "schema.yaml" in head, (
            f"0304: {role}.md Session start must reference schema.yaml"
        )
        assert "PROJECT.md" in head, (
            f"0304: {role}.md Session start must reference PROJECT.md"
        )


def test_session_start_mentions_inbox_drain() -> None:
    """Each role must be told to drain its own inbox at session
    start (the ack step). Each role's inbox dir name is the
    role-name lowercased."""
    for role in ROLES:
        text = _role_text(role)
        head = text.split("## Session start (0304)", 1)[1].split(
            "\n## ", 1)[0]
        inbox_dir = f"inbox/{role.lower()}/"
        assert inbox_dir in head, (
            f"0304: {role}.md Session start must mention "
            f"`{inbox_dir}` for the inbox drain step"
        )


def test_session_start_references_role_contract_cli() -> None:
    """Each role's Session start must point at the 0288 CLI
    helper ``greatminds role contract <ROLE>`` so the agent
    has a concrete way to render its schema contract."""
    for role in ROLES:
        text = _role_text(role)
        head = text.split("## Session start (0304)", 1)[1].split(
            "\n## ", 1)[0]
        # Either inline as a code-formatted command or as prose.
        assert f"role contract {role}" in head, (
            f"0304: {role}.md Session start must reference "
            f"`greatminds role contract {role}`"
        )


def test_session_start_carries_inline_invariants() -> None:
    """Each role's Session start must surface the worktree /
    CLI-only / location=ownership invariants inline so a
    distracted agent sees them even without following the
    canon-read step."""
    for role in ROLES:
        text = _role_text(role)
        head = text.split("## Session start (0304)", 1)[1].split(
            "\n## ", 1)[0]
        # The invariants header is a marker; the bullets reference
        # ``greatminds`` (CLI-only) and ``.worktrees/<task-id>`` or
        # ``coordination/`` (mutation surface).
        assert "Inline invariants" in head, (
            f"0304: {role}.md missing 'Inline invariants' marker"
        )


# ---------- ordering: Session start precedes Owns/Does ----------


def test_session_start_precedes_owns_section() -> None:
    """Session start must land BEFORE ``## Owns`` (or ``## Profiles``
    for STAND-KEEPER) so the agent reads it first."""
    for role in ROLES:
        text = _role_text(role)
        ss_pos = text.find("## Session start (0304)")
        # STAND-KEEPER's first post-intro header is ``## Profiles``;
        # others use ``## Owns``.
        first_after = text.find("## Owns")
        if role == "STAND-KEEPER":
            first_after = text.find("## Profiles")
        assert ss_pos != -1 and first_after != -1
        assert ss_pos < first_after, (
            f"0304: {role}.md Session start ({ss_pos}) must "
            f"precede the next-section header ({first_after})"
        )


# ---------- MAINTAINER untouched ----------


def test_maintainer_retains_its_per_tick_start_guidance() -> None:
    """MAINTAINER is the reference pattern: its doc must keep explicit
    start-of-tick guidance. As the self-loop watchdog it phrases this as
    'At each self-loop tick' (plus a '## Bootstrap (self-loop)' block)
    rather than the worker roles' '## Session start' heading."""
    text = (find_canon_dir() / "roles" / "MAINTAINER.md") \
        .read_text(encoding="utf-8")
    assert (
        "At each self-loop tick" in text
        or "## Bootstrap (self-loop)" in text
        or "At each session start" in text
        or "## Session start" in text
    ), "MAINTAINER.md must retain explicit start-of-tick guidance"
