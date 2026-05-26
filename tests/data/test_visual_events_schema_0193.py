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


# ---------- command_START.yaml common: prose ----------


def test_command_start_carries_visual_marker_paragraph() -> None:
    """0193 prose pin: command_START.yaml's common: block contains
    the «Visual event markers» paragraph that tells agents WHEN to
    emit (the rule isn't machine-encodable for a prompt-driven
    agent)."""
    doc = yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8"
        )
    ) or {}
    common = doc.get("common") or ""
    assert "Visual event markers" in common, (
        "0193: command_START.yaml common: missing the «Visual event "
        "markers» paragraph"
    )


def test_prose_references_schema_not_inlined_templates() -> None:
    """0193 design pin: the prose paragraph points agents at
    ``schema.visual_events`` rather than inlining template strings.
    If templates were duplicated in prose, schema + prose would
    drift on every future change."""
    doc = yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8"
        )
    ) or {}
    common = doc.get("common") or ""
    # Locate the marker paragraph.
    idx = common.find("Visual event markers")
    assert idx >= 0
    # Extract the next ~600 chars as the paragraph window.
    para = common[idx:idx + 700]
    assert "schema.visual_events" in para, (
        "0193: prose must reference schema.visual_events as the "
        "source of truth (not duplicate templates inline)"
    )
    # No literal emoji in the prose — those live in schema.
    for emoji in ("🔵", "🟢", "🟩", "🔴", "🟣"):
        assert emoji not in para, (
            f"0193: prose paragraph leaks emoji {emoji!r} — must live "
            f"in schema only to avoid drift"
        )


def test_prose_lists_three_trigger_verbs() -> None:
    """0193: the WHEN-to-emit instruction names the three CLI verbs
    operators expect a marker after (task mv, task append-block,
    inbox send)."""
    doc = yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8"
        )
    ) or {}
    common = doc["common"]
    idx = common.find("Visual event markers")
    para = common[idx:idx + 700]
    for verb in ("greatminds task mv", "greatminds task append-block",
                 "greatminds inbox send"):
        assert verb in para, (
            f"0193: prose paragraph missing trigger verb {verb!r}"
        )


def test_prose_says_marker_is_last_line() -> None:
    """0193 contract: marker is the LAST sentence of the reply
    (otherwise operators scrolling have to find it amid follow-up
    text). Plan §prose §last sentence."""
    doc = yaml.safe_load(
        (find_canon_dir() / "command_START.yaml").read_text(
            encoding="utf-8"
        )
    ) or {}
    common = doc["common"]
    idx = common.find("Visual event markers")
    para = common[idx:idx + 700]
    assert "AFTER this line" in para or "last sentence" in para, (
        "0193: prose paragraph must state that the marker is the "
        "LAST line of the action's reply"
    )
