"""Contract tests for the Lead-owned data model and selection rule."""

from __future__ import annotations

import json

import pytest

from psbridge.errors import ExperimentInvalid, ObjectNotFound, ValidationError
from psbridge.experiment.contracts import (
    RUN_STATUS_FAILED,
    RUN_STATUS_INVALID,
    RUN_STATUS_VALID,
    CheckResult,
    ExperimentSpec,
    Metrics,
    Provenance,
    RunRecord,
    ValidityReport,
)
from psbridge.experiment.engine import DecisionPoint, ReadyOperation
from psbridge.scheduling.policy import canonical_tiebreak, select_operation
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


def sample_instance() -> Instance:
    return Instance(
        instance_id="tiny_01",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                job_id="J1",
                release_time=0.0,
                due_date=10.0,
                operations=(
                    Operation("J1_O1", "J1", 0, ("M1",), 3.0),
                    Operation("J1_O2", "J1", 1, ("M2",), 2.0),
                ),
            ),
            Job(
                job_id="J2",
                release_time=1.0,
                due_date=6.0,
                weight=2.0,
                operations=(
                    Operation("J2_O1", "J2", 0, ("M1",), 2.0),
                    Operation("J2_O2", "J2", 1, ("M2",), 4.0),
                ),
            ),
        ),
        meta={"source": "unit-test"},
    )


def test_instance_roundtrip_and_hash() -> None:
    instance = sample_instance()
    payload = instance.to_dict()
    restored = Instance.from_dict(payload)
    assert restored == instance
    assert restored.canonical_json() == instance.canonical_json()
    assert instance.content_hash() == restored.content_hash()
    assert len(instance.content_hash()) == 64
    changed = instance.with_durations({"J1_O1": 5.0})
    assert changed.content_hash() != instance.content_hash()
    assert changed.operation("J1_O1").duration == 5.0
    assert instance.operation("J1_O1").duration == 3.0


def test_instance_lookups() -> None:
    instance = sample_instance()
    assert instance.total_operations == 4
    assert instance.machine_ids() == ("M1", "M2")
    assert instance.job_of("J2_O2").job_id == "J2"
    with pytest.raises(ObjectNotFound):
        instance.job("nope")
    with pytest.raises(ObjectNotFound):
        instance.operation("nope")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda data: data["jobs"][0].__setitem__("job_id", "bad id"),
        lambda data: data["jobs"][0]["operations"][0].__setitem__("index", 1),
        lambda data: data["jobs"][0]["operations"][0].__setitem__("candidates", ["M9"]),
        lambda data: data["jobs"][0]["operations"][0].__setitem__("duration", 0.0),
        lambda data: data["jobs"][0]["operations"][0].__setitem__("duration", float("nan")),
        lambda data: data.__setitem__("machines", []),
        lambda data: data["jobs"][0].__setitem__("due_date", -1.0),
    ],
)
def test_invalid_instances_are_rejected(mutator) -> None:
    data = sample_instance().to_dict()
    mutator(data)
    with pytest.raises(ValidationError):
        Instance.from_dict(data)


def test_unknown_fields_are_rejected() -> None:
    data = sample_instance().to_dict()
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        Instance.from_dict(data)


def test_scheduled_operation_validation() -> None:
    op = ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0)
    assert op.duration == 3.0
    with pytest.raises(ValidationError):
        ScheduledOperation("J1_O1", "J1", "M1", 3.0, 3.0)


def test_experiment_spec_validation() -> None:
    instance = sample_instance()
    spec = ExperimentSpec("exp_01", instance, "fifo", seeds=(0, 1), duration_jitter=0.1)
    assert spec.run_count == 2
    with pytest.raises(ValidationError):
        ExperimentSpec("exp_01", instance, "fifo", seeds=())
    with pytest.raises(ValidationError):
        ExperimentSpec("exp_01", instance, "fifo", seeds=(-1,))
    with pytest.raises(ValidationError):
        ExperimentSpec("exp_01", instance, "fifo", duration_jitter=1.0)
    with pytest.raises(ValidationError):
        ExperimentSpec("bad id", instance, "fifo")
    with pytest.raises(ValidationError):
        ExperimentSpec("exp_01", instance, "not-a-policy")


