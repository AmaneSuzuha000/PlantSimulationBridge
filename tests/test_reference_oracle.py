"""Tests for the independent reference oracle (hand-computed schedules).

Every expected schedule in this file was derived by hand from the S1-S9
specification in psbridge/experiment/engine.py: at each event time the machines
are walked in ascending id order, one decision point is emitted per (time,
machine), and exactly one operation starts per decision.

Independence: these expectations come from the written semantics, never from
running psbridge.experiment.fake_engine.
"""

from __future__ import annotations

import json

import pytest

from psbridge.errors import EngineUnsupported, ValidationError
from psbridge.scheduling.policies import get_policy
from psbridge.scheduling.reference import (
    ReferenceStepper,
    brute_force_optimal_makespan,
    lower_bound_makespan,
    reference_run,
    reference_schedule,
)
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


def op(op_id: str, job_id: str, index: int, machine: str, duration: float) -> Operation:
    return Operation(op_id=op_id, job_id=job_id, index=index, candidates=(machine,), duration=duration)


def job(job_id: str, operations, *, release: float = 0.0, due=None, weight: float = 1.0) -> Job:
    return Job(job_id=job_id, operations=tuple(operations), release_time=release, due_date=due, weight=weight)


def inst(instance_id: str, jobs, machines=("M1",)) -> Instance:
    return Instance(instance_id=instance_id, jobs=tuple(jobs), machines=tuple(Machine(m) for m in machines))


def as_tuples(schedule) -> list[tuple[str, str, float, float]]:
    return [(item.op_id, item.machine_id, item.start, item.end) for item in schedule]


def makespan(schedule) -> float:
    return max(item.end for item in schedule)


# -- basic semantics ---------------------------------------------------------
def test_single_job_single_machine() -> None:
    instance = inst("one", [job("J1", [op("J1_O1", "J1", 0, "M1", 3.0)])])
    assert as_tuples(reference_schedule(instance, get_policy("fifo"))) == [("J1_O1", "M1", 0.0, 3.0)]


def test_release_time_delays_start() -> None:
    instance = inst("rel", [job("J1", [op("J1_O1", "J1", 0, "M1", 2.0)], release=5.0)])
    assert as_tuples(reference_schedule(instance, get_policy("fifo"))) == [("J1_O1", "M1", 5.0, 7.0)]


def test_machine_order_is_ascending_machine_id() -> None:
    """S4: at equal time M1 is served before M2 even if the instance lists M2 first."""
    instance = inst(
        "machines",
        [
            job("J1", [op("J1_O1", "J1", 0, "M2", 1.0)]),
            job("J2", [op("J2_O1", "J2", 0, "M1", 1.0)]),
        ],
        machines=("M2", "M1"),
    )
    run = reference_run(instance, get_policy("fifo"))
    assert [decision.machine_id for decision in run.decisions] == ["M1", "M2"]
    assert as_tuples(run.schedule) == [("J2_O1", "M1", 0.0, 1.0), ("J1_O1", "M2", 0.0, 1.0)]


def test_precedence_is_respected() -> None:
    instance = inst("prec", [job("J1", [op("J1_O1", "J1", 0, "M1", 2.0), op("J1_O2", "J1", 1, "M1", 3.0)])])
    assert as_tuples(reference_schedule(instance, get_policy("fifo"))) == [
        ("J1_O1", "M1", 0.0, 2.0),
        ("J1_O2", "M1", 2.0, 5.0),
    ]


def test_idle_machine_waits_for_next_ready_time() -> None:
    """S5: an idle machine jumps straight to the next ready time (no busy wait)."""
    instance = inst("wait", [job("J1", [op("J1_O1", "J1", 0, "M1", 1.0)], release=7.0)])
    run = reference_run(instance, get_policy("fifo"))
    assert [decision.time for decision in run.decisions] == [7.0]
    assert as_tuples(run.schedule) == [("J1_O1", "M1", 7.0, 8.0)]


