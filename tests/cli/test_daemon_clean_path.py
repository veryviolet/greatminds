"""1.6.3: the daemon unit bakes a CLEAN minimal PATH, not the operator's
raw shell PATH (which dragged in cuda / flutter / plugin bins / another
project's .venv-coord)."""
from __future__ import annotations

from greatminds.cli import daemon


def test_clean_path_project_venv_first_and_no_junk():
    p = daemon._clean_daemon_path("/opt/area/nginarea/.venv/bin/greatminds")
    dirs = p.split(":")
    assert dirs[0] == "/opt/area/nginarea/.venv/bin", \
        "the project's own venv bin must be first (its ansible + greatminds)"
    assert ".venv-coord" not in p, "must NOT leak another project's venv"
    for junk in ("cuda", "flutter", "JetBrains", "plugins/cache", "reflex"):
        assert junk not in p, f"raw-shell junk {junk!r} leaked into the PATH"
    for sysd in ("/usr/bin", "/bin"):
        assert sysd in dirs


def test_clean_path_handles_execstart_with_args():
    p = daemon._clean_daemon_path("/x/.venv/bin/greatminds coordd --project %i")
    assert p.split(":")[0] == "/x/.venv/bin"
