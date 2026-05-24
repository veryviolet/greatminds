"""Regression: canon data files must not reference deleted bin/* shims.

The `bin/*` console-script symlinks were removed in efaac33 in favor of
the unified `greatminds <sub>` CLI. Stale `bin/task`, `bin/inbox`, etc.
in canon docs / codex profiles crash downstream codex agents on a fresh
checkout. This grep-style test catches future regressions when someone
copies a stale snippet.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


BANNED = re.compile(
    r"\bbin/(task|inbox|stand|plan|gate_check|wake_check|watchdog)\b"
)
DATA = Path(__file__).resolve().parents[2] / "src" / "greatminds" / "data"


def test_no_stale_bin_refs_in_canon_data():
    offenders: list[str] = []
    for f in DATA.rglob("*"):
        if not f.is_file() or f.suffix not in (".toml", ".md", ".yaml"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if BANNED.search(line):
                offenders.append(
                    f"{f.relative_to(DATA)}:{n}: {line.rstrip()}"
                )
    assert not offenders, (
        "stale bin/* refs in canon data — replace with `greatminds <sub>`:\n"
        + "\n".join(offenders)
    )