# -- hand-computed comparison instance ---------------------------------------
#
# M1: J1_O1(3), J2_O1(2)   M2: J1_O2(2), J2_O2(4)
# J1 release 0, due 10;  J2 release 1, due 6.
#
# t=0  M1: J1_O1 ready (J2 not released)        -> J1_O1 [0,3)
#      M2: nothing ready (J1_O2 needs J1_O1)     -> advance to next event
# t=1  M2 free but nothing ready; next event is t=3 (J1_O1 end, J1_O2 ready)
# t=3  M1: J2_O1 ready                          -> J2_O1 [3,5)
#      M2: J1_O2 ready                          -> J1_O2 [3,5)
# t=5  M2: J2_O2 ready                          -> J2_O2 [5,9)
# makespan 9.  Every decision had exactly one candidate, so all three policies
# produce this same schedule.
FIFO_EXPECTED = [
    ("J1_O1", "M1", 0.0, 3.0),
    ("J2_O1", "M1", 3.0, 5.0),
    ("J1_O2", "M2", 3.0, 5.0),
    ("J2_O2", "M2", 5.0, 9.0),
]


def comparison_instance() -> Instance:
    return inst(
        "compare",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 3.0), op("J1_O2", "J1", 1, "M2", 2.0)], release=0.0, due=10.0),
            job("J2", [op("J2_O1", "J2", 0, "M1", 2.0), op("J2_O2", "J2", 1, "M2", 4.0)], release=1.0, due=6.0),
        ],
        machines=("M1", "M2"),
    )


def test_hand_computed_comparison_schedule() -> None:
    run = reference_run(comparison_instance(), get_policy("fifo"))
    assert as_tuples(run.schedule) == FIFO_EXPECTED
    assert run.complete is True
    assert run.truncated is False
    assert run.final_time == 9.0


def test_all_policies_agree_when_no_decision_has_a_choice() -> None:
    for name in ("fifo", "spt", "edd"):
        assert as_tuples(reference_schedule(comparison_instance(), get_policy(name))) == FIFO_EXPECTED


# -- policy differences (each hand-computed) ---------------------------------
def test_fifo_vs_spt_differ_when_jobs_compete() -> None:
    """FIFO orders by job id here; SPT takes the short job first.

    J1: M1(10), J2: M1(1), both released at t=0.
    FIFO tie-break -> J1 then J2: [0,10) + [10,11)
    SPT            -> J2 then J1: [0,1) + [1,11)
    """
    instance = inst(
        "fifo_vs_spt",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 10.0)], release=0.0),
            job("J2", [op("J2_O1", "J2", 0, "M1", 1.0)], release=0.0),
        ],
    )
    assert as_tuples(reference_schedule(instance, get_policy("fifo"))) == [
        ("J1_O1", "M1", 0.0, 10.0),
        ("J2_O1", "M1", 10.0, 11.0),
    ]
    assert as_tuples(reference_schedule(instance, get_policy("spt"))) == [
        ("J2_O1", "M1", 0.0, 1.0),
        ("J1_O1", "M1", 1.0, 11.0),
    ]


def test_spt_vs_edd_differ_when_due_dates_oppose_durations() -> None:
    """J1: 1 time unit, due 20.  J2: 5 time units, due 2.  Both released at 0."""
    instance = inst(
        "spt_vs_edd",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 1.0)], due=20.0),
            job("J2", [op("J2_O1", "J2", 0, "M1", 5.0)], due=2.0),
        ],
    )
    assert as_tuples(reference_schedule(instance, get_policy("spt"))) == [
        ("J1_O1", "M1", 0.0, 1.0),
        ("J2_O1", "M1", 1.0, 6.0),
    ]
    assert as_tuples(reference_schedule(instance, get_policy("edd"))) == [
        ("J2_O1", "M1", 0.0, 5.0),
        ("J1_O1", "M1", 5.0, 6.0),
    ]


