"""Tests for task 0284 (0276 Phase H): end-to-end stand-profile
integration.

These tests exercise the full chain from canon preset → setup
seeder → loader → executor for both dialects (YAML / MD). Real
avatar deploy + probe lives in TESTER's lease cycle (stand
evidence). The Python tests pin the WIRING — that every Phase A-G
artifact participates correctly when invoked end-to-end on a
local fixture.

Two lease cycles are simulated:

  1. ``full-deploy`` (YAML) — load via ``stand_profile.load_profile``,
     dispatch via ``stand_executor.dispatch_profile`` with mocked
     ansible-playbook subprocess; assert the argv shape, inventory
     content, prereq-flag honor, and lease-meta substitution.

  2. ``smoke-only`` (MD) — load + dispatch returns rendered prose
     with ``${var}`` substitution + PREREQ_ONLY_NOTICE plumbing.

A skip-if-no-ansible guard lets the YAML test run cleanly on hosts
without ansible-core (CI runners that didn't pip-install greatminds
in full).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from greatminds.cli import setup as setup_mod
from greatminds.cli import stand_executor as se
from greatminds.cli import stand_profile as sp
from greatminds.core.paths import find_canon_dir


# ---------- fixtures ----------


def _project_with_seeded_presets(tmp_path: Path) -> Path:
    """Build a toy project and run the Phase E seeder so the canon
    presets land in ``coordination/stand-profiles/``. Returns the
    coord dir."""
    coord = tmp_path / "proj" / "coordination"
    coord.mkdir(parents=True)
    setup_mod._seed_stand_profiles(coord, find_canon_dir())
    return coord


def _lease(host: str = "avatar",
            user: str = "deploy",
            deploy_path: str = "/srv/greatminds",
            task_id: str = "0284-probe",
            **extras) -> dict:
    base = {
        "lease_id": "lease-uuid-0284",
        "task_id": task_id,
        "worktree": "/opt/greatminds/.worktrees/0284",
        "host": host,
        "user": user,
        "deploy_path": deploy_path,
    }
    base.update(extras)
    return base


# ---------- YAML cycle: full-deploy ----------


def test_full_deploy_yaml_cycle_loads_via_phase_b(
    tmp_path: Path,
) -> None:
    """End-to-end: setup seeds full-deploy.yaml → loader returns
    a ProfileSpec with format='yaml' and the canon playbook content
    accessible via yaml_data."""
    coord = _project_with_seeded_presets(tmp_path)
    spec = sp.load_profile(coord, "full-deploy")
    assert spec.format == "yaml"
    assert spec.path.name == "full-deploy.yaml"
    # Canon ships as list-of-plays.
    assert isinstance(spec.yaml_data, list) and len(spec.yaml_data) == 1
    play = spec.yaml_data[0]
    for field in ("name", "hosts", "tasks"):
        assert field in play


def test_full_deploy_yaml_cycle_dispatches_to_ansible(
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end: load → dispatch wires the ansible-playbook
    subprocess with inventory derived from the lease, ``--extra-vars
    @<json>`` carrying the non-inventory keys, and (when the lease
    sets the prereq flag) ``--tags prerequisite``."""
    coord = _project_with_seeded_presets(tmp_path)

    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        inv_path = Path(cmd[cmd.index("-i") + 1])
        captured["inventory"] = inv_path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="ok\n", stderr="")
    monkeypatch.setattr(se.subprocess, "run", fake_run)

    spec = sp.load_profile(coord, "full-deploy")
    rc, log = se.dispatch_profile(
        spec, _lease(deploy_prerequisites_only=True))
    assert rc == 0 and log == "ok\n"

    cmd = captured["cmd"]
    assert cmd[0] == "/fake/bin/ansible-playbook"
    assert "-i" in cmd
    assert str(spec.path) in cmd
    assert "--extra-vars" in cmd
    # Lease flag → prereq tag.
    assert "--tags" in cmd
    assert "prerequisite" in cmd
    # Inventory carries the lease's host.
    assert "avatar" in captured["inventory"]


