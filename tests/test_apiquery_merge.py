"""A later evidence file must not erase an earlier measurement.

Measured consequence of getting this wrong: the combined report claimed 5
verified methods where the evidence held 202, because api_surface_by_owner.json
lists every member it never called as "not-executed" and was applied second.
"""
from __future__ import annotations

import json
from pathlib import Path

from psbridge.apiquery import ApiIndex, Member, _apply, normalise_status


def _index(tmp_path: Path, members: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"entries": [
        {"title": n, "owner": o, "kind": k, "page": "", "syntax": ""}
        for o, n, k in members
    ]}), encoding="utf-8")
    return path


def _evidence(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"results": rows}), encoding="utf-8")
    return path


def test_a_bookkeeping_status_never_overrides_a_measurement(tmp_path: Path) -> None:
    index = _index(tmp_path, [("Objects", "closeDialog", "method"),
                              ("Objects", "width", "attribute")])
    measured = _evidence(tmp_path, "measured.json", [
        {"owner": "Objects", "name": "closeDialog", "status": "callable"},
        {"owner": "Objects", "name": "width", "status": "readable"},
    ])
    sweep = _evidence(tmp_path, "sweep.json", [
        {"owner": "Objects", "name": "closeDialog", "status": "not-executed"},
        {"owner": "Objects", "name": "width", "status": "not-executed"},
    ])
    loaded = ApiIndex.load(index, measured, sweep)
    assert [m.status for m in loaded.search("")] == ["callable", "readable"]
    assert all(m.exists for m in loaded.search(""))


def test_order_of_the_evidence_files_does_not_matter(tmp_path: Path) -> None:
    index = _index(tmp_path, [("Objects", "closeDialog", "method")])
    measured = _evidence(tmp_path, "measured.json", [
        {"owner": "Objects", "name": "closeDialog", "status": "callable"}])
    sweep = _evidence(tmp_path, "sweep.json", [
        {"owner": "Objects", "name": "closeDialog", "status": "unmapped"}])
    forward = ApiIndex.load(index, measured, sweep)
    backward = ApiIndex.load(index, sweep, measured)
    assert forward.search("")[0].status == "callable"
    assert backward.search("")[0].status == "callable"


def test_the_probe_vocabulary_is_normalised(tmp_path: Path) -> None:
    """Status aliases map ``exists-needs-args`` and ``needs-args`` consistently."""
    index = _index(tmp_path, [("Objects", "readFile", "method")])
    ev = _evidence(tmp_path, "e.json", [
        {"owner": "Objects", "name": "readFile", "status": "exists-needs-args"}])
    member = ApiIndex.load(index, ev).search("")[0]
    assert member.status == "needs-args"
    assert member.exists
    assert normalise_status("exists-needs-args") == "needs-args"
    assert normalise_status("callable") == "callable"


def test_a_measurement_does_replace_the_documented_placeholder() -> None:
    members = [Member(name="width", owner="Objects", kind="attribute")]
    out = _apply(members, [{"owner": "Objects", "name": "width", "status": "readable"}])
    assert out[0].status == "readable"


def test_an_absent_verdict_survives_a_later_unmapped_row(tmp_path: Path) -> None:
    """Proving something does NOT exist is a measurement too."""
    index = _index(tmp_path, [("Objects", "ghost", "method")])
    absent = _evidence(tmp_path, "a.json", [
        {"owner": "Objects", "name": "ghost", "status": "absent"}])
    sweep = _evidence(tmp_path, "s.json", [
        {"owner": "Objects", "name": "ghost", "status": "unmapped"}])
    member = ApiIndex.load(index, absent, sweep).search("")[0]
    assert member.status == "absent"
    assert not member.exists