def test_fifo_vs_edd_differ_on_ready_since() -> None:
    """A long first operation delays a short urgent one; ready_since decides FIFO.

    M1 only.  J1: M1(10) then M1(1), due 5.  J2: M1, released at 2, duration 3, due 100.
    t=0  only J1_O1 is ready            -> J1_O1 [0,10)
    t=10 J1_O2 ready_since=10, J2_O1 ready_since=2
         FIFO -> J2_O1 (2 < 10)         -> J2_O1 [10,13), J1_O2 [13,14)
         EDD  -> J1_O2 (due 5)          -> J1_O2 [10,11), J2_O1 [11,14)
    """
    instance = inst(
        "fifo_vs_edd",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 10.0), op("J1_O2", "J1", 1, "M1", 1.0)], due=5.0),
            job("J2", [op("J2_O1", "J2", 0, "M1", 3.0)], release=2.0, due=100.0),
        ],
    )
    fifo = reference_schedule(instance, get_policy("fifo"))
    edd = reference_schedule(instance, get_policy("edd"))
    assert as_tuples(fifo) == [
        ("J1_O1", "M1", 0.0, 10.0),
        ("J2_O1", "M1", 10.0, 13.0),
        ("J1_O2", "M1", 13.0, 14.0),
    ]
    assert as_tuples(edd) == [
        ("J1_O1", "M1", 0.0, 10.0),
        ("J1_O2", "M1", 10.0, 11.0),
        ("J2_O1", "M1", 11.0, 14.0),
    ]
    assert makespan(fifo) == makespan(edd) == 14.0


def test_edd_puts_jobs_without_due_date_last() -> None:
    instance = inst(
        "no_due",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 1.0)], due=None),
            job("J2", [op("J2_O1", "J2", 0, "M1", 1.0)], due=50.0),
        ],
    )
    assert as_tuples(reference_schedule(instance, get_policy("edd"))) == [
        ("J2_O1", "M1", 0.0, 1.0),
        ("J1_O1", "M1", 1.0, 2.0),
    ]


# -- horizon and rejected capabilities ---------------------------------------
def test_horizon_truncates_without_partial_start() -> None:
    """S7: an operation whose end exceeds the horizon is never started."""
    instance = inst("horizon", [job("J1", [op("J1_O1", "J1", 0, "M1", 5.0), op("J1_O2", "J1", 1, "M1", 5.0)])])
    run = reference_run(instance, get_policy("fifo"), horizon=8.0)
    assert run.truncated is True
    assert run.complete is False
    assert run.completed_operations == 1
    assert as_tuples(run.schedule) == [("J1_O1", "M1", 0.0, 5.0)]


def test_horizon_exactly_at_end_is_allowed() -> None:
    instance = inst("horizon_ok", [job("J1", [op("J1_O1", "J1", 0, "M1", 5.0)])])
    run = reference_run(instance, get_policy("fifo"), horizon=5.0)
    assert run.complete is True
    assert run.truncated is False


def test_invalid_horizon_is_rejected() -> None:
    instance = inst("bad_horizon", [job("J1", [op("J1_O1", "J1", 0, "M1", 1.0)])])
    with pytest.raises(ValidationError):
        reference_schedule(instance, get_policy("fifo"), horizon=0.0)


def test_multi_candidate_routing_raises_engine_unsupported() -> None:
    instance = Instance(
        instance_id="routing",
        jobs=(job("J1", [Operation("J1_O1", "J1", 0, ("M1", "M2"), 1.0)]),),
        machines=(Machine("M1"), Machine("M2")),
    )
    with pytest.raises(EngineUnsupported) as excinfo:
        reference_schedule(instance, get_policy("fifo"))
    assert excinfo.value.code == "ENGINE_UNSUPPORTED"


def test_non_unit_capacity_raises_engine_unsupported() -> None:
    instance = Instance(
        instance_id="capacity",
        jobs=(job("J1", [op("J1_O1", "J1", 0, "M1", 1.0)]),),
        machines=(Machine("M1", capacity=2),),
    )
    with pytest.raises(EngineUnsupported):
        reference_schedule(instance, get_policy("fifo"))


def test_missing_policy_rejected() -> None:
    with pytest.raises(ValidationError):
        reference_schedule(comparison_instance(), None)  # type: ignore[arg-type]


