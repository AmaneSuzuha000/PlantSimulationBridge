"""Tests for psbridge.experiment.runner.

Local stub policies and engines are defined here on purpose: the shared policy
module (psbridge.scheduling.policies) is owned by another teammate, so the
runner tests must not depend on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psbridge.errors import SimulationError, ValidationError
from psbridge.experiment.contracts import (
    RUN_STATUS_FAILED,
    RUN_STATUS_INVALID,
    RUN_STATUS_VALID,
    ExperimentSpec,
)
from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.runner import (
    CROSS_CHECK_MISMATCH,
    ExperimentRunner,
    run_spec_records,
    schedule_signature,
)
from psbridge.experiment.store import ResultStore
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


# -- local stubs --------------------------------------------------------
class FIFO:
    name = "fifo"
    description = "first ready first served"

    def preference(self, operation):
        return (operation.ready_since,)


class SPT:
    name = "spt"
    description = "shortest processing time"

    def preference(self, operation):
        return (operation.duration,)


class BrokenPolicy:
    name = "broken"
    description = "always raises"

    def preference(self, operation):
        raise RuntimeError("policy exploded")


class Config:
    """Minimal stand-in for psbridge.config.BridgeConfig."""

    backend = "fake"
    engine_kind = "fake_dispatch"


class PoisonEngine:
    """Wraps the real engine but explodes on the first apply()."""

    kind = "poison"

    def __init__(self) -> None:
        self._inner = FakeDispatchEngine()

    def reset(self, instance, seed, horizon=None):
        return self._inner.reset(instance, seed, horizon=horizon)

    def next_decision(self):
        return self._inner.next_decision()

    def apply(self, op_id, machine_id=None):
        raise SimulationError("actuator stuck", details={"op_id": op_id})

    def is_done(self):
        return self._inner.is_done()

    def current_state(self):
        return self._inner.current_state()

    def schedule(self):
        return self._inner.schedule()

    def state_hash(self):
        return self._inner.state_hash()


class OpOnlyEngine:
    """Engine whose apply() only accepts op_id (allowed by the protocol)."""

    kind = "op_only"

    def __init__(self) -> None:
        self._inner = FakeDispatchEngine()

    def reset(self, instance, seed, horizon=None):
        return self._inner.reset(instance, seed, horizon=horizon)

    def next_decision(self):
        return self._inner.next_decision()

    def apply(self, op_id):
        return self._inner.apply(op_id)

    def is_done(self):
        return self._inner.is_done()

    def current_state(self):
        return self._inner.current_state()

    def schedule(self):
        return self._inner.schedule()

    def state_hash(self):
        return self._inner.state_hash()


def two_by_two() -> Instance:
    return Instance(
        instance_id="tiny_01",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 3.0), Operation("J1_O2", "J1", 1, ("M2",), 2.0)),
                due_date=6.0,
            ),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 2.0), Operation("J2_O2", "J2", 1, ("M2",), 4.0)),
                release_time=1.0,
                due_date=6.0,
            ),
        ),
    )


def spec(**kwargs) -> ExperimentSpec:
    defaults = {"experiment_id": "exp_01", "instance": two_by_two(), "policy": "fifo", "seeds": (0,)}
    defaults.update(kwargs)
    return ExperimentSpec(**defaults)


def reference_from(schedule):
    """Build a reference oracle that returns a fixed schedule."""

    def reference(instance, policy, horizon=None):
        return schedule

    return reference


# -- valid runs ---------------------------------------------------------
def test_valid_run_records_metrics_and_provenance() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, replication=0, policy=FIFO())
    assert record.status == RUN_STATUS_VALID
    assert record.error is None
    assert record.metrics is not None
    assert record.metrics.makespan == 9.0
    assert record.metrics.tardy_jobs == 1
    assert [item.op_id for item in record.schedule] == ["J1_O1", "J2_O1", "J1_O2", "J2_O2"]
    assert [(item.machine_id, item.start) for item in record.schedule] == [
        ("M1", 0.0),
        ("M1", 3.0),
        ("M2", 3.0),
        ("M2", 5.0),
    ]
    assert record.validity.valid
    assert record.backend == "fake"
    assert record.engine_kind == "fake_dispatch"
    provenance = record.provenance
    assert provenance.bridge_version == "0.1.0"
    assert provenance.policy == "fifo"
    assert provenance.seed == 0
    assert provenance.duration_jitter == 0.0
    assert provenance.backend == "fake"
    assert provenance.engine_kind == "fake_dispatch"
    assert provenance.instance_hash == two_by_two().content_hash()
    assert provenance.python_version
    assert provenance.platform
    assert provenance.model_hash is None
    assert provenance.timestamp.endswith("+00:00")


def test_run_id_is_deterministic_and_distinct_per_dimension() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    first = runner.run_one(spec(), seed=0, replication=0, policy=FIFO())
    second = runner.run_one(spec(), seed=0, replication=0, policy=FIFO())
    assert first.run_id == second.run_id
    assert first.provenance.instance_hash == second.provenance.instance_hash
    other_seed = runner.run_one(spec(), seed=1, replication=0, policy=FIFO())
    other_policy = runner.run_one(spec(), seed=0, replication=0, policy=SPT())
    other_replication = runner.run_one(spec(), seed=0, replication=1, policy=FIFO())
    assert len({first.run_id, other_seed.run_id, other_policy.run_id, other_replication.run_id}) == 4
    assert "-s0-" in first.run_id and "-r0" in first.run_id


def test_run_id_contains_experiment_and_instance_prefix() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO())
    assert record.run_id.startswith("exp_01-")
    assert record.instance_hash[:12] in record.run_id


def test_unknown_policy_name_is_recorded_as_failed() -> None:
    # A policy name that no policy registry knows must never raise; it becomes a
    # failed run whose error names the policy.
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(policy="not_a_policy"), seed=0, policy="not_a_policy")
    assert record.status == RUN_STATUS_FAILED
    assert "policy" in (record.error or "").lower()


def test_policy_name_string_resolves_when_the_registry_exists() -> None:
    # psbridge.scheduling.policies is owned by another teammate. If it is
    # present, a policy name string must be accepted; if it is absent, the run
    # is recorded as failed instead of raising.
    try:
        from psbridge.scheduling.policies import get_policy  # noqa: F401
    except ImportError:
        pytest.skip("policy registry not available in this checkout")
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(policy="fifo"), seed=0, policy="fifo")
    assert record.status == RUN_STATUS_VALID
    assert record.policy == "fifo"
    assert record.metrics.makespan == 9.0


def test_policy_object_without_preference_is_a_failed_run() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())

    class NotAPolicy:
        name = "not_a_policy"

    record = runner.run_one(spec(), seed=0, policy=NotAPolicy())
    assert record.status == RUN_STATUS_FAILED
    assert "preference" in (record.error or "")


def test_changing_jitter_changes_instance_hash() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    plain = runner.run_one(spec(duration_jitter=0.0), seed=0, policy=FIFO())
    jittered = runner.run_one(spec(duration_jitter=0.2), seed=0, policy=FIFO())
    assert plain.provenance.instance_hash != jittered.provenance.instance_hash
    assert jittered.provenance.duration_jitter == 0.2
    assert jittered.status == RUN_STATUS_VALID


def test_same_spec_and_seed_reproduce_the_same_schedule() -> None:
    runners = [ExperimentRunner(FakeDispatchEngine(), Config()) for _ in range(2)]
    first = runners[0].run_one(spec(duration_jitter=0.1), seed=3, policy=SPT())
    second = runners[1].run_one(spec(duration_jitter=0.1), seed=3, policy=SPT())
    assert first.schedule == second.schedule
    assert first.metrics.to_dict() == second.metrics.to_dict()


# -- horizon ------------------------------------------------------------
def test_horizon_run_is_invalid_when_the_schedule_is_truncated() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(horizon=5.0), seed=0, policy=FIFO())
    assert record.status == RUN_STATUS_INVALID
    assert record.metrics is None
    assert record.error.startswith("VALIDITY_FAILED")
    assert "all_operations_scheduled" in record.error
    assert all(item.end <= 5.0 for item in record.schedule)


def test_valid_horizon_metrics_use_the_horizon_window() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(horizon=18.0), seed=0, policy=FIFO())
    assert record.status == RUN_STATUS_VALID
    assert record.metrics.makespan == 9.0
    assert record.metrics.throughput == pytest.approx(2.0 / 18.0)


# -- failures -----------------------------------------------------------
def test_engine_failure_is_recorded_as_failed_not_raised() -> None:
    runner = ExperimentRunner(PoisonEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO())
    assert record.status == RUN_STATUS_FAILED
    assert record.metrics is None
    assert record.schedule == ()
    assert "SIMULATION_ERROR" in record.error
    assert "actuator stuck" in record.error


def test_policy_failure_is_recorded_as_failed() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=BrokenPolicy())
    assert record.status == RUN_STATUS_FAILED
    assert "policy exploded" in record.error


def test_unsupported_instance_is_recorded_as_failed() -> None:
    flexible = Instance(
        instance_id="flex",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1", "M2"), 1.0),)),),
    )
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(instance=flexible), seed=0, policy=FIFO())
    assert record.status == RUN_STATUS_FAILED
    assert "ENGINE_UNSUPPORTED" in record.error


def test_invalid_seed_is_recorded_as_failed() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=-1, policy=FIFO())
    assert record.status == RUN_STATUS_FAILED
    assert "seed" in record.error


def test_runner_rejects_obviously_wrong_arguments() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    with pytest.raises(ValidationError):
        runner.run_one("not a spec", seed=0)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        runner.run_one(spec(), seed=0, replication=-1)
    with pytest.raises(ValidationError):
        ExperimentRunner(object(), Config())  # type: ignore[arg-type]


def test_engine_with_op_only_apply_is_supported() -> None:
    runner = ExperimentRunner(OpOnlyEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO())
    assert record.status == RUN_STATUS_VALID
    assert record.engine_kind == "op_only"


# -- cross-check --------------------------------------------------------
def test_matching_reference_keeps_the_run_valid() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    engine_only = runner.run_one(spec(), seed=0, policy=FIFO())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference_from(engine_only.schedule))
    assert record.status == RUN_STATUS_VALID
    names = {check.name: check.passed for check in record.validity.checks}
    assert names["cross_check_matches_reference"] is True
    assert record.metrics is not None


def test_reference_can_be_a_plain_callable_without_horizon() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    engine_only = runner.run_one(spec(), seed=0, policy=FIFO())
    expected = engine_only.schedule

    def reference(instance, policy):
        return expected

    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference)
    assert record.status == RUN_STATUS_VALID


def test_reference_time_mismatch_is_invalid_with_cross_check_mismatch() -> None:
    engine_only = ExperimentRunner(FakeDispatchEngine(), Config()).run_one(spec(), seed=0, policy=FIFO())
    shifted = tuple(
        ScheduledOperation(item.op_id, item.job_id, item.machine_id, item.start + 0.5, item.end + 0.5)
        for item in engine_only.schedule
    )
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference_from(shifted))
    assert record.status == RUN_STATUS_INVALID
    assert record.error.startswith(CROSS_CHECK_MISMATCH)
    assert "J1_O1" in record.error
    assert record.metrics is None
    checks = {check.name: check.passed for check in record.validity.checks}
    assert checks["cross_check_matches_reference"] is False


def test_reference_machine_mismatch_is_invalid() -> None:
    engine_only = ExperimentRunner(FakeDispatchEngine(), Config()).run_one(spec(), seed=0, policy=FIFO())
    swapped = tuple(
        ScheduledOperation(item.op_id, item.job_id, "M2" if item.machine_id == "M1" else "M1", item.start, item.end)
        for item in engine_only.schedule
    )
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference_from(swapped))
    assert record.status == RUN_STATUS_INVALID
    assert record.error.startswith(CROSS_CHECK_MISMATCH)


def test_reference_with_a_different_operation_set_is_invalid() -> None:
    engine_only = ExperimentRunner(FakeDispatchEngine(), Config()).run_one(spec(), seed=0, policy=FIFO())
    partial = engine_only.schedule[:-1]
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference_from(partial))
    assert record.status == RUN_STATUS_INVALID
    assert "operation sets differ" in record.error


def test_reference_crash_is_failed_not_invalid() -> None:
    def reference(instance, policy, horizon=None):
        raise RuntimeError("oracle unavailable")

    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference)
    assert record.status == RUN_STATUS_FAILED
    assert record.error.startswith("reference oracle failed")
    assert record.metrics is None


def test_cross_check_is_order_insensitive() -> None:
    engine_only = ExperimentRunner(FakeDispatchEngine(), Config()).run_one(spec(), seed=0, policy=FIFO())
    reversed_schedule = tuple(reversed(engine_only.schedule))
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    record = runner.run_one(spec(), seed=0, policy=FIFO(), reference=reference_from(reversed_schedule))
    assert record.status == RUN_STATUS_VALID


def test_schedule_signature_is_canonical() -> None:
    schedule = (
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
    )
    assert schedule_signature(schedule) == (
        ("J1_O1", "M1", 0.0, 3.0),
        ("J2_O1", "M1", 3.0, 5.0),
    )


# -- batches ------------------------------------------------------------
def test_run_spec_covers_seed_policy_replication_grid() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    records = runner.run_spec(spec(seeds=(0, 1)), [FIFO(), SPT()], replications=2)
    assert len(records) == 2 * 2 * 2
    assert {(record.seed, record.policy, record.replication) for record in records} == {
        (seed, policy.name, replication)
        for seed in (0, 1)
        for policy in (FIFO(), SPT())
        for replication in range(2)
    }
    assert all(record.status == RUN_STATUS_VALID for record in records)
    assert len({record.run_id for record in records}) == len(records)


def test_run_spec_and_batch_reject_empty_inputs() -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    with pytest.raises(ValidationError):
        runner.run_spec(spec(), [])
    with pytest.raises(ValidationError):
        runner.run_spec(spec(), [FIFO()], replications=0)
    with pytest.raises(ValidationError):
        runner.run_batch([], [FIFO()])
    with pytest.raises(ValidationError):
        runner.run_spec("nope", [FIFO()])  # type: ignore[arg-type]


def test_run_batch_keeps_spec_order() -> None:
    other = Instance(
        instance_id="tiny_02",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    records = runner.run_batch(
        [spec(experiment_id="exp_a"), spec(experiment_id="exp_b", instance=other)], [FIFO()]
    )
    assert [record.experiment_id for record in records] == ["exp_a", "exp_b"]
    assert records[1].metrics.makespan == 2.0


def test_run_spec_records_helper() -> None:
    records = run_spec_records(FakeDispatchEngine(), Config(), spec(), [FIFO()])
    assert len(records) == 1
    assert records[0].status == RUN_STATUS_VALID


# -- persistence --------------------------------------------------------
def test_runs_are_persisted_when_a_store_is_supplied(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    runner = ExperimentRunner(FakeDispatchEngine(), Config(), store=store)
    records = runner.run_spec(spec(seeds=(0, 1)), [FIFO()])
    rows = store.read_runs()
    assert len(rows) == 2
    assert [row["run_id"] for row in rows] == [record.run_id for record in records]
    assert rows[0]["metrics"]["makespan"] == 9.0
    summary = json.loads(store.summary_path("exp_01").read_text(encoding="utf-8"))
    assert summary["run_count"] == 2
    assert summary["counts_by_status"]["valid"] == 2


def test_failures_and_invalid_runs_are_persisted_too(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    runner = ExperimentRunner(FakeDispatchEngine(), Config(), store=store)
    runner.run_one(spec(horizon=5.0), seed=0, policy=FIFO())
    runner.run_one(spec(), seed=0, policy=BrokenPolicy())
    runner.run_one(spec(), seed=0, policy=FIFO())
    summary = json.loads(store.write_summary("exp_01").read_text(encoding="utf-8"))
    assert summary["counts_by_status"] == {"valid": 1, "invalid": 1, "failed": 1}
    assert summary["metrics"]["makespan"]["count"] == 1


def test_store_summary_aggregates_policies(tmp_path: Path) -> None:
    store = ResultStore(tmp_path)
    runner = ExperimentRunner(FakeDispatchEngine(), Config(), store=store)
    runner.run_spec(spec(seeds=(0, 1, 2)), [FIFO(), SPT()])
    summary = json.loads(store.summary_path("exp_01").read_text(encoding="utf-8"))
    assert set(summary["by_policy"]) == {"fifo", "spt"}
    assert summary["by_policy"]["fifo"]["run_count"] == 3
    assert summary["by_policy"]["spt"]["valid_run_count"] == 3


def test_runner_without_store_writes_nothing(tmp_path: Path) -> None:
    runner = ExperimentRunner(FakeDispatchEngine(), Config())
    runner.run_one(spec(), seed=0, policy=FIFO())
    assert list(tmp_path.iterdir()) == []
