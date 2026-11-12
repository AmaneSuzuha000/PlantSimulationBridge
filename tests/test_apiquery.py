from __future__ import annotations

import json
from pathlib import Path

from psbridge.apiquery import ApiIndex, Member


def _index(tmp_path: Path) -> Path:
    payload = {"entries": [
        {"title": "procTime", "owner": "SingleProc", "kind": "attribute", "page": "p1"},
        {"title": "print", "owner": "Objects", "kind": "method", "page": "p2"},
        {"title": "Capacity", "owner": "Buffer", "kind": "attribute", "page": "p3"},
    ]}
    path = tmp_path / "api_index.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_members_are_documented_until_measured(tmp_path: Path) -> None:
    index = ApiIndex.load(_index(tmp_path))
    assert len(index) == 3
    assert {m.status for m in index.search("")} == {"documented"}
    assert index.stats()["verified"] == 0


def test_measurements_attach_by_owner_and_name(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"results": [
        {"owner": "SingleProc", "name": "procTime", "status": "readable"},
        {"owner": "Objects", "name": "print", "status": "hung", "detail": "killed"},
    ]}), encoding="utf-8")
    index = ApiIndex.load(_index(tmp_path), evidence)
    by_name = {m.name: m for m in index.search("")}
    assert by_name["procTime"].status == "readable"
    assert by_name["procTime"].exists is True
    assert by_name["print"].status == "hung"
    assert by_name["print"].exists is False
    # Capacity had no measurement, so it stays documented and is not claimed.
    assert by_name["Capacity"].status == "documented"
    assert by_name["Capacity"].exists is False


def test_missing_evidence_file_is_ignored(tmp_path: Path) -> None:
    index = ApiIndex.load(_index(tmp_path), tmp_path / "nope.json")
    assert len(index) == 3


def test_search_filters_by_kind_and_status(tmp_path: Path) -> None:
    evidence = tmp_path / "e.json"
    evidence.write_text(json.dumps({"results": [
        {"owner": "SingleProc", "name": "procTime", "status": "readable"},
    ]}), encoding="utf-8")
    index = ApiIndex.load(_index(tmp_path), evidence)
    assert [m.name for m in index.search("", kind="attribute")] == ["procTime", "Capacity"]
    assert [m.name for m in index.search("", status="readable")] == ["procTime"]
    assert [m.name for m in index.search("", exists=True)] == ["procTime"]
    assert [m.name for m in index.search("cap")] == ["Capacity"]


def test_existence_vocabulary_is_explicit() -> None:
    # "wrong number of arguments" proves a method is there; "not a valid path"
    # proves it is not. Both are different from "the probe never reached it".
    assert Member("m", "o", "method", status="needs-args").exists is True
    assert Member("m", "o", "method", status="callable").exists is True
    assert Member("m", "o", "method", status="absent").exists is False
    assert Member("m", "o", "method", status="unmapped").exists is False
    assert Member("m", "o", "method", status="documented").exists is False


def test_owners_lists_every_owner(tmp_path: Path) -> None:
    index = ApiIndex.load(_index(tmp_path))
    assert sorted(name for name, _ in index.owners()) == ["Buffer", "Objects", "SingleProc"]
    assert all(count == 1 for _, count in index.owners())