"""Configured ACP manifests and run-backed role diagnostics."""
from pathlib import Path
from typing import Any, NamedTuple
import json

import click

from greatminds.core.paths import find_project_dir, project_runtime_dir
from greatminds.runtime.observation import agent_rows, manifests
from greatminds.cli.chat import terminal_text

def collect_agent_status(coord: Path, role: str) -> dict:
    rows = [r for r in agent_rows(coord.parent) if r['role'] == role.upper()]
    usable = any(row['usable'] for row in rows)
    state = 'running' if usable else (rows[0]['state'] if rows else 'unconfigured')
    return {'role': role.upper(), 'registered': bool(rows), 'runs': rows,
            'alive': any(r['alive'] is True for r in rows), 'usable': usable,
            'state': state, 'reason': state}


def required_live_roles(header: dict[str, Any],
                        blocked_block: "dict[str, Any] | None") -> list[str]:
    """Roles a task's objective needs USABLE before it may resume.

    Opt-in: read from the latest blocked block's ``requires_live_roles``
    (override) else the task header's. Absent / non-list → empty list, so
    tasks that don't declare the field behave exactly as before (no new
    holds, no regression). Normalized upper-case, de-duped,
    order-preserving."""
    raw: Any = None
    if blocked_block is not None and blocked_block.get("requires_live_roles") is not None:
        raw = blocked_block.get("requires_live_roles")
    elif header.get("requires_live_roles") is not None:
        raw = header.get("requires_live_roles")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        if not isinstance(r, str):
            continue
        u = r.strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def wedged_required_roles(coord, roles):
    held = []
    for role in roles:
        try:
            record = collect_agent_status(coord, role)
            if not record['usable']:
                held.append((role, record['state']))
        except Exception:
            held.append((role, 'inspection_unavailable'))
    return held


class LiveRoleHold(NamedTuple):
    """Outcome of the required-live-roles resume gate for one task.

    ``held`` is True when the resume / wake must be withheld — either a
    required role is DEFINITIVELY wedged (``wedged`` non-empty) or a
    DECLARED remote target context could not be resolved
    (``context_error`` set, the conservative 0389 branch)."""

    wedged: "list[tuple[str, str]]"   # (role, observed ACP state)
    context: "str | None"             # declared target context, if any
    context_error: "str | None"       # set iff a declared context is unreachable

    @property
    def held(self) -> bool:
        return bool(self.wedged) or self.context_error is not None


def required_live_roles_context(header: dict[str, Any],
                                blocked_block: "dict[str, Any] | None"
                                ) -> "str | None":
    """0389: opt-in target context where required live roles must be
    evaluated — a greatminds runtime dir or a project dir path.

    Read from the latest blocked block (override) else the task header.
    Absent / non-str / blank → None, meaning «evaluate locally» (the 0388
    behaviour, unchanged). Lets a review_session whose objective targets a
    REMOTE stand require its live roles to be checked against THAT stand's
    runtime state, so a healthy LOCAL role can't falsely unblock a
    remote-targeted campaign."""
    val: Any = None
    if (blocked_block is not None
            and blocked_block.get("requires_live_roles_context") is not None):
        val = blocked_block.get("requires_live_roles_context")
    elif header.get("requires_live_roles_context") is not None:
        val = header.get("requires_live_roles_context")
    if not isinstance(val, str):
        return None
    val = val.strip()
    return val or None