def test_metrics_validation() -> None:
    metrics = Metrics(10.0, 2.0, 3.0, 2.0, 1, 14.0, 7.0, 1.5, 0.2, {"M1": 0.5})
    assert metrics.to_dict()["utilization"] == {"M1": 0.5}
    with pytest.raises(ValidationError):
        Metrics(-1.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, {})


def test_run_record_status_invariants() -> None:
    instance = sample_instance()
    schedule = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),)
    validity = ValidityReport.from_checks((CheckResult("all_operations_scheduled", True),))
    provenance = Provenance(
        bridge_version="0.1.0",
        instance_hash=instance.content_hash(),
        policy="fifo",
        seed=0,
        duration_jitter=0.0,
        backend="fake",
        engine_kind="fake",
        timestamp="2026-01-01T00:00:00+00:00",
        python_version="3.14.0",
        platform="test",
    )
    metrics = Metrics(3.0, 0.0, 0.0, 0.0, 0, 3.0, 3.0, 1.0, 1.0, {"M1": 1.0})
    valid = RunRecord(
        "run_1", "exp_01", instance.instance_id, instance.content_hash(), "fifo", 0, 0,
        RUN_STATUS_VALID, metrics, schedule, validity, provenance, "fake", "fake", None,
    )
    assert valid.to_dict()["status"] == RUN_STATUS_VALID

    with pytest.raises(ExperimentInvalid):
        RunRecord(
            "run_1", "exp_01", instance.instance_id, instance.content_hash(), "fifo", 0, 0,
            RUN_STATUS_VALID, None, schedule, validity, provenance, "fake", "fake", None,
        )
    failed = RunRecord(
        "run_2", "exp_01", instance.instance_id, instance.content_hash(), "fifo", 0, 0,
        RUN_STATUS_FAILED, None, (), validity, provenance, "fake", "fake", "boom",
    )
    assert failed.to_dict()["status"] == RUN_STATUS_FAILED
    invalid = RunRecord(
        "run_3", "exp_01", instance.instance_id, instance.content_hash(), "fifo", 0, 0,
        RUN_STATUS_INVALID, None, (), validity, provenance, "fake", "fake", "overlap",
    )
    assert invalid.metrics is None


def _ready(op_id: str, index: int, duration: float, ready_since: float, job_id: str = "J1") -> ReadyOperation:
    return ReadyOperation(
        op_id=op_id,
        job_id=job_id,
        index=index,
        duration=duration,
        candidates=("M1",),
        ready_since=ready_since,
        job_release=0.0,
        due_date=10.0,
        weight=1.0,
    )


class _SPT:
    name = "spt"
    description = "shortest processing time"

    def preference(self, operation: ReadyOperation):
        return (operation.duration,)


class _Same:
    name = "same"
    description = "always equal preference"

    def preference(self, operation: ReadyOperation):
        return ()


def test_select_operation_uses_canonical_tiebreak() -> None:
    a = _ready("J1_O1", 0, 5.0, 2.0, "J1")
    b = _ready("J2_O1", 0, 3.0, 1.0, "J2")
    decision = DecisionPoint(0, 2.0, "M1", (a, b), 0, 4)
    assert select_operation(decision, _SPT()).op_id == "J2_O1"
    assert select_operation(decision, _Same()).op_id == "J2_O1"
    assert canonical_tiebreak(b) < canonical_tiebreak(a)


def test_json_serialisable_records() -> None:
    instance = sample_instance()
    json.dumps(instance.to_dict())
    spec = ExperimentSpec("exp_01", instance, "fifo", seeds=(0,))
    assert spec.instance is instance
