"""Schema lifecycle metadata is independent of ACP binding scheduling."""
import yaml
from greatminds.core.paths import find_canon_dir


def test_schema_lifecycle_self_loop_matches_canon() -> None:
    """The metadata does not install or launch a native self-loop."""
    doc = yaml.safe_load(
        (find_canon_dir() / "schema.yaml").read_text(encoding="utf-8")
    ) or {}
    maint = (doc.get("roles") or {}).get("MAINTAINER") or {}
    assert maint.get("lifecycle") == "self-loop"
