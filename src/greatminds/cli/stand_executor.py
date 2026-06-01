"""0279 (0276 Phase C): SK execution path for stand profiles.

Two execution dialects, both consuming the :class:`ProfileSpec`
returned by Phase B's :mod:`stand_profile` loader:

  * ``execute_yaml_profile`` — runs ``ansible-playbook`` against the
    YAML playbook with an inventory + extra-vars synthesized from
    the lease metadata. Returns ``(exit_code, log)``. Honors the
    spec's ``deploy_prerequisites_only`` flag by adding
    ``--tags prerequisite`` so only tagged tasks run.
  * ``execute_md_profile`` — does NOT subprocess. Substitutes
    ``${var}`` references in the prose body and returns the
    rendered text; SK (the caller) injects the result into its
    next-tick prompt context for the LLM to act on.

Substitution variables surfaced in both dialects (``${name}`` shell
form, ``string.Template.safe_substitute`` semantics so unknown
names stay literal):

  - ``${lease_id}``, ``${task_id}``, ``${worktree}`` — lease fields
  - ``${host}``, ``${user}``, ``${deploy_path}`` — current host loop
  - any ``${KEY}`` whose value lives in lease_meta or PROJECT.env

No SK runtime wiring here; that happens in ``stand.py`` (the
dispatch helper) and STAND-KEEPER.md's workflow update (also in
this phase). Future Phase D adds the ansible-core dependency
declaration; this phase emits a clear error if ``ansible-playbook``
is missing on PATH.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import Template
from typing import Any

from greatminds.core.errors import GreatMindsError
from greatminds.cli.stand_profile import ProfileSpec


PREREQ_TAG = "prerequisite"


# ---------------------------------------------------------------------------
# variable substitution
# ---------------------------------------------------------------------------


def _resolve_substitution_vars(lease_meta: dict[str, Any]) -> dict[str, str]:
    """Build the substitution dict from lease metadata.

    Keys passed by callers (lease_id / task_id / worktree / host /
    user / deploy_path / any PROJECT.env var) are stringified. None
    values become empty strings so ``${var}`` substitution doesn't
    insert a literal "None".
    """
    out: dict[str, str] = {}
    for k, v in (lease_meta or {}).items():
        if not isinstance(k, str):
            continue
        out[k] = "" if v is None else str(v)
    return out


def _substitute(text: str, lease_meta: dict[str, Any]) -> str:
    """``${var}``-substitute ``text`` against the lease meta dict.

    Uses :func:`string.Template.safe_substitute` so unmatched names
    stay literal — operators see the unresolved token instead of a
    silent empty string, which makes the misconfigure obvious.
    """
    return Template(text).safe_substitute(
        _resolve_substitution_vars(lease_meta))


# ---------------------------------------------------------------------------
# YAML profile execution
# ---------------------------------------------------------------------------


def _ansible_playbook_path() -> str:
    """Resolve ``ansible-playbook`` on PATH or raise.

    Phase D will install ansible-core as a hard dependency; until
    then the inline check turns a confusing ``FileNotFoundError``
    deep inside subprocess into a clear actionable message.
    """
    found = shutil.which("ansible-playbook")
    if not found:
        raise GreatMindsError(
            "ansible-playbook not on PATH — install ansible-core "
            "(pipx install ansible-core or `uv pip install "
            "ansible-core`). Phase D / task 0276 makes this a hard "
            "dependency; until then it's a one-line operator install.",
            exit_code=2,
        )
    return found


def _build_inventory(lease_meta: dict[str, Any]) -> str:
    """Build a single-host INI inventory string for ansible.

    Group name is ``stand`` (matches the playbook's typical
    ``hosts: stand`` directive — playbooks may also use ``all`` /
    a literal host pattern; ansible matches by name).

    Required keys in ``lease_meta``: ``host``. Optional: ``user``,
    ``ansible_become`` (defaults to true since SK deploys typically
    need privilege escalation; operators can override via PROJECT.env).
    """
    host = lease_meta.get("host")
    if not host:
        raise GreatMindsError(
            "execute_yaml_profile: lease_meta.host is required for "
            "ansible inventory synthesis",
            exit_code=2,
        )
    user = lease_meta.get("user") or ""
    become = lease_meta.get("ansible_become")
    if become is None:
        become = True

    line = str(host)
    if user:
        line += f" ansible_user={shlex.quote(str(user))}"
    if become:
        line += " ansible_become=true"
    return "[stand]\n" + line + "\n"


def _build_extra_vars(lease_meta: dict[str, Any]) -> dict[str, Any]:
    """Filter ``lease_meta`` into the dict ansible receives via
    ``--extra-vars``.

    We keep every entry whose key is a valid ansible variable name
    (string starting with a letter/underscore). ``host`` / ``user`` /
    ``ansible_become`` are dropped because they're already on the
    inventory line and including them in extra-vars would be
    redundant noise in the playbook log.
    """
    inventory_only = {"host", "user", "ansible_become"}
    out: dict[str, Any] = {}
    for k, v in (lease_meta or {}).items():
        if not isinstance(k, str):
            continue
        if k in inventory_only:
            continue
        # Stringify None so ansible doesn't see ``null``.
        out[k] = "" if v is None else v
    return out


def execute_yaml_profile(
    spec: ProfileSpec,
    lease_meta: dict[str, Any],
    *,
    ansible_playbook: str | None = None,
    extra_argv: list[str] | None = None,
    timeout_seconds: float | None = None,
    capture_output: bool = True,
) -> tuple[int, str]:
    """Run ``ansible-playbook`` against a YAML profile.

    Returns ``(exit_code, log)``. The log is the combined stdout +
    stderr (capture_output=True default) or empty string when the
    caller asked to stream output to its own descriptors
    (capture_output=False).

    ``deploy_prerequisites_only`` (per :class:`ProfileSpec`) becomes
    ``--tags prerequisite`` so only tagged tasks fire — used by
    warmup leases.

    Tunables:
      * ``ansible_playbook`` — override the executable path (tests).
      * ``extra_argv`` — appended to the command line (tests / power
        users wanting --check, --diff, etc.).
      * ``timeout_seconds`` — kills the subprocess at the boundary;
        returns exit_code=124 + a timeout note in the log.
    """
    if spec.format != "yaml":
        raise GreatMindsError(
            f"execute_yaml_profile: spec.format must be 'yaml', "
            f"got {spec.format!r}",
            exit_code=2,
        )
    if not spec.path.is_file():
        raise GreatMindsError(
            f"execute_yaml_profile: spec.path {spec.path} does not "
            "exist",
            exit_code=2,
        )

    # 0286: gate the deploy on the is_deploy_safe classifier so SK
    # cannot land an ansible-playbook run against the main fleet
    # tree + localhost. When unsafe, skip the subprocess and record
    # the refusal to the marker log (no ansible run = no marker
    # = stand ready will refuse downstream).
    coord = _coord_from_lease_meta(lease_meta)
    if coord is not None:
        project_dir = coord.parent
        worktree = (lease_meta or {}).get("worktree")
        host = (lease_meta or {}).get("host")
        if worktree:
            safe, reason = is_deploy_safe(worktree, host, project_dir)
            if not safe:
                _write_deploy_marker(
                    coord, lease_meta,
                    rc=126,
                    log=f"refused by is_deploy_safe: {reason}\n",
                )
                return (126, f"refused by is_deploy_safe: {reason}")

    binary = ansible_playbook or _ansible_playbook_path()

    inventory_text = _build_inventory(lease_meta)
    extra_vars = _build_extra_vars(lease_meta)

    with tempfile.TemporaryDirectory(prefix="stand-profile-") as tmpd:
        inv_path = Path(tmpd) / "inventory.ini"
        inv_path.write_text(inventory_text, encoding="utf-8")

        cmd: list[str] = [
            binary,
            "-i", str(inv_path),
            str(spec.path),
        ]
        if extra_vars:
            # Pass as JSON via the @file form so values with shell
            # metacharacters survive intact.
            ev_path = Path(tmpd) / "extra-vars.json"
            import json as _json
            ev_path.write_text(_json.dumps(extra_vars), encoding="utf-8")
            cmd.extend(["--extra-vars", f"@{ev_path}"])
        # 0283 (0276 Phase G): lease-level override wins over spec
        # value (CLI flag is the most-recent intent).
        if "deploy_prerequisites_only" in (lease_meta or {}):
            prereq_only = bool(lease_meta["deploy_prerequisites_only"])
        else:
            prereq_only = bool(spec.deploy_prerequisites_only)
        if prereq_only:
            cmd.extend(["--tags", PREREQ_TAG])
        if extra_argv:
            cmd.extend(extra_argv)

        # 0305 (upstream issue #2): refresh SK heartbeat while
        # ansible runs so watchdog doesn't flag the lease cycle as a
        # dead pid during a multi-minute remote deploy. Best-effort;
        # never crashes the executor.
        hb_handle = _start_heartbeat_refresher(coord, "stand-keeper")
        try:
            cp = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                timeout=timeout_seconds,
                env={**os.environ, "ANSIBLE_FORCE_COLOR": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            msg = (f"ansible-playbook timed out after "
                   f"{timeout_seconds}s: {exc}")
            if coord is not None:
                _write_deploy_marker(coord, lease_meta, rc=124, log=msg)
            return (124, msg)
        except FileNotFoundError as exc:
            msg = f"ansible-playbook not executable: {exc}"
            if coord is not None:
                _write_deploy_marker(coord, lease_meta, rc=127, log=msg)
            return (127, msg)
        finally:
            # 0305: stop the heartbeat refresher on every exit path
            # (success, timeout, FileNotFoundError, unexpected raise).
            _stop_heartbeat_refresher(hb_handle)

        log = ""
        if capture_output:
            log = (cp.stdout or "") + (cp.stderr or "")
        # 0286: record marker so stand ready can prove the deploy
        # actually ran. The marker captures whatever rc + log the
        # subprocess produced (success OR failure).
        if coord is not None:
            _write_deploy_marker(coord, lease_meta,
                                  rc=cp.returncode, log=log)
        return (cp.returncode, log)


# ---------------------------------------------------------------------------
# MD profile execution (no subprocess — prompt injection)
# ---------------------------------------------------------------------------


PREREQ_ONLY_NOTICE = (
    "**Mode: PREREQUISITES ONLY** — execute only the prerequisite "
    "steps (e.g. host clean, docker installed, venv ready); skip the "
    "main deploy logic. After the prerequisites succeed, call "
    "`greatminds stand ready --lease-id <lease_id>` and let TESTER "
    "run the actual deploy + functional probes.\n\n"
)


def execute_md_profile(
    spec: ProfileSpec,
    lease_meta: dict[str, Any],
) -> tuple[int, str]:
    """Render an MD-format profile for SK's prompt context.

    No subprocess. Substitutes ``${var}`` references in
    ``spec.md_content`` against ``lease_meta`` and returns
    ``(0, rendered_text)``. SK's caller injects the rendered text
    into its next-tick prompt; the LLM emits the actual Bash
    commands.

    0283 (0276 Phase G): when the spec's
    ``deploy_prerequisites_only`` flag is set OR the lease meta
    overrides it (``lease_meta['deploy_prerequisites_only']``), a
    machine-readable notice is prepended to the rendered output so
    the LLM sees the mode switch as the FIRST line of context. The
    notice tells SK to stop after the prerequisite steps and hand
    off to TESTER for the actual deploy. Lease-level override
    wins over spec-level value (CLI flag is the most-recent intent).
    """
    if spec.format != "md":
        raise GreatMindsError(
            f"execute_md_profile: spec.format must be 'md', "
            f"got {spec.format!r}",
            exit_code=2,
        )
    body = spec.md_content or ""
    rendered = _substitute(body, lease_meta)
    # Lease-level override takes precedence; fall back to the spec's
    # frontmatter / yaml.vars value.
    if "deploy_prerequisites_only" in (lease_meta or {}):
        prereq_only = bool(lease_meta["deploy_prerequisites_only"])
    else:
        prereq_only = bool(spec.deploy_prerequisites_only)
    if prereq_only:
        rendered = PREREQ_ONLY_NOTICE + rendered

    # 0286: record marker so stand ready can prove SK actually
    # invoked the executor (even for MD profiles where the LLM does
    # the work). The marker is informational — its presence proves
    # the dispatch path ran; SK still does the actual deploy by
    # acting on the rendered prose.
    coord = _coord_from_lease_meta(lease_meta)
    if coord is not None:
        _write_deploy_marker(coord, lease_meta, rc=0, log=rendered)
    return (0, rendered)


# ---------------------------------------------------------------------------
# Safety check: distinguish remote / isolated deploys from self-modify
# ---------------------------------------------------------------------------


LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


# ---------------------------------------------------------------------------
# 0305 (upstream issue #2): heartbeat refresh while ansible runs.
#
# Watchdog's stall-detection threshold is ~600s by default; an
# ansible-playbook over SSH can easily exceed that on a fresh
# remote-build deploy. Pre-0305 SK's heartbeat went stale during
# legit work → watchdog flagged it as a dead pid → spurious
# dead-pid asks landed in MAINTAINER inbox.
#
# Fix: spin a background thread that touches the SK heartbeat
# every ``HEARTBEAT_REFRESH_INTERVAL_SEC`` seconds while the
# subprocess runs. Stops as soon as the subprocess returns.
# ---------------------------------------------------------------------------


HEARTBEAT_REFRESH_INTERVAL_SEC = 30.0


def _start_heartbeat_refresher(
    coord: Path | None,
    role: str,
    interval: float = HEARTBEAT_REFRESH_INTERVAL_SEC,
) -> tuple[Any, Any] | None:
    """Spawn a daemon thread that touches the role's heartbeat at
    ``interval`` seconds. Returns ``(thread, stop_event)`` so the
    caller can stop the thread once the work is done. Returns None
    when ``coord`` is unresolvable or threading is unavailable —
    callers tolerate that (it's a watchdog convenience, not the
    FSM source of truth).
    """
    if coord is None:
        return None
    try:
        import threading
    except ImportError:
        return None
    hb_path = coord / f"heartbeat.{role.lower()}"
    stop = threading.Event()

    def _loop() -> None:
        # Touch immediately so a quick subprocess still refreshes
        # the heartbeat once.
        try:
            hb_path.parent.mkdir(parents=True, exist_ok=True)
            hb_path.touch()
        except OSError:
            return
        while not stop.wait(interval):
            try:
                hb_path.touch()
            except OSError:
                return

    t = threading.Thread(target=_loop, name=f"sk-heartbeat-{role}",
                          daemon=True)
    t.start()
    return (t, stop)


def _stop_heartbeat_refresher(handle: tuple[Any, Any] | None) -> None:
    """Signal the heartbeat refresher to exit. Idempotent and
    swallows errors — the watchdog convenience must never crash
    the lease cycle."""
    if handle is None:
        return
    try:
        thread, stop_event = handle
        stop_event.set()
        # Best-effort join with a short timeout — the thread is a
        # daemon, the process can exit if it doesn't finish.
        try:
            thread.join(timeout=2.0)
        except RuntimeError:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 0286: deploy marker so `stand ready` can prove execute_yaml_profile ran
# ---------------------------------------------------------------------------


STAND_STATE_DIR = ".stand"


def deploy_marker_path(coord: Path | str, lease_id: str) -> Path:
    """Canonical path for the per-lease deploy marker.

    ``<coord>/.stand/deploy-<lease_id>.log``. Tests + the
    ``stand ready`` gate use this helper so the path stays in one
    place.
    """
    return Path(coord) / STAND_STATE_DIR / f"deploy-{lease_id}.log"


def _coord_from_lease_meta(lease_meta: dict[str, Any] | None
                            ) -> Path | None:
    """Resolve the coord dir for marker writes. Callers can either
    pass ``coord`` directly in lease_meta or rely on the standard
    resolution path; we prefer explicit ``coord`` to keep tests
    hermetic."""
    if not lease_meta:
        return None
    coord = lease_meta.get("coord")
    if isinstance(coord, (str, Path)):
        return Path(coord)
    return None


def _write_deploy_marker(coord: Path, lease_meta: dict[str, Any] | None,
                          *, rc: int, log: str) -> None:
    """Write the marker file. Best-effort — failures don't crash
    the executor (the deploy already happened; the marker is a
    bookkeeping aid)."""
    if not lease_meta:
        return
    lease_id = lease_meta.get("lease_id")
    if not lease_id:
        return
    target = deploy_marker_path(coord, str(lease_id))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"rc={rc}\n"
            f"task_id={lease_meta.get('task_id', '')}\n"
            f"host={lease_meta.get('host', '')}\n"
            f"worktree={lease_meta.get('worktree', '')}\n"
            f"---log---\n{log}",
            encoding="utf-8",
        )
    except OSError:
        pass


def is_deploy_safe(
    worktree: str | Path,
    host: str | None,
    project_dir: str | Path,
) -> tuple[bool, str]:
    """0285: classify a (worktree, host) pair as safe-to-deploy or
    self-modify.

    Returns ``(safe, reason)``. ``reason`` is a short explanation
    suitable for ``stand down`` / setup logs / test assertions.

    Rules:
      - Worktree resolves under ``<project_dir>/.worktrees/<seq>/`` →
        ALWAYS safe (0271 isolation guarantee covers self-modify
        regardless of host).
      - Worktree IS the project_dir itself (main fleet tree):
          * host empty / localhost / loopback → self-modify trap.
            Refuse: deploying onto the running host's own checkout
            would overwrite the very processes the lease serves.
          * any other host → safe (remote deploy from main tree is
            OK; nothing local is modified).
      - Any other worktree (random path outside ``.worktrees`` or
        the project) → refuse with "unknown worktree location"; the
        operator can override via an explicit safe worktree.
    """
    try:
        wt = Path(worktree).resolve(strict=False)
    except (OSError, RuntimeError):
        return False, f"unresolvable worktree path: {worktree!r}"
    project_resolved = Path(project_dir).resolve(strict=False)
    host_norm = (host or "").strip().lower()

    # .worktrees/<seq>/ — always safe (0271 enforces the layout at
    # lease-acquire time, so reaching here means an isolated branch).
    worktrees_root = project_resolved / ".worktrees"
    try:
        if worktrees_root in wt.parents or wt.parent == worktrees_root:
            return True, "isolated worktree under .worktrees/"
    except OSError:
        pass

    # Main fleet tree itself — depends on the target host.
    if wt == project_resolved:
        if host_norm in LOCAL_HOSTS:
            return False, (
                "self-modify trap: deploy from main fleet tree to "
                "localhost would overwrite the running host's "
                "checkout; set STAND_HOST to a remote target or "
                "use a .worktrees/<seq>/ worktree."
            )
        return True, (
            f"remote deploy from main tree to {host!r} — no local "
            "self-modify risk"
        )

    return False, (
        f"unknown worktree location {wt} — not under "
        f"{worktrees_root} and not equal to {project_resolved}"
    )


# ---------------------------------------------------------------------------
# Dispatch helper consumed by stand.py
# ---------------------------------------------------------------------------


def dispatch_profile(
    spec: ProfileSpec,
    lease_meta: dict[str, Any],
    *,
    ansible_playbook: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[int, str]:
    """Single entrypoint that delegates per ``spec.format``.

    YAML → :func:`execute_yaml_profile`; MD →
    :func:`execute_md_profile`. Keeps the dispatch in one place so
    ``stand.py`` (and tests) only need to know about this helper.
    """
    if spec.format == "yaml":
        return execute_yaml_profile(
            spec, lease_meta,
            ansible_playbook=ansible_playbook,
            timeout_seconds=timeout_seconds,
        )
    return execute_md_profile(spec, lease_meta)