def test_full_deploy_yaml_cycle_no_prereq_runs_everything(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without the lease flag → no ``--tags prerequisite``, full
    playbook runs."""
    coord = _project_with_seeded_presets(tmp_path)
    monkeypatch.setattr(se.shutil, "which",
                         lambda _name: "/fake/bin/ansible-playbook")
    captured: list = []
    monkeypatch.setattr(
        se.subprocess, "run",
        lambda cmd, **kw: captured.append(list(cmd)) or
        subprocess.CompletedProcess(args=cmd, returncode=0,
                                      stdout="", stderr=""),
    )

    spec = sp.load_profile(coord, "full-deploy")
    se.dispatch_profile(spec, _lease())  # no prereq flag
    argv = captured[0]
    assert "--tags" not in argv


# ---------- MD cycle: smoke-only ----------


def test_smoke_only_md_cycle_loads_via_phase_b(
    tmp_path: Path,
) -> None:
    """The canon ``smoke-only.yaml`` ships in YAML form (preferred);
    confirm that the MD fallback is loadable when YAML is absent."""
    coord = _project_with_seeded_presets(tmp_path)
    # Remove the YAML so the MD becomes the resolved file (mirrors
    # an operator who removed the playbook to switch to prose).
    (coord / "stand-profiles" / "smoke-only.yaml").unlink()
    spec = sp.load_profile(coord, "smoke-only")
    assert spec.format == "md"
    assert spec.path.name == "smoke-only.md"
    assert "${" in (spec.md_content or "")


def test_smoke_only_md_dispatch_substitutes_lease_vars(
    tmp_path: Path,
) -> None:
    """End-to-end: MD spec rendered with lease meta replaces
    ``${host}`` / ``${user}`` / ``${deploy_path}`` correctly so the
    prose SK injects into its prompt has concrete values."""
    coord = _project_with_seeded_presets(tmp_path)
    (coord / "stand-profiles" / "smoke-only.yaml").unlink()
    spec = sp.load_profile(coord, "smoke-only")

    rc, rendered = se.dispatch_profile(
        spec, _lease(host="avatar-test", deploy_path="/opt/x"))
    assert rc == 0
    assert "avatar-test" in rendered
    assert "/opt/x" in rendered
    # ${unknown} stays literal (safe_substitute pin).
    assert "${unknown_key}" not in rendered  # smoke-only.md doesn't reference it


def test_smoke_only_md_prereq_notice_injected(tmp_path: Path) -> None:
    """When the lease asks for prereq-only mode on an MD profile,
    the PREREQUISITES ONLY notice prepends the rendered body."""
    coord = _project_with_seeded_presets(tmp_path)
    (coord / "stand-profiles" / "smoke-only.yaml").unlink()
    spec = sp.load_profile(coord, "smoke-only")

    rc, rendered = se.dispatch_profile(
        spec, _lease(deploy_prerequisites_only=True))
    assert rc == 0
    assert rendered.startswith("**Mode: PREREQUISITES ONLY**")
    assert "stand ready" in rendered
    assert "TESTER" in rendered


# ---------- optional real ansible exec (skipped in unit suite) ----------


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None,
    reason="ansible-playbook not on PATH; install ansible-core to run",
)
def test_full_deploy_playbook_passes_syntax_check(tmp_path: Path) -> None:
    """When ansible-core is installed, ``ansible-playbook --syntax-check``
    against the seeded canon playbook exits 0 — proves the YAML is
    not just parseable but is a structurally valid playbook from
    ansible's perspective.

    This test is the LOCAL part of Phase H verify; the FULL
    end-to-end (rsync + ssh + install + smoke) is TESTER's lease
    cycle on real avatar — that's where ``stand_evidence`` is
    captured."""
    coord = _project_with_seeded_presets(tmp_path)
    spec = sp.load_profile(coord, "full-deploy")
    # Synth a minimal inventory the playbook accepts (matches the
    # executor's runtime synthesis) — single host in the [stand]
    # group with become disabled (we're not actually running tasks).
    inv = tmp_path / "inv.ini"
    inv.write_text("[stand]\nlocalhost ansible_connection=local\n",
                    encoding="utf-8")
    cp = subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(spec.path),
         "--syntax-check"],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, (
        f"ansible-playbook --syntax-check failed: "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
