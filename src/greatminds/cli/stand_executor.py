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
        if spec.deploy_prerequisites_only:
            cmd.extend(["--tags", PREREQ_TAG])
        if extra_argv:
            cmd.extend(extra_argv)

        try:
            cp = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                timeout=timeout_seconds,
                env={**os.environ, "ANSIBLE_FORCE_COLOR": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            return (124, f"ansible-playbook timed out after "
                          f"{timeout_seconds}s: {exc}")
        except FileNotFoundError as exc:
            return (127, f"ansible-playbook not executable: {exc}")

        log = ""
        if capture_output:
            log = (cp.stdout or "") + (cp.stderr or "")
        return (cp.returncode, log)


# ---------------------------------------------------------------------------
# MD profile execution (no subprocess — prompt injection)
# ---------------------------------------------------------------------------


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

    The ``deploy_prerequisites_only`` flag is NOT decoded here —
    MD profiles are prose and the flag's effect is documented in
    the prose itself (e.g. "if deploy_prerequisites_only is set,
    stop after step 3"). The flag is still on the spec for the
    LLM to read.
    """
    if spec.format != "md":
        raise GreatMindsError(
            f"execute_md_profile: spec.format must be 'md', "
            f"got {spec.format!r}",
            exit_code=2,
        )
    body = spec.md_content or ""
    rendered = _substitute(body, lease_meta)
    return (0, rendered)


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