# -- determinism and structure ----------------------------------------------
def test_decisions_are_one_per_time_and_machine_in_order() -> None:
    run = reference_run(comparison_instance(), get_policy("fifo"))
    keys = [(decision.time, decision.machine_id) for decision in run.decisions]
    assert len(keys) == len(set(keys))
    assert keys == sorted(keys)
    assert len(keys) == 4


def test_schedule_is_deterministic_and_typed() -> None:
    first = reference_schedule(comparison_instance(), get_policy("spt"))
    second = reference_schedule(comparison_instance(), get_policy("spt"))
    assert first == second
    assert isinstance(first, tuple)
    for item in first:
        assert isinstance(item, ScheduledOperation)
        json.dumps(item.op_id)


def test_stepper_exposes_incremental_decisions() -> None:
    stepper = ReferenceStepper(comparison_instance())
    decision = stepper.next_decision()
    assert decision is not None
    assert (decision.time, decision.machine_id) == (0.0, "M1")
    assert decision.action_ids == ("J1_O1",)
    assert stepper.next_decision() is not None  # idempotent while unapplied
    stepper.apply("J1_O1")
    with pytest.raises(ValidationError):
        stepper.apply("J1_O1")  # nothing pending any more
    following = stepper.next_decision()
    assert following is not None
    assert (following.time, following.machine_id) == (3.0, "M1")
    with pytest.raises(ValidationError):
        stepper.apply("J1_O2")  # J1_O2 is not ready for M1
    with pytest.raises(ValidationError):
        stepper.apply("J2_O1", "M2")  # machine mismatch


# -- brute-force optimality --------------------------------------------------
def tiny_job_shop() -> Instance:
    """2 jobs x 2 machines, each job visiting both machines in a fixed order."""
    return inst(
        "tiny_2x2",
        [
            job("J1", [op("J1_O1", "J1", 0, "M1", 3.0), op("J1_O2", "J1", 1, "M2", 2.0)]),
            job("J2", [op("J2_O1", "J2", 0, "M2", 2.0), op("J2_O2", "J2", 1, "M1", 4.0)]),
        ],
        machines=("M1", "M2"),
    )


def test_brute_force_matches_hand_computed_optimum() -> None:
    # M1 load 3+4=7, M2 load 2+2=4.  Order M2: J2_O1 then J1_O2 (J1_O2 starts at 3),
    # M1: J1_O1 then J2_O2 (starts at max(2,3)=3) -> makespan 7 = M1 load: optimal.
    assert brute_force_optimal_makespan(tiny_job_shop()) == 7.0
    assert lower_bound_makespan(tiny_job_shop()) == 7.0


def test_reference_schedule_cannot_beat_the_optimum() -> None:
    instance = tiny_job_shop()
    optimal = brute_force_optimal_makespan(instance)
    boundary = lower_bound_makespan(instance)
    for name in ("fifo", "spt", "edd"):
        value = makespan(reference_schedule(instance, get_policy(name)))
        assert value >= optimal - 1e-9
        assert value >= boundary - 1e-9


def test_at_least_one_policy_reaches_the_optimum_on_the_tiny_shop() -> None:
    instance = tiny_job_shop()
    optimal = brute_force_optimal_makespan(instance)
    values = {name: makespan(reference_schedule(instance, get_policy(name))) for name in ("fifo", "spt", "edd")}
    assert min(values.values()) == optimal


def test_brute_force_rejects_oversized_search() -> None:
    with pytest.raises(ValidationError):
        brute_force_optimal_makespan(tiny_job_shop(), max_combinations=1)


def test_lower_bound_is_a_valid_bound() -> None:
    instance = comparison_instance()
    bound = lower_bound_makespan(instance)
    # machine loads: M1 = 3 + 2 = 5, M2 = 2 + 4 = 6
    # job bounds:    J1 = 0 + 5 = 5, J2 = 1 + 6 = 7  -> the job bound wins
    assert bound == 7.0
    assert bound <= makespan(reference_schedule(instance, get_policy("fifo")))