def resolve_live_roles_coord(context: "str | None", local_coord: Path
                             ) -> "tuple[Path | None, str | None]":
    """0389: resolve a declared ``requires_live_roles_context`` to the
    runtime dir whose agents must be inspected.

    Returns ``(coord, error)``:
      * context None        → ``(local_coord, None)`` — local behaviour.
      * context resolvable  → ``(<target coord>, None)``. Accepts either a
        runtime dir directly or a project dir that contains
        ``.greatminds/``; an existing dir without a registry yet still
        resolves (its agents simply read as not-registered).
      * context unreachable → ``(None, <reason>)``. DELIBERATELY NOT
        fail-open: a task that explicitly names a remote target whose
        runtime state can't be found must HOLD with an actionable
        message, never silently resume against nothing (an unreachable
        avatar must not read READY)."""
    if context is None:
        return local_coord, None
    p = Path(context).expanduser()
    # A project dir resolves to its ``.greatminds/`` child; a runtime dir
    # resolves to itself.
    if (p / ".greatminds").is_dir():
        return project_runtime_dir(p), None
    if (p / "coordination").is_dir():
        return p / "coordination", None
    if p.is_dir() and (
        p.name in {".greatminds", "coordination"} or (p / ".runtime").is_dir()
    ):
        return p, None
    return None, (
        f"target context {context!r} not found / unreachable (looked for "
        f"{p / '.greatminds'}/ and {p}/). Deploy / fix the target stand, "
        f"confirm its greatminds runtime exists, then retry the resume.")


def held_live_roles(local_coord: Path, header: dict[str, Any],
                    blocked_block: "dict[str, Any] | None") -> LiveRoleHold:
    """0389: single entry point for the required-live-roles resume gate,
    shared by wake-check (advisory) and the feature_blocked → resume
    validator (enforcing).

    Resolves the (optional) remote target context, then evaluates the
    declared ``requires_live_roles`` against that context. No declared roles
    → never held (opt-in). A declared role list with no context → evaluated
    against ``local_coord`` (the 0388 behaviour)."""
    roles = required_live_roles(header, blocked_block)
    if not roles:
        return LiveRoleHold([], None, None)
    context = required_live_roles_context(header, blocked_block)
    target_coord, ctx_err = resolve_live_roles_coord(context, local_coord)
    if ctx_err is not None:
        return LiveRoleHold([], context, ctx_err)
    assert target_coord is not None  # ctx_err is None ⇒ coord resolved
    wedged = wedged_required_roles(target_coord, roles)
    return LiveRoleHold(wedged, context, None)


def describe_live_role_hold(hold: LiveRoleHold) -> str:
    """Explain which explicitly required ACP roles are currently unavailable."""
    if hold.context_error is not None:
        return hold.context_error
    detail = ", ".join(f"{role} ({state})" for role, state in hold.wedged)
    where = f" in target context {hold.context}" if hold.context else ""
    return (f"required live role(s) unavailable{where}: {detail}. "
            "Inspect greatminds agent status and the corresponding run; "
            "resolve its waiting state or start the required ACP conversation before resuming.")


@click.group(name="agent")
def agent():
    """Inspect configured ACP agents and their actual runs."""


@agent.command(name="tools")
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def agent_tools(project_dir, as_json):
    """List configured ACP manifests; configuration is not live compatibility proof."""
    project = project_dir.resolve() if project_dir else find_project_dir()
    rows = manifests(project)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
    else:
        for row in rows:
            click.echo(terminal_text(f"{row['id']}  acp  adapter={row['adapter_version']}  harness={row['harness_version']}  configured"))


@agent.command(name="status")
@click.argument("roles", nargs=-1)
@click.option("--project-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True)
def agent_status(roles, project_dir, as_json):
    """Show configured bindings, active runs and latest terminal outcomes."""
    project = project_dir.resolve() if project_dir else find_project_dir()
    rows = agent_rows(project)
    if roles:
        wanted = {role.upper() for role in roles}
        rows = [row for row in rows if row['role'] in wanted]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
    elif not rows:
        click.echo('(no matching ACP bindings or runs)')
    else:
        for row in rows:
            click.echo(terminal_text(f"{row['role']} [{row['binding_id']}] {row['agent_id']} {row['state']} "
                                      f"run={row['run_id'] or '-'} task={row['task_id'] or '-'} {row['reason']}"))
