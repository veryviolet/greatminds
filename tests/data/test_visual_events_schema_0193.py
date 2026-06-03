"""Tests for task 0193: agent-uttered visual markers.

Agent ends each successful CLI-action reply with one markdown line
per ``schema.visual_events``. Schema carries the emoji + template;
``command_START.yaml`` common: carries the irreducible-minimum prose
instruction telling agents WHEN to emit (the rule cannot be encoded
machine-readably because the agent's behavior is prompt-driven).
"""
from __future__ import annotations

import re

import pytest
import yaml

from greatminds.core.paths import find_canon_dir


# ---------- schema.visual_events ----------


def _load_schema() -> dict:
    return yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}


def test_schema_visual_events_present() -> None:
    """0193: ``schema.yaml > visual_events:`` exists."""
    doc = _load_schema()
    assert doc.get("visual_events") is not None, (
        "0193: schema.yaml missing 'visual_events:' section"
    )


def test_each_event_has_emoji_and_template() -> None:
    """0193: each of the 5 plan events carries emoji + template."""
    events = _load_schema()["visual_events"]
    for name in ("claimed", "finished", "accepted", "rejected",
                 "message_sent"):
        assert name in events, f"0193: visual_events.{name} missing"
        entry = events[name]
        assert "emoji" in entry, f"0193: {name}.emoji missing"
        assert "template" in entry, f"0193: {name}.template missing"


def test_emojis_are_unicode_not_ansi() -> None:
    """0193 contract: emoji is a Unicode character, NOT an ANSI
    escape sequence. ANSI doesn't survive most TUIs; the whole point
    is that the marker renders colored regardless of terminal."""
    events = _load_schema()["visual_events"]
    for name, entry in events.items():
        emoji = entry.get("emoji")
        assert isinstance(emoji, str), f"0193: {name}.emoji not str"
        assert "\x1b" not in emoji, (
            f"0193: {name}.emoji contains ANSI escape; must be Unicode "
            f"only (got {emoji!r})"
        )
        # 1–4 codepoints is a typical emoji width (single + variation
        # selectors / ZWJ sequences).
        assert 1 <= len(emoji) <= 8, (
            f"0193: {name}.emoji unexpectedly long: {emoji!r}"
        )


def test_templates_use_only_known_placeholders() -> None:
    """0193: templates reference only the documented placeholder set.
    A typo (e.g. ``{from_quue}``) would crash agent's str.format at
    runtime — pin the allowed names here."""
    allowed = {
        "emoji", "task_id", "from_queue", "to_queue",
        "from_role", "to_role", "kind", "task_ref", "reason",
    }
    placeholder_re = re.compile(r"\{([a-z_]+)\}")
    events = _load_schema()["visual_events"]
    for name, entry in events.items():
        template = entry["template"]
        names = set(placeholder_re.findall(template))
        unknown = names - allowed
        assert not unknown, (
            f"0193: {name}.template references unknown placeholders "
            f"{sorted(unknown)} — allowed set: {sorted(allowed)}"
        )


def test_templates_include_emoji_placeholder() -> None:
    """0193 contract: every template starts with ``{emoji}`` so the
    rendered marker leads with a colored bullet that scans easily."""
    events = _load_schema()["visual_events"]
    for name, entry in events.items():
        template = entry["template"]
        assert template.startswith("{emoji}"), (
            f"0193: {name}.template must start with {{emoji}} for "
            f"scannable pane-output (got {template!r})"
        )


def test_templates_use_markdown_bold_verb() -> None:
    """0193 contract: each template has a bold verb (the action word).
    Bold + emoji + inline-code is the three-cue pattern operators
    scroll-scan by."""
    events = _load_schema()["visual_events"]
    verbs = {
        "claimed":      "**CLAIMED**",
        "finished":     "**FINISHED**",
        "accepted":     "**ACCEPTED**",
        "rejected":     "**REJECTED**",
        "message_sent": "**SENT**",
    }
    for name, verb in verbs.items():
        template = events[name]["template"]
        assert verb in template, (
            f"0193: {name}.template missing bold verb {verb!r}"
        )


# ---------- COORDINATE.md §16: WHEN-to-emit prose ----------
#
# The agent-facing WHEN-to-emit convention moved from command_START's
# common block to COORDINATE.md §16 (the system prompt is now the static
# bootstrap.md). The marker TEMPLATES stay in schema.visual_events.


def _marker_para() -> str:
    text = (find_canon_dir() / "COORDINATE.md").read_text(encoding="utf-8")
    idx = text.find("Visual event markers")
    assert idx >= 0, (
        "COORDINATE.md missing the «Visual event markers» section")
    return text[idx:idx + 800]


def test_coordinate_carries_visual_marker_paragraph() -> None:
    assert "Visual event markers" in _marker_para()


def test_prose_references_schema_not_inlined_templates() -> None:
    """The paragraph points at ``schema.visual_events`` rather than
    inlining template strings (else schema + prose drift)."""
    para = _marker_para()
    assert "schema.visual_events" in para, (
        "prose must reference schema.visual_events as the source of truth")
    for emoji in ("🔵", "🟢", "🟩", "🔴", "🟣"):
        assert emoji not in para, (
            f"prose paragraph leaks emoji {emoji!r} — must live in schema")


def test_prose_lists_three_trigger_verbs() -> None:
    """The WHEN-to-emit instruction names the three CLI verbs a marker
    follows (task mv, task append-block, inbox send)."""
    para = _marker_para()
    for verb in ("greatminds task mv", "greatminds task append-block",
                 "greatminds inbox send"):
        assert verb in para, f"prose paragraph missing trigger verb {verb!r}"


def test_prose_says_marker_is_last_line() -> None:
    """The marker is the LAST line of the reply (operators scrolling a
    pane should find it without hunting through follow-up text)."""
    para = _marker_para()
    assert "LAST line" in para or "last sentence" in para or \
        "AFTER any follow-up" in para, (
        "prose must state the marker is the LAST line of the reply")
