"""Tests for psbridge.experiment.store (JSONL log + atomic summaries)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.contracts import (
    RUN_STATUS_FAILED,
    RUN_STATUS_INVALID,
    RUN_STATUS_VALID,
    CheckResult,
    Metrics,
    Provenance,
    RunRecord,
    ValidityReport,
)
from psbridge.experiment.store import (
    RUNS_FILENAME,
    SUMMARY_DIRNAME,
    ResultStore,
    run_record_from_dict,
    summarize_records,
)
from psbridge.schema import ScheduledOperation


def provenance(policy: str = "fifo", seed: int = 0) -> Provenance:
    return Provenance(
        bridge_version="0.1.0",
        instance_hash="a" * 64,
        policy=policy,
        seed=seed,
        duration_jitter=0.0,
        backend="fake",
        engine_kind="fake_dispatch",
        timestamp="2026-01-01T00:00:00+00:00",
        python_version="3.14.0",
        platform="test-platform",
        model_hash=None,
    )


def metrics(makespan: float = 9.0) -> Metrics:
    return Metrics(
        makespan=makespan,
        total_tardiness=1.0,
        weighted_tardiness=2.0,
        max_tardiness=1.0,
        tardy_jobs=1,
        total_flow_time=13.0,
        mean_flow_time=6.5,
        avg_wip=1.4,
        throughput=0.2,
        utilization={"M1": 0.5, "M2": 0.6},
    )


def valid_record(run_id: str = "run_1", policy: str = "fifo", experiment_id: str = "exp_01") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        experiment_id=experiment_id,
        instance_id="tiny",
        instance_hash="a" * 64,
        policy=policy,
        seed=0,
        replication=0,
        status=RUN_STATUS_VALID,
        metrics=metrics(),
        schedule=(ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),),
        validity=ValidityReport.from_checks((CheckResult("all_operations_scheduled", True),)),
        provenance=provenance(policy),
        backend="fake",
        engine_kind="fake_dispatch",
        error=None,
    )


def failed_record(run_id: str = "run_2", policy: str = "fifo") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        experiment_id="exp_01",
        instance_id="tiny",
        instance_hash="a" * 64,
        policy=policy,
        seed=1,
        replication=0,
        status=RUN_STATUS_FAILED,
        metrics=None,
        schedule=(),
        validity=ValidityReport.from_checks(()),
        provenance=provenance(policy, seed=1),
        backend="fake",
        engine_kind="fake_dispatch",
        error="SIMULATION_ERROR: boom",
    )


def invalid_record(run_id: str = "run_3", policy: str = "spt") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        experiment_id="exp_01",
        instance_id="tiny",
        instance_hash="a" * 64,
        policy=policy,
        seed=2,
        replication=0,
        status=RUN_STATUS_INVALID,
        metrics=None,
        schedule=(ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),),
        validity=ValidityReport.from_checks(
            (
                CheckResult("all_operations_scheduled", True),
                CheckResult("no_machine_overlap", False, "M1 overlap"),
            )
        ),
        provenance=provenance(policy, seed=2),
        backend="fake",
        engine_kind="fake_dispatch",
        error="VALIDITY_FAILED: no_machine_overlap",
    )


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    record = valid_record()
    row = store.append_run(record)
    assert row["run_id"] == "run_1"
    assert store.runs_path == tmp_path / RUNS_FILENAME
    rows = store.read_runs()
    assert len(rows) == 1
    assert rows[0] == row
    restored = store.read_records()[0]
    assert restored.to_dict() == record.to_dict()


def test_jsonl_is_one_object_per_line_and_utf8(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    store.append_run(failed_record("run_2"))
    raw = store.runs_path.read_bytes().decode("utf-8")
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2
    for line in lines:
        assert isinstance(json.loads(line), dict)
    assert raw.endswith("\n")


def test_append_appends_rather_than_truncates(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    store.append_run(valid_record("run_2"))
    store.append_run(valid_record("run_3"))
    assert [row["run_id"] for row in store.read_runs()] == ["run_1", "run_2", "run_3"]


def test_read_runs_on_missing_file_is_empty(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "not-created")
    assert store.read_runs() == []
    assert store.read_records() == []


def test_partial_trailing_line_is_tolerated_but_middle_corruption_is_not(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    with open(store.runs_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"run_id": "run_2", "trunc')  # simulated crash mid-write
    rows = store.read_runs()
    assert [row["run_id"] for row in rows] == ["run_1"]
    assert store.malformed_lines == []

    corpus = tmp_path / "corpus"
    corpus_store = ResultStore(corpus)
    corpus_store.append_run(valid_record("run_1"))
    text = corpus_store.runs_path.read_text(encoding="utf-8")
    corpus_store.runs_path.write_text("this is not json\n" + text, encoding="utf-8")
    rows = corpus_store.read_runs()
    assert [row["run_id"] for row in rows] == ["run_1"]
    assert corpus_store.malformed_lines == [1]
    with pytest.raises(ValidationError):
        corpus_store.read_runs(strict=True)


def test_append_rejects_non_records(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    with pytest.raises(ValidationError):
        store.append_run({"run_id": "x"})  # type: ignore[arg-type]


def test_append_runs_returns_count(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    assert store.append_runs([valid_record("run_1"), valid_record("run_2")]) == 2
    assert len(store.read_runs()) == 2


def test_summary_counts_by_status_and_policy(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_runs(
        [
            valid_record("run_1", "fifo"),
            valid_record("run_2", "fifo"),
            invalid_record("run_3", "spt"),
            failed_record("run_4", "fifo"),
        ]
    )
    document = store.summarize()
    assert document["run_count"] == 4
    assert document["valid_run_count"] == 2
    assert document["counts_by_status"] == {"valid": 2, "invalid": 1, "failed": 1}
    assert document["counts_by_policy"]["fifo"] == {"valid": 2, "invalid": 0, "failed": 1}
    assert document["counts_by_policy"]["spt"] == {"valid": 0, "invalid": 1, "failed": 0}
    assert document["by_policy"]["fifo"]["valid_run_count"] == 2
    assert document["by_policy"]["fifo"]["metrics"]["makespan"]["count"] == 2
    assert document["by_policy"]["fifo"]["metrics"]["makespan"]["mean"] == 9.0
    assert document["validity_failure_frequencies"] == {"no_machine_overlap": 1}
    assert document["failure_reasons"] == {"SIMULATION_ERROR: boom": 1}
    assert document["engine_kinds"] == {"fake_dispatch": 4}
    assert document["backends"] == {"fake": 4}


def test_metrics_aggregate_ignores_invalid_and_failed_runs(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_runs([valid_record("run_1"), invalid_record("run_2"), failed_record("run_3")])
    document = store.summarize()
    assert document["metrics"]["makespan"] == {
        "count": 1,
        "mean": 9.0,
        "min": 9.0,
        "max": 9.0,
        "total": 9.0,
    }
    assert document["metrics"]["utilization"]["M1"]["count"] == 1


def test_utilization_is_aggregated_per_machine(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    document = store.summarize()
    assert set(document["metrics"]["utilization"]) == {"M1", "M2"}
    assert document["metrics"]["utilization"]["M2"]["mean"] == pytest.approx(0.6)


def test_summary_is_deterministic_and_atomic(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_runs([valid_record("run_1"), invalid_record("run_2")])
    path = store.write_summary("exp_01")
    assert path == tmp_path / SUMMARY_DIRNAME / "exp_01.json"
    first = path.read_bytes()
    document = json.loads(first.decode("utf-8"))
    assert document["experiment_id"] == "exp_01"
    assert document["run_count"] == 2
    path2 = store.write_summary("exp_01", store.read_records())
    assert path2.read_bytes() == first
    leftovers = [entry.name for entry in path.parent.iterdir() if entry.name != "exp_01.json"]
    assert leftovers == []


def test_summary_selects_only_the_requested_experiment(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    other = valid_record("run_9", experiment_id="exp_02")
    store.append_runs([valid_record("run_1"), other])
    document = json.loads(store.write_summary("exp_01").read_text(encoding="utf-8"))
    assert document["run_count"] == 1
    assert document["counts_by_experiment"] == {"exp_01": 1}


def test_summary_from_explicit_records(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    path = store.write_summary("exp_manual", [valid_record("run_1"), valid_record("run_2")])
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["run_count"] == 2
    assert store.read_runs() == []  # write_summary must not append to the log


def test_global_summary_covers_every_experiment(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_runs([valid_record("run_1"), valid_record("run_2", experiment_id="exp_02")])
    path = store.write_global_summary()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["experiment_id"] == "all_runs"
    assert document["counts_by_experiment"] == {"exp_01": 1, "exp_02": 1}


def test_summary_with_no_runs_is_still_a_document(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    document = store.summarize()
    assert document["run_count"] == 0
    assert document["valid_run_count"] == 0
    assert document["counts_by_status"] == {"valid": 0, "invalid": 0, "failed": 0}
    assert document["metrics"]["makespan"]["count"] == 0
    assert document["metrics"]["makespan"]["mean"] is None


def test_summary_survives_a_corrupt_trailing_line(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    with open(store.runs_path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{ truncated")
    document = store.summarize(store.read_runs())
    assert document["run_count"] == 1


def test_summarize_records_is_a_pure_function() -> None:
    rows = [valid_record("run_1").to_dict(), invalid_record("run_2").to_dict()]
    assert summarize_records(rows) == summarize_records(rows)
    assert summarize_records([])["run_count"] == 0


def test_run_record_from_dict_rejects_incomplete_rows() -> None:
    with pytest.raises(KeyError):
        run_record_from_dict({"run_id": "run_1"})


def test_summary_file_is_utf8_json(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    store.append_run(valid_record("run_1"))
    text = store.write_summary("exp_01").read_text(encoding="utf-8")
    assert text.endswith("\n")
    json.loads(text)
    assert "experiment_id" in text


def test_summary_path_is_sanitised_for_experiment_ids(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    path = store.write_summary("exp/with/slashes", [valid_record("run_1")])
    assert path.parent == tmp_path / SUMMARY_DIRNAME
    assert "/" not in path.name and "\\" not in path.name
