from pathlib import Path

import pytest

from greatminds.core.errors import GreatMindsError
from greatminds.core.schema import inspect_schema_copy, load_schema_snapshot


def test_pinned_schema_cache_uses_contents_and_never_shares_mutable_views():
    from greatminds.core.schema import parse_schema_document
    original = "version: 1\nroles: {TESTER: {requires: [tests]}}\n"
    altered = original.replace("[tests]", "[tests, review]")
    first = parse_schema_document(original)
    first["roles"]["TESTER"]["requires"].clear()
    assert parse_schema_document(original)["roles"]["TESTER"]["requires"] == ["tests"]
    assert parse_schema_document(altered)["roles"]["TESTER"]["requires"] == ["tests", "review"]
    with pytest.raises(ValueError, match="mapping"):
        parse_schema_document("- invalid schema root")


def test_snapshot_survives_source_replacement_and_caller_mutation(tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("version: 1\nroles:\n  TESTER: {lifecycle: driven}\n")
    original = load_schema_snapshot(tmp_path)
    view = original.document
    view["roles"]["TESTER"]["lifecycle"] = "interactive"
    path.write_text("version: 2\nroles: {}\n")
    replacement = load_schema_snapshot(tmp_path)

    assert original.document["roles"]["TESTER"]["lifecycle"] == "driven"
    assert original.version == 1
    assert replacement.version == 2
    assert replacement.sha256 != original.sha256


@pytest.mark.parametrize("content", [b"- list\n", b"", b"{broken", b"\xff"])
def test_invalid_contract_fails_with_source_path(tmp_path, content):
    path = tmp_path / "schema.yaml"
    path.write_bytes(content)
    with pytest.raises(GreatMindsError) as error:
        load_schema_snapshot(tmp_path)
    assert error.value.exit_code == 2
    assert str(path) in str(error.value)


def test_explicit_canon_override_is_authoritative(tmp_path, monkeypatch):
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text("version: 7\n")
    monkeypatch.setenv("GREATMINDS_CANON_DIR", str(canon))
    assert load_schema_snapshot().source == canon / "schema.yaml"


def test_copy_inspection_never_repairs_or_creates_files(tmp_path):
    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "schema.yaml").write_text("version: 1\n")
    snapshot = load_schema_snapshot(canon)
    project = tmp_path / "project"
    assert inspect_schema_copy(snapshot, project)["status"] == "missing"
    assert not project.exists()

    runtime = project / ".greatminds"
    runtime.mkdir(parents=True)
    mirror = runtime / "schema.yaml"
    mirror.write_text(snapshot.text)
    assert inspect_schema_copy(snapshot, project)["status"] == "current"
    mirror.write_text("version: 0\n")
    assert inspect_schema_copy(snapshot, project)["status"] == "drifted"
    assert mirror.read_text() == "version: 0\n"


def test_unreadable_copy_does_not_hide_effective_contract(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text("version: 1\n")
    snapshot = load_schema_snapshot(tmp_path)

    def denied(_path):
        raise PermissionError("fixture denial")

    monkeypatch.setattr(Path, "read_bytes", denied)
    report = inspect_schema_copy(snapshot, tmp_path / "project")
    assert report["status"] == "unreadable"
    assert "fixture denial" in report["error"]
    assert snapshot.document == {"version": 1}
