#!/usr/bin/env python3
"""Report blocked tasks ready to wake, malformed deps, and deadlock cycles.

Usage:
    wake_check [--project-dir <dir>] [--canon-dir <dir>] [--quiet]

Scans <project-dir>/coordination/feature_blocked/*.{yaml,md}. For each:
  - parses the latest `blocked` block (or `blocked_block` for legacy md);
  - validates dependency entries match `<queue>/<id>.{yaml,md}`;
  - checks each referenced file actually exists;
  - flags tasks whose ALL deps exist AND none of those deps is itself
    in a wait state (active blocked queue) → READY TO WAKE.

Also detects:
  - malformed deps (wrong format or unknown queue);
  - deadlock cycles (A blocked on B, B blocked on A, etc — DFS over
    the feature_blocked dependency graph);
  - tasks with no blocked block (orphans).

ARCHITECT-REVIEWER is expected to run this at the start of every tick
and act on ready-to-wake / cycle / malformed findings. wake_check
never moves files.

Exit code:
  0 always (informational).
"""
from __future__ import annotations

import re
from pathlib import Path

import click
import yaml

from greatminds.core.paths import find_canon_dir
from greatminds.cli._colors import info, ok, warn

# accept either yaml or md task files in dependency entries during the
# R8 transition period
DEP_RE = re.compile(r"^(?P<queue>[a-z_]+)/(?P<id>[0-9]{1,4}-[a-z0-9-]+)\.(?P<ext>yaml|md)$")


def load_schema_queues(canon_dir: Path) -> set[str]:
    schema_path = canon_dir / "schema.yaml"
    if not schema_path.exists():
        return set()
    try:
        data = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return set()
    return set((data.get("queues") or {}).keys())


def load_task(path: Path) -> tuple[dict, list[dict]]:
    """Return (header_dict, list_of_legacy_blocks_for_md)."""
    if path.suffix == ".yaml":
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}, []
        if not isinstance(data, dict):
            return {}, []
        return data, []
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    header: dict = {}
    blocks: list[dict] = []
    for chunk in parts:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            data = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        if not header and "id" in data:
            header = data
        else:
            blocks.append(data)
    return header, blocks


def latest_blocked(data: dict, legacy_blocks: list[dict]) -> dict | None:
    yaml_blocks = data.get("blocks") or []
    if isinstance(yaml_blocks, list) and yaml_blocks:
        latest = None
        for b in yaml_blocks:
            if isinstance(b, dict) and b.get("kind") == "blocked":
                latest = b
        if latest:
            return latest
    latest_legacy = None
    for b in legacy_blocks:
        for key in ("blocked_block", "blocked"):
            if key in b and isinstance(b[key], dict):
                latest_legacy = b[key]
    return latest_legacy


def dep_path_under_coord(coord: Path, dep: str) -> Path | None:
    m = DEP_RE.match(dep)
    if m is None:
        return None
    p = coord / m.group("queue") / f"{m.group('id')}.{m.group('ext')}"
    if p.is_file():
        return p
    alt_ext = "yaml" if m.group("ext") == "md" else "md"
    p2 = coord / m.group("queue") / f"{m.group('id')}.{alt_ext}"
    if p2.is_file():
        return p2
    return None


def all_blocked_files(coord: Path) -> list[Path]:
    out: list[Path] = []
    d = coord / "feature_blocked"
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir()):
        if f.suffix in (".yaml", ".md") and not f.name.startswith("_TEMPLATE"):
            out.append(f)
    return out


@click.command(
    short_help="report blocked-tasks ready to wake, cycles, malformed deps",
    help=__doc__,
)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path),
              default=None, help="project root containing coordination/ (default: cwd)")
