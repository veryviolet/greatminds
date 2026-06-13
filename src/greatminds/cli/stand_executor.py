"""Stand profile execution: run ``ansible-playbook`` against a YAML profile.

``execute_yaml_profile`` consumes the :class:`ProfileSpec` from the
:mod:`stand_profile` loader and runs ansible. greatminds is HOST-AGNOSTIC:
it hands the whole ``PROJECT.env`` (plus lease meta) to ansible as
``--extra-vars`` and runs the playbook in the fleet's coord dir. The
profile author owns host topology by ansible-native means — ``add_host``
from those vars, or a static inventory shipped alongside (auto-discovered
via the fleet's ``ansible.cfg``). No inventory synthesis, no ``${}``
substitution, no required host. ``deploy_prerequisites_only`` adds
``--tags prerequisite`` so only tagged tasks run.

Emits a clear error if ``ansible-playbook`` is missing on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from greatminds.core.errors import GreatMindsError
from greatminds.cli.stand_profile import ProfileSpec


PREREQ_TAG = "prerequisite"


# ---------------------------------------------------------------------------
# variable substitution
# ---------------------------------------------------------------------------


def read_project_env(coord: Path | None) -> dict[str, str]:
    """Parse ``coordination/PROJECT.env`` (``KEY=value`` lines) into a dict.

    PROJECT.env is the SINGLE per-fleet config source, visible to everyone:
    injected into the daemon + every driven agent's process environment
    (systemd ``EnvironmentFile=``), sourced into interactive agent shells,
    and handed to ansible as ``--extra-vars`` here so a profile reads any
    fleet variable as ``{{ KEY }}``. greatminds owns no host topology — the
    profile author targets hosts natively (``add_host`` from these vars, or
    a static inventory shipped alongside)."""
    if coord is None:
        return {}
    f = coord / "PROJECT.env"
    if not f.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                out[key] = val
    except OSError:
        return {}
    return out


# ---------------------------------------------------------------------------
# YAML profile execution
# ---------------------------------------------------------------------------


# 0366 / issue #16: ansible exits rc=0 even when a play matched NO hosts
# (host=None → the play's hosts pattern matches nothing). Such a run
# executes zero tasks and deploys nothing, yet rc=0 made coordd transition
# the stand `preparing → ready` — a false ready that silently invalidates
# every stand_required validation. A vacuous run is converted to this
# synthetic failure rc so deploy_lease transitions the stand `down`.
DEPLOY_NO_HOSTS_RC = 113

_NO_HOST_MARKERS = (
    "no hosts matched",
    "could not match supplied host pattern",
)


def vacuous_deploy_reason(log: str) -> str | None:
    """Return a reason string if an ansible run that exited rc=0 actually
    deployed NOTHING (no hosts matched / the PLAY RECAP lists no host), else
    None. rc=0 from such a run must NOT count as deploy success (issue #16).

    Only meaningful when ansible output was captured; an empty log yields
    None (we cannot prove a no-op, so the raw rc stands)."""
    if not log:
        return None
    low = log.lower()
    for marker in _NO_HOST_MARKERS:
        if marker in low:
            return (f"ansible matched no hosts ({marker!r}) — deploy ran "
                    f"zero tasks, stand was NOT provisioned")
    # A real deploy's PLAY RECAP lists each host with an ``ok=N`` count;
    # a recap with no such line means no host ran the play.
    idx = log.rfind("PLAY RECAP")
    if idx != -1 and not re.search(r"\bok=\d+", log[idx:]):
        return ("ansible PLAY RECAP lists no hosts — deploy ran zero tasks, "
                "stand was NOT provisioned")
    return None


def _sibling_ansible_playbook() -> str | None:
    """Return ansible-playbook next to the active Python, if present.

    ``uv pip install --python /x/venv/bin/python greatminds`` installs both
    the ``greatminds`` and ``ansible-playbook`` console scripts into
    ``/x/venv/bin``. Operators and systemd units may invoke
    ``/x/venv/bin/greatminds`` by absolute path without putting that bin dir
    on PATH, so PATH-only resolution misses a valid hard dependency.
    """
    for base in (Path(sys.executable).parent, Path(sys.executable).resolve().parent):
        candidate = base / "ansible-playbook"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _ansible_playbook_path() -> str:
    """Resolve ``ansible-playbook`` from this venv or PATH, or raise.

    Phase D will install ansible-core as a hard dependency; until
    then the inline check turns a confusing ``FileNotFoundError``
    deep inside subprocess into a clear actionable message.
    """
    sibling = _sibling_ansible_playbook()
    if sibling:
        return sibling
    found = shutil.which("ansible-playbook")
    if not found:
        raise GreatMindsError(
            "ansible-playbook not found next to the active Python and "
            "not on PATH — install ansible-core (pipx install ansible-core "
            "or `uv pip install ansible-core`). Phase D / task 0276 makes "
            "this a hard dependency; until then it's a one-line operator "
            "install.",
            exit_code=2,
        )
    return found


def _build_extra_vars(lease_meta: dict[str, Any]) -> dict[str, Any]:
    """The lease-meta slice ansible receives via ``--extra-vars`` (merged
    under PROJECT.env by the caller). Keeps every string-keyed entry —
    ``worktree`` / ``task_id`` / ``lease_id`` and any extra a caller pins.
    Drops the legacy single-host inventory keys (``host`` / ``user`` /
    ``ansible_become``) that the retired inventory-synthesis used; in the
    host-agnostic scheme hosts come from PROJECT.env (e.g. STAND_HOST_*).
    """
    legacy_inventory = {"host", "user", "ansible_become"}
    out: dict[str, Any] = {}
    for k, v in (lease_meta or {}).items():
        if not isinstance(k, str):
            continue
        if k in legacy_inventory:
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

    # Clean host scheme: greatminds is HOST-AGNOSTIC. The WHOLE PROJECT.env
    # (plus lease meta: worktree / task_id / lease_id …) is handed to ansible
    # as ``--extra-vars`` so a playbook reads any fleet variable as
    # ``{{ KEY }}``. The profile AUTHOR owns host topology by ansible-native
    # means — ``add_host`` from those vars (dynamic, 1 or N nodes), or a
    # static inventory the fleet ships alongside (picked up via ``ansible.cfg``
    # because we run with ``cwd=coord``). We do NOT synthesize an inventory,
    # do NOT ${}-substitute the playbook, and do NOT require a host: ``hosts:``
    # resolves against whatever the author set up (or the implicit localhost
    # an ``add_host`` bootstrap play runs on).
    extra_vars: dict[str, Any] = {**read_project_env(coord),
                                  **_build_extra_vars(lease_meta)}

    with tempfile.TemporaryDirectory(prefix="stand-profile-") as tmpd:
        cmd: list[str] = [binary, str(spec.path)]
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
                # Run in the fleet's coord dir so an author-shipped
                # ansible.cfg / inventory is auto-discovered; absent that,
                # ansible falls back to the implicit localhost an add_host
                # bootstrap play uses.
                cwd=str(coord) if coord is not None else None,
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
        rc = cp.returncode
        # 0366 / issue #16: a vacuous ansible run (no hosts matched, e.g.
        # host=None) exits rc=0 having deployed nothing. Defensively convert
        # it to a failure so deploy_lease transitions the stand `down`
        # instead of falsely `ready`. Only the raw rc==0 case needs this —
        # a real failure already returns non-zero.
        if rc == 0:
            reason = vacuous_deploy_reason(log)
            if reason is not None:
                rc = DEPLOY_NO_HOSTS_RC
                log = f"{log}\n\ngreatminds: deploy failed — {reason}\n"
        # 0286: record marker so stand ready can prove the deploy
        # actually ran. The marker captures whatever rc + log the
        # subprocess produced (success OR failure, incl. the no-hosts
        # conversion above).
        if coord is not None:
            _write_deploy_marker(coord, lease_meta, rc=rc, log=log)
        return (rc, log)


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
      - Worktree resolves under ``<project_dir>/.worktrees/<seq>/`` and has
        a ``.git`` file/dir → safe (0271 isolation guarantee covers
        self-modify regardless of host). A plain directory under
        ``.worktrees`` is refused: it is a deployed/no-git payload or stale
        debris, not a source checkout.
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

    # .worktrees/<seq>/ — safe only when it is still a git worktree. Lease
    # acquire enforces this for new state, but executor is the lower trust
    # boundary for old/corrupt state.yaml entries.
    worktrees_root = project_resolved / ".worktrees"
    try:
        if worktrees_root in wt.parents or wt.parent == worktrees_root:
            if not (wt / ".git").exists():
                return False, (
                    f"worktree path {wt} is under .worktrees/ but is not "
                    "a git worktree (missing .git file/dir); refusing to "
                    "deploy a no-git payload or stale directory"
                )
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

    0302: defensive cross-check — ``spec.name`` (the profile name the
    loader resolved) MUST equal ``lease_meta['profile']`` (the
    requested profile on the lease). Without this gate SK could
    silently run the wrong playbook against the wrong lease (e.g. a
    title-derived spec landing on an unrelated lease). The
    assertion fires BEFORE any subprocess, so a misrouted dispatch
    can never reach ansible-playbook / md prose.
    """
    lease_profile = (lease_meta or {}).get("profile")
    if lease_profile is not None and spec.name != lease_profile:
        raise GreatMindsError(
            f"dispatch_profile: spec.name={spec.name!r} != "
            f"lease.profile={lease_profile!r} — refusing to run "
            "wrong playbook against this lease. The profile loaded "
            "by SK MUST come from lease_meta['profile'], not a "
            "title-derived fallback.",
            exit_code=2,
        )
    # 1.6.0: YAML/ansible only — MD (prose) profiles were removed.
    if spec.format != "yaml":
        raise GreatMindsError(
            f"dispatch_profile: profile {spec.name!r} is "
            f"{spec.format!r}; only YAML/ansible profiles are supported "
            "(1.6.0 removed MD/prose profiles).",
            exit_code=2,
        )
    return execute_yaml_profile(
        spec, lease_meta,
        ansible_playbook=ansible_playbook,
        timeout_seconds=timeout_seconds,
    )
