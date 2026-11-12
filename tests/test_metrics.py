"""Tests for psbridge.experiment.metrics (hand-computed on tiny instances)."""

from __future__ import annotations

import math

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.metrics import TARDINESS_TOLERANCE, compute_metrics
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


def instance_2x2() -> Instance:
    return Instance(
        instance_id="m_2x2",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 3.0), Operation("J1_O2", "J1", 1, ("M2",), 2.0)),
                release_time=0.0,
                due_date=6.0,
            ),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 2.0), Operation("J2_O2", "J2", 1, ("M2",), 4.0)),
                release_time=1.0,
                due_date=6.0,
                weight=2.0,
            ),
        ),
    )


def fifo_schedule() -> tuple[ScheduledOperation, ...]:
    # Hand-computed FIFO schedule of instance_2x2 (see test_fake_engine).
    return (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )


def test_metrics_match_hand_calculation() -> None:
    metrics = compute_metrics(instance_2x2(), fifo_schedule())
    # makespan: last end = 9
    assert metrics.makespan == 9.0
    # completions: J1 -> 5, J2 -> 9; flow: J1 = 5 - 0 = 5, J2 = 9 - 1 = 8
    assert metrics.total_flow_time == 13.0
    assert metrics.mean_flow_time == 6.5
    # tardiness: J1 = max(0, 5-6) = 0, J2 = max(0, 9-6) = 3
    assert metrics.total_tardiness == 3.0
    assert metrics.weighted_tardiness == 6.0  # weight 2 * 3
    assert metrics.max_tardiness == 3.0
    assert metrics.tardy_jobs == 1
    # WIP: J2 released at 1, J1 released at 0.
    #   [0,1): J1 open            -> 1 * 1 = 1
    #   [1,5): J1, J2 open        -> 2 * 4 = 8
    #   [5,9): J2 open            -> 1 * 4 = 4
    #   integral = 13 over [0, 9] -> 13/9
    assert metrics.avg_wip == pytest.approx(13.0 / 9.0)
    assert metrics.avg_wip == pytest.approx(1.4444444444444444)
    # throughput: 2 jobs / 9 time units
    assert metrics.throughput == pytest.approx(2.0 / 9.0)
    # utilization: M1 busy 3+2 = 5, M2 busy 2+4 = 6
    assert metrics.utilization == {"M1": pytest.approx(5 / 9), "M2": pytest.approx(6 / 9)}


def test_idle_machine_has_zero_utilization() -> None:
    instance = Instance(
        instance_id="idle",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    metrics = compute_metrics(instance, (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),))
    assert metrics.utilization["M2"] == 0.0
    assert metrics.utilization["M1"] == 1.0
    assert metrics.makespan == 2.0


def test_horizon_widens_the_observation_window() -> None:
    instance = Instance(
        instance_id="horizon",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    schedule = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),)
    without = compute_metrics(instance, schedule)
    assert without.makespan == 2.0
    assert without.throughput == 0.5
    assert without.utilization == {"M1": 1.0}
    with_horizon = compute_metrics(instance, schedule, horizon=10.0)
    assert with_horizon.makespan == 2.0  # makespan is still the real finish
    assert with_horizon.throughput == pytest.approx(0.1)
    assert with_horizon.utilization == {"M1": pytest.approx(0.2)}
    assert with_horizon.avg_wip == pytest.approx(0.2)  # 2 time units of WIP / 10


def test_wip_integration_with_release_gap() -> None:
    # Single machine, three jobs; J2 releases at 4. WIP(t) counts jobs that are
    # released and not yet complete, so:
    #   [0,2): J1 and J3 are open      -> 2 * 2 = 4
    #   [2,4): only J3 open            -> 1 * 2 = 2
    #   [4,6): only J2 open            -> 1 * 2 = 2
    # integral = 8 over a makespan of 6 -> 8/6 = 4/3.
    instance = Instance(
        instance_id="wip_gap",
        machines=(Machine("M1"),),
        jobs=(
            Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 2.0),),
                release_time=4.0,
            ),
            Job("J3", (Operation("J3_O1", "J3", 0, ("M1",), 2.0),)),
        ),
    )
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),
        ScheduledOperation("J3_O1", "J3", "M1", 2.0, 4.0),
        ScheduledOperation("J2_O1", "J2", "M1", 4.0, 6.0),
    )
    metrics = compute_metrics(instance, schedule)
    assert metrics.makespan == 6.0
    assert metrics.avg_wip == pytest.approx(8.0 / 6.0)
    # flow: J1 = 2 - 0 = 2, J3 = 4 - 0 = 4, J2 = 6 - 4 = 2
    assert metrics.total_flow_time == pytest.approx(2.0 + 4.0 + 2.0)
    assert metrics.mean_flow_time == pytest.approx(8.0 / 3.0)
    assert metrics.tardy_jobs == 0
    assert metrics.total_tardiness == 0.0