@click.option("--canon-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="canon data dir (default: packaged greatminds.data)")
@click.option("--quiet", is_flag=True, help="suppress no-findings output")
def wake_check(project_dir: Path | None, canon_dir: Path | None, quiet: bool) -> None:
    project_dir = project_dir or Path.cwd()
    canon_dir = canon_dir or find_canon_dir()

    if (project_dir.name == "coordination"
            and (project_dir / "feature_blocked").is_dir()):
        coord = project_dir
    else:
        coord = project_dir / "coordination"
    if not (coord / "feature_blocked").is_dir():
        if not quiet:
            info(f"no feature_blocked/ at {coord}")
        return

    schema_queues = load_schema_queues(canon_dir)
    blocked_files = all_blocked_files(coord)
    blocked_id_set = {f.stem for f in blocked_files}

    graph: dict[str, list[str]] = {}
    task_deps: dict[str, list[str]] = {}
    blocked_meta: dict[str, dict] = {}
    malformed: list[tuple[str, str, str]] = []
    no_block: list[str] = []

    for f in blocked_files:
        tid = f.stem
        header, legacy = load_task(f)
        latest = latest_blocked(header, legacy)
        if latest is None:
            no_block.append(tid)
            continue
        deps_raw = latest.get("dependencies") or []
        if not isinstance(deps_raw, list):
            malformed.append((tid, str(deps_raw), "dependencies must be a list"))
            continue
        deps: list[str] = []
        edges: list[str] = []
        for d in deps_raw:
            if not isinstance(d, str):
                malformed.append((tid, str(d), "dep entry must be string"))
                continue
            m = DEP_RE.match(d)
            if m is None:
                malformed.append((tid, d, "bad format (expected <queue>/<id>.{yaml,md})"))
                continue
            if schema_queues and m.group("queue") not in schema_queues:
                malformed.append((tid, d, f"unknown queue {m.group('queue')!r}"))
                continue
            deps.append(d)
            dep_id = m.group("id")
            if dep_id in blocked_id_set:
                edges.append(dep_id)
        task_deps[tid] = deps
        blocked_meta[tid] = {"resume_to": latest.get("resume_to") or ""}
        graph[tid] = edges

    # DFS cycle detection over the blocked-subgraph
    cycles: list[list[str]] = []
    color: dict[str, int] = {n: 0 for n in graph}  # 0 white 1 gray 2 black

    def dfs(start: str) -> None:
        path: list[str] = []
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = 1
        path.append(start)
        while stack:
            node, i = stack[-1]
            neighbors = graph.get(node, [])
            if i < len(neighbors):
                nxt = neighbors[i]
                stack[-1] = (node, i + 1)
                c = color.get(nxt, 0)
                if c == 1 and nxt in path:
                    idx = path.index(nxt)
                    cycles.append(path[idx:] + [nxt])
                elif c == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, 0))
            else:
                color[node] = 2
                stack.pop()
                if path and path[-1] == node:
                    path.pop()

    for n in list(graph.keys()):
        if color[n] == 0:
            dfs(n)

    in_cycle = {n for cyc in cycles for n in cyc}
    # A dep counts as "done" only if it sits in a terminal queue.
    # Anything in an active queue (incl. feature_blocked itself) means
    # work is not finished, so the blocked task is NOT yet ready.
    terminal_queues = set()
    schema_path = canon_dir / "schema.yaml"
    if schema_path.is_file():
        try:
            schema_data = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            schema_data = {}
        for q, meta in (schema_data.get("queues") or {}).items():
            if isinstance(meta, dict) and meta.get("kind") == "terminal":
                terminal_queues.add(q)
    if not terminal_queues:
        terminal_queues = {"verified", "archive"}

    ready: list[tuple[str, str]] = []
    not_ready: list[tuple[str, list[str]]] = []
    for tid, deps in task_deps.items():
        if tid in in_cycle:
            continue
        unresolved: list[str] = []
        for d in deps:
            p = dep_path_under_coord(coord, d)
            if p is None:
                unresolved.append(f"{d} (missing)")
                continue
            parent = p.parent.name
            if parent not in terminal_queues:
                unresolved.append(f"{d} (in {parent}, not terminal)")
        if unresolved:
            not_ready.append((tid, unresolved))
        else:
            ready.append((tid, blocked_meta[tid]["resume_to"]))

    findings = 0
    if ready:
        findings += len(ready)
        ok(f"READY TO WAKE ({len(ready)}):")
        for tid, resume_to in ready:
            ok(f"  {tid} → {resume_to}")
        click.echo()
    elif not quiet:
        info("ready to wake: 0")

    if not_ready and not quiet:
        info(f"BLOCKED (not yet ready) ({len(not_ready)}):")
        for tid, deps in not_ready:
            info(f"  {tid}: {', '.join(deps)}")
        click.echo()

    if cycles:
        findings += len(cycles)
        warn(f"DEADLOCK CYCLES ({len(cycles)}):")
        seen: set[tuple[str, ...]] = set()
        for cyc in cycles:
            key = tuple(sorted(set(cyc)))
            if key in seen:
                continue
            seen.add(key)
            warn(f"  cycle: {' → '.join(cyc)}")
        click.echo()

    if malformed:
        findings += len(malformed)
        warn(f"MALFORMED DEPENDENCIES ({len(malformed)}):")
        for tid, dep, why in malformed:
            warn(f"  {tid}: {dep!r} — {why}")
        click.echo()

    if no_block:
        findings += len(no_block)
        warn(f"BLOCKED WITHOUT blocked BLOCK ({len(no_block)}):")
        for tid in no_block:
            warn(f"  {tid}")
        click.echo()

    if findings == 0 and not quiet:
        info("(no actionable findings)")


if __name__ == "__main__":
    wake_check()
