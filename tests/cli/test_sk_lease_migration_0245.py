"""Tests for task 0245 (0242c, Phase 3+4 of 0242): SK + TESTER /
EXPLORER migration to the new lease API.

Phase 3 (SK): coordd's inotify watcher includes
``coordination/.stand/`` so state-file transitions wake the daemon
sub-second. The actual SK deploy playbook lives in PROJECT.md
(project-specific) — greatminds canon just exposes the lease/release
CLI + the state-file event.

Phase 4 (TESTER / EXPLORER): role-doc migration to the lease API.
The CLI surface itself was shipped in 0244; this phase rewrites
the workflow docs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from greatminds.cli import coordd as coordd_mod


# ---------- coordd inotify includes .stand ----------






# Role-doc lease-API prose pins removed: the per-role prose docs are
# gone (system prompt is the static bootstrap.md). The lease/release
# workflow + information-asymmetry are pinned in schema (event_triggers
# / forbidden_actions, test_schema_role_contracts_0288) and the stand
# resource model (schema.stand).
