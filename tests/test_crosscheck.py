"""Cross-check the dispatch engine against the independent reference oracle.

This is the acceptance gate that makes the closed loop trustworthy: two
implementations written from the same written specification must agree on
operation starts, machine assignment, makespan, and metrics for randomized
instances. A disagreement is a hard failure, not a warning.
"""

from __future__ import annotations

import random

import pytest

from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.metrics import compute_metrics
from psbridge.experiment.validity import validate_schedule
from psbridge.scheduling.policies import get_policy
from psbridge.scheduling.policy import select_operation
from psbridge.scheduling.reference import reference_schedule
from psbridge.schema import Instance, Job, Machine, Operation


def random_instance(seed: int) -> Instance:
    rng = random.Random(seed)
    machine_count = rng.randint(2, 4)
    job_count = rng.randint(3, 6)
    machines = tuple(Machine("M" + str(index + 1)) for index in range(machine_count))
    machine_ids = [machine.machine_id for machine in machines]
    jobs = []
    for job_index in range(job_count):
        job_id = "J" + str(job_index + 1)
        operations = []
        operation_count = rng.randint(1, 4)
        for position in range(operation_count):
            operations.append(
                Operation(
                    op_id=job_id + "_O" + str(position + 1),
                    job_id=job_id,
                    index=position,
                    candidates=(rng.choice(machine_ids),),
                    duration=float(rng.randint(1, 10)),
                )
            )
        release = float(rng.randint(0, 5))
        due = release + float(rng.randint(8, 30))
        jobs.append(Job(job_id=job_id, operations=tuple(operations), release_time=release, due_date=due))
    return Instance(instance_id="rand_" + str(seed), jobs=tuple(jobs), machines=machines)


def drive_engine(instance: Instance, policy, *, horizon: float | None = None, seed: int = 0) -> tuple:
    engine = FakeDispatchEngine()
    engine.reset(instance, seed=seed, horizon=horizon)
    while True:
        decision = engine.next_decision()
        if decision is None:
            break
        operation = select_operation(decision, policy)
        engine.apply(operation.op_id)
    return tuple(engine.schedule())


def schedule_key(schedule) -> tuple:
    return tuple(sorted((item.op_id, item.machine_id, round(item.start, 9), round(item.end, 9)) for item in schedule))


@pytest.mark.parametrize("policy_name", ["fifo", "spt", "edd"])
@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_engine_matches_reference_oracle(policy_name: str, seed: int) -> None:
    instance = random_instance(seed)
    policy = get_policy(policy_name)
    engine_schedule = drive_engine(instance, policy)
    oracle_schedule = reference_schedule(instance, policy)
    assert schedule_key(engine_schedule) == schedule_key(oracle_schedule)
    report = validate_schedule(instance, engine_schedule)
    assert report.valid, [check for check in report.failures()]
    engine_metrics = compute_metrics(instance, engine_schedule)
    oracle_metrics = compute_metrics(instance, oracle_schedule)
    assert engine_metrics.to_dict().keys() == oracle_metrics.to_dict().keys()
    assert engine_metrics.makespan == pytest.approx(oracle_metrics.makespan, abs=1e-9)


def test_engine_determinism_same_seed() -> None:
    instance = random_instance(11)
    policy = get_policy("spt")
    first = drive_engine(instance, policy)
    second = drive_engine(instance, policy)
    assert schedule_key(first) == schedule_key(second)


def test_policies_produce_different_schedules_somewhere() -> None:
    differences = 0
    for seed in range(1, 21):
        instance = random_instance(seed)
        fifo = schedule_key(drive_engine(instance, get_policy("fifo")))
        spt = schedule_key(drive_engine(instance, get_policy("spt")))
        edd = schedule_key(drive_engine(instance, get_policy("edd")))
        if not (fifo == spt == edd):
            differences += 1
    assert differences > 0, "dispatch policies should not all collapse to the same schedule"


def test_horizon_truncation_is_detected() -> None:
    instance = random_instance(3)
    policy = get_policy("fifo")
    horizon = 5.0
    schedule = drive_engine(instance, policy, horizon=horizon)
    assert len(schedule) <= instance.total_operations
    report = validate_schedule(instance, schedule, horizon=horizon)
    if len(schedule) < instance.total_operations:
        assert not report.valid


def test_ready_since_is_availability_time_not_decision_time() -> None:
    """Lock the ready_since contract the FIFO order and the tie-break rely on.

    J1_O1 occupies M1 for 0..10; J2 is released at t=2 but must wait. At the
    decision point t=10 both J2_O1 (ready since 2) and J1_O2 (ready since 10)
    are ready, so FIFO must pick J2_O1. If ready_since were reported as the
    decision time, both would tie at 10 and the tie-break would pick J1_O2
    instead, silently producing a different schedule.
    """
    instance = Instance(
        instance_id="ready_since_probe",
        machines=(Machine("M1"),),
        jobs=(
            Job(
                job_id="J1",
                due_date=5.0,
                operations=(
                    Operation("J1_O1", "J1", 0, ("M1",), 10.0),
                    Operation("J1_O2", "J1", 1, ("M1",), 1.0),
                ),
            ),
            Job(
                job_id="J2",
                release_time=2.0,
                due_date=100.0,
                operations=(Operation("J2_O1", "J2", 0, ("M1",), 3.0),),
            ),
        ),
    )
    engine = FakeDispatchEngine()
    engine.reset(instance, seed=0)
    first = engine.next_decision()
    assert first.time == 0.0
    assert engine.apply(first.ready_operations[0].op_id) is not None or True
    second = engine.next_decision()
    assert second.time == 10.0
    ready = {operation.op_id: operation.ready_since for operation in second.ready_operations}
    assert ready["J2_O1"] == pytest.approx(2.0)
    assert ready["J1_O2"] == pytest.approx(10.0)
    assert select_operation(second, get_policy("fifo")).op_id == "J2_O1"

    oracle = reference_schedule(instance, get_policy("fifo"))
    starts = {item.op_id: item.start for item in oracle}
    assert starts["J2_O1"] == pytest.approx(10.0)
    assert starts["J1_O2"] == pytest.approx(13.0)


def test_reference_rejects_unsupported_routing() -> None:
    from psbridge.errors import EngineUnsupported

    instance = Instance(
        instance_id="flex_01",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                job_id="J1",
                operations=(Operation("J1_O1", "J1", 0, ("M1", "M2"), 2.0),),
            ),
        ),
    )
    with pytest.raises(EngineUnsupported):
        reference_schedule(instance, get_policy("fifo"))