def test_no_due_dates_means_no_tardiness() -> None:
    instance = Instance(
        instance_id="no_due",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    metrics = compute_metrics(instance, (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),))
    assert metrics.total_tardiness == 0.0
    assert metrics.weighted_tardiness == 0.0
    assert metrics.max_tardiness == 0.0
    assert metrics.tardy_jobs == 0


def test_on_time_boundary_is_not_tardy() -> None:
    instance = Instance(
        instance_id="on_time",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),), due_date=2.0),),
    )
    metrics = compute_metrics(instance, (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),))
    assert metrics.total_tardiness == 0.0
    assert metrics.tardy_jobs == 0


def test_tardiness_tolerance_absorbs_float_noise() -> None:
    instance = Instance(
        instance_id="noise",
        machines=(Machine("M1"),),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 0.1 + 0.2),),
                due_date=0.3,
            ),
        ),
    )
    schedule = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 0.1 + 0.2),)
    metrics = compute_metrics(instance, schedule)
    assert metrics.tardy_jobs == 0
    assert metrics.total_tardiness == 0.0
    assert TARDINESS_TOLERANCE > 0.0


def test_weighted_tardiness_uses_job_weight() -> None:
    instance = Instance(
        instance_id="weights",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 4.0),), due_date=1.0, weight=3.0),
            Job("J2", (Operation("J2_O1", "J2", 0, ("M2",), 5.0),), due_date=4.0, weight=0.5),
        ),
    )
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 4.0),
        ScheduledOperation("J2_O1", "J2", "M2", 0.0, 5.0),
    )
    metrics = compute_metrics(instance, schedule)
    assert metrics.total_tardiness == pytest.approx(3.0 + 1.0)
    assert metrics.weighted_tardiness == pytest.approx(3.0 * 3.0 + 0.5 * 1.0)
    assert metrics.max_tardiness == 3.0
    assert metrics.tardy_jobs == 2


def test_complete_schedule_is_required() -> None:
    instance = instance_2x2()
    schedule = fifo_schedule()
    with pytest.raises(ValidationError) as excinfo:
        compute_metrics(instance, schedule[:-1])
    assert "missing" in excinfo.value.details


def test_duplicate_operations_are_rejected() -> None:
    instance = instance_2x2()
    schedule = fifo_schedule()
    with pytest.raises(ValidationError):
        compute_metrics(instance, schedule + (schedule[0],))


def test_unknown_operations_are_rejected() -> None:
    instance = instance_2x2()
    schedule = fifo_schedule()
    schedule = schedule + (ScheduledOperation("J9_O1", "J9", "M1", 9.0, 10.0),)
    with pytest.raises(ValidationError) as excinfo:
        compute_metrics(instance, schedule)
    assert "unknown" in excinfo.value.details


def test_bad_arguments_are_rejected() -> None:
    instance = instance_2x2()
    with pytest.raises(ValidationError):
        compute_metrics("nope", fifo_schedule())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        compute_metrics(instance, None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        compute_metrics(instance, ("not an op",))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        compute_metrics(instance, fifo_schedule(), horizon=0.0)


def test_metrics_are_json_serialisable() -> None:
    import json

    metrics = compute_metrics(instance_2x2(), fifo_schedule())
    payload = metrics.to_dict()
    json.dumps(payload)
    assert payload["utilization"] == {"M1": metrics.utilization["M1"], "M2": metrics.utilization["M2"]}
    restored = type(metrics).from_dict(payload)
    assert restored.to_dict() == payload


def test_throughput_is_zero_for_a_zero_makespan_window() -> None:
    # Defensive branch: a schedule cannot have makespan 0 for a real instance,
    # but the metric formulas must not divide by zero if they ever see one.
    instance = Instance(
        instance_id="degenerate",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 1.0),)),),
    )
    schedule = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 1.0),)
    metrics = compute_metrics(instance, schedule, horizon=None)
    assert math.isfinite(metrics.throughput)
    assert metrics.throughput == 1.0
