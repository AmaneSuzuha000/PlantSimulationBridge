"""Tests for the deterministic fake dispatch engine (S1-S9 semantics).

Every expected start time below is hand-computed from the S1-S9 rules.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from psbridge.errors import EngineUnsupported, ValidationError
from psbridge.experiment.engine import DecisionPoint
from psbridge.experiment.fake_engine import FakeDispatchEngine, simulate
from psbridge.schema import Instance, Job, Machine, Operation


class FIFO:
    name = "fifo"
    description = "first ready, first served"

    def preference(self, operation):
        return (operation.ready_since,)


class SPT:
    name = "spt"
    description = "shortest processing time"

    def preference(self, operation):
        return (operation.duration,)


class LPT:
    name = "lpt"
    description = "longest processing time"

    def preference(self, operation):
        return (-operation.duration,)


class LowestDue:
    name = "lowest_due"
    description = "earliest effective due date"

    def preference(self, operation):
        return (operation.effective_due,)


def two_by_two() -> Instance:
    """2 jobs x 2 ops on 2 dedicated machines (no routing choice)."""
    return Instance(
        instance_id="two_by_two",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                job_id="J1",
                release_time=0.0,
                due_date=6.0,
                operations=(
                    Operation("J1_O1", "J1", 0, ("M1",), 3.0),
                    Operation("J1_O2", "J1", 1, ("M2",), 2.0),
                ),
            ),
            Job(
                job_id="J2",
                release_time=0.0,
                due_date=10.0,
                operations=(
                    Operation("J2_O1", "J2", 0, ("M1",), 2.0),
                    Operation("J2_O2", "J2", 1, ("M2",), 4.0),
                ),
            ),
        ),
    )


def schedule_of(instance: Instance, policy, seed: int = 0, horizon: float | None = None):
    engine = FakeDispatchEngine()
    schedule = simulate(engine, instance, policy, seed=seed, horizon=horizon)
    return engine, schedule


def as_tuples(schedule):
    return [(item.op_id, item.machine_id, item.start, item.end) for item in schedule]


# -- hand-computed schedules --------------------------------------------
def test_fifo_schedule_exact_times() -> None:
    # t=0: M1 free, ready {J1_O1(3), J2_O1(2)} -> FIFO picks J1_O1 (both
    # ready since 0; canonical tie-break orders by job_id then index) -> M1 busy
    # until 3. M2 is free at t=0 but nothing is ready for it (both jobs are on
    # their first operation, which runs on M1).
    # t=2 is not an event time here; the next event is M1 at 3.
    # t=3: M1 free again -> ready {J2_O1} -> M1 3..5. M2 free -> ready {J1_O2}
    # -> M2 3..5.
    # t=5: M2 free -> ready {J2_O2} -> M2 5..9. M1 free at 5, nothing ready.
    _, schedule = schedule_of(two_by_two(), FIFO())
    assert as_tuples(schedule) == [
        ("J1_O1", "M1", 0.0, 3.0),
        ("J2_O1", "M1", 3.0, 5.0),
        ("J1_O2", "M2", 3.0, 5.0),
        ("J2_O2", "M2", 5.0, 9.0),
    ]


def test_spt_and_lpt_differ_on_machine_one() -> None:
    # At t=0 both jobs are ready on M1. SPT picks J2_O1 (2 < 3) then J1_O1;
    # LPT picks J1_O1 first. The M1 order changes but the makespan happens to
    # stay 9 here - we assert the concrete difference in start times.
    instance = two_by_two()
    _, spt_schedule = schedule_of(instance, SPT())
    assert as_tuples(spt_schedule)[:2] == [
        ("J2_O1", "M1", 0.0, 2.0),
        ("J1_O1", "M1", 2.0, 5.0),
    ]
    _, lpt_schedule = schedule_of(instance, LPT())
    assert as_tuples(lpt_schedule)[:2] == [
        ("J1_O1", "M1", 0.0, 3.0),
        ("J2_O1", "M1", 3.0, 5.0),
    ]
    assert spt_schedule != lpt_schedule


def test_release_time_delays_first_operation() -> None:
    # J2 releases at 4; at t=0 only J1_O1 is ready on M1 (0..3) and M2 has
    # nothing. t=3: M1 free, no operation ready (J2 releases at 4) -> wait.
    # t=4: M1 starts J2_O1 (4..6). M2: J1_O2 ready since 3 -> 4..6 (M2 free at
    # 0 but nothing was ready at 0; the earliest ready time is 3, so at t=3
    # M2 gets J1_O2 3..5 - see the assertion below).
    instance = Instance(
        instance_id="release_1",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 3.0), Operation("J1_O2", "J1", 1, ("M2",), 2.0))),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 2.0), Operation("J2_O2", "J2", 1, ("M2",), 1.0)),
                release_time=4.0,
            ),
        ),
    )
    _, schedule = schedule_of(instance, FIFO())
    assert as_tuples(schedule) == [
        ("J1_O1", "M1", 0.0, 3.0),
        ("J1_O2", "M2", 3.0, 5.0),
        ("J2_O1", "M1", 4.0, 6.0),
        ("J2_O2", "M2", 6.0, 7.0),
    ]


def test_edd_versus_spt_change_the_m1_order() -> None:
    instance = Instance(
        instance_id="edd_vs_spt",
        machines=(Machine("M1"),),
        jobs=(
            Job("J_urgent", (Operation("J_urgent_O1", "J_urgent", 0, ("M1",), 5.0),), due_date=5.0),
            Job("J_loose", (Operation("J_loose_O1", "J_loose", 0, ("M1",), 1.0),), due_date=50.0),
        ),
    )
    _, spt_schedule = schedule_of(instance, SPT())
    assert as_tuples(spt_schedule) == [
        ("J_loose_O1", "M1", 0.0, 1.0),
        ("J_urgent_O1", "M1", 1.0, 6.0),
    ]
    _, edd_schedule = schedule_of(instance, LowestDue())
    assert as_tuples(edd_schedule) == [
        ("J_urgent_O1", "M1", 0.0, 5.0),
        ("J_loose_O1", "M1", 5.0, 6.0),
    ]


def test_precedence_holds_with_one_machine() -> None:
    instance = Instance(
        instance_id="chain",
        machines=(Machine("M1"),),
        jobs=(
            Job(
                "J1",
                (
                    Operation("J1_O1", "J1", 0, ("M1",), 2.0),
                    Operation("J1_O2", "J1", 1, ("M1",), 3.0),
                    Operation("J1_O3", "J1", 2, ("M1",), 1.0),
                ),
            ),
        ),
    )
    _, schedule = schedule_of(instance, FIFO())
    assert as_tuples(schedule) == [
        ("J1_O1", "M1", 0.0, 2.0),
        ("J1_O2", "M1", 2.0, 5.0),
        ("J1_O3", "M1", 5.0, 6.0),
    ]


# -- decision points -----------------------------------------------------
def test_decisions_are_one_per_time_and_machine() -> None:
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    decision = engine.next_decision()
    assert isinstance(decision, DecisionPoint)
    assert decision.time == 0.0
    assert decision.machine_id == "M1"
    assert decision.action_ids == ("J1_O1", "J2_O1")
    assert decision.completed_operations == 0
    assert decision.total_operations == 4
    ready = decision.ready_operations
    assert [op.ready_since for op in ready] == [0.0, 0.0]
    assert [op.job_release for op in ready] == [0.0, 0.0]
    # Ready set is in canonical order (job_id, index, op_id).
    assert decision.ready_operations[0].job_id < decision.ready_operations[1].job_id


def test_next_decision_is_idempotent_until_apply() -> None:
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    first = engine.next_decision()
    second = engine.next_decision()
    assert first is second
    engine.apply("J1_O1")
    assert engine.current_state().completed_operations == 1


def test_no_decision_offered_for_a_machine_with_empty_ready_set() -> None:
    # At t=0 nothing is ready on M2 (both first operations are M1-only), so the
    # engine must not emit a decision point for M2 at t=0.
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    decisions = []
    while not engine.is_done():
        decision = engine.next_decision()
        if decision is None:
            break
        decisions.append((decision.time, decision.machine_id))
        engine.apply(decision.action_ids[0])
    assert decisions == [(0.0, "M1"), (3.0, "M1"), (3.0, "M2"), (5.0, "M2")]


def test_machines_visited_in_ascending_id_order_at_the_same_time() -> None:
    # Both operations can run on either... no: routing is single-candidate, so
    # build two single-candidate jobs on M_b and M_a that both become ready at
    # t=0 to prove the ascending machine_id visit order of S4.
    instance = Instance(
        instance_id="order",
        machines=(Machine("M_b"), Machine("M_a")),
        jobs=(
            Job("J2", (Operation("J2_O1", "J2", 0, ("M_b",), 1.0),)),
            Job("J1", (Operation("J1_O1", "J1", 0, ("M_a",), 1.0),)),
        ),
    )
    engine = FakeDispatchEngine()
    engine.reset(instance, 0)
    seen = []
    while not engine.is_done():
        decision = engine.next_decision()
        if decision is None:
            break
        seen.append(decision.machine_id)
        engine.apply(decision.action_ids[0])
    assert seen[:2] == ["M_a", "M_b"]


def test_apply_rejects_operation_outside_the_ready_set() -> None:
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    engine.next_decision()
    with pytest.raises(ValidationError):
        engine.apply("J1_O2")  # not ready yet (precedence)
    with pytest.raises(ValidationError):
        engine.apply("J1_O1", "M2")  # decision is bound to M1
    assert engine.current_state().completed_operations == 0


def test_apply_without_pending_decision_is_rejected() -> None:
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    with pytest.raises(ValidationError):
        engine.apply("J1_O1")


# -- routing / capacity / horizon (S1, S7) -------------------------------
def test_multi_candidate_routing_is_unsupported() -> None:
    instance = Instance(
        instance_id="flex",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1", "M2"), 1.0),)),),
    )
    with pytest.raises(EngineUnsupported) as excinfo:
        FakeDispatchEngine().reset(instance, 0)
    assert "routing" in str(excinfo.value).lower() or "candidate" in str(excinfo.value).lower()


def test_non_unit_capacity_is_unsupported() -> None:
    instance = Instance(
        instance_id="cap2",
        machines=(Machine("M1", capacity=2),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 1.0),)),),
    )
    with pytest.raises(EngineUnsupported):
        FakeDispatchEngine().reset(instance, 0)


def test_horizon_truncates_instead_of_overshooting() -> None:
    # Makespan is 9 without a horizon. With horizon 5 the op that would end at
    # 9 is never started, so the engine finishes with 3/4 operations done and
    # no scheduled end time exceeds the horizon.
    engine = FakeDispatchEngine()
    schedule = simulate(engine, two_by_two(), FIFO(), horizon=5.0)
    assert engine.truncated
    assert engine.completed_operations == 3
    assert engine.total_operations == 4
    assert all(item.end <= 5.0 for item in schedule)
    assert "J2_O2" not in {item.op_id for item in schedule}


def test_horizon_that_fits_everything_is_not_truncated() -> None:
    engine = FakeDispatchEngine()
    schedule = simulate(engine, two_by_two(), FIFO(), horizon=9.0)
    assert not engine.truncated
    assert len(schedule) == 4
    assert max(item.end for item in schedule) == 9.0


def test_apply_beyond_horizon_is_refused() -> None:
    # J2_O1 (duration 2.0) ends exactly at t=2.0, which is inside a horizon of
    # 2.0; it is therefore still offered. A horizon of 1.5 filters every first
    # operation and the run is truncated with nothing scheduled.
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0, horizon=2.0)
    decision = engine.next_decision()
    assert decision is not None
    assert decision.action_ids == ("J2_O1",)
    engine.apply("J2_O1")
    assert engine.next_decision() is None
    assert engine.truncated
    assert [item.op_id for item in engine.schedule()] == ["J2_O1"]

    tight = FakeDispatchEngine()
    tight.reset(two_by_two(), 0, horizon=1.5)
    assert tight.next_decision() is None
    assert tight.truncated
    assert tight.schedule() == ()


# -- determinism, hashing, event log (S8, S9) ----------------------------
def test_seed_is_provenance_only() -> None:
    instance = two_by_two()
    _, first = schedule_of(instance, FIFO(), seed=0)
    _, second = schedule_of(instance, FIFO(), seed=123456)
    assert first == second


def test_state_hash_changes_with_progress() -> None:
    engine = FakeDispatchEngine()
    engine.reset(two_by_two(), 0)
    empty = engine.state_hash()
    assert len(empty) == 64
    engine.next_decision()
    engine.apply("J1_O1")
    assert engine.state_hash() != empty
    payload = {
        "time": engine.current_state().time,
        "completed_operations": engine.current_state().completed_operations,
        "schedule": [
            [item.op_id, item.job_id, item.machine_id, item.start, item.end]
            for item in engine.schedule()
        ],
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert engine.state_hash() == expected


def test_identical_runs_produce_identical_state_hash() -> None:
    first = FakeDispatchEngine()
    simulate(first, two_by_two(), SPT())
    second = FakeDispatchEngine()
    simulate(second, two_by_two(), SPT())
    assert first.state_hash() == second.state_hash()
    assert first.schedule() == second.schedule()


def test_event_log_is_ordered_and_complete() -> None:
    engine = FakeDispatchEngine()
    simulate(engine, two_by_two(), FIFO())
    log = engine.event_log()
    assert log[0]["event"] == "reset"
    assert log[-1]["event"] == "complete"
    starts = [entry for entry in log if entry["event"] == "start"]
    assert len(starts) == 4
    keys = [(entry["time"], entry["machine_id"]) for entry in starts]
    assert keys == sorted(keys)
    # Exactly one decision record per applied operation here (each decision
    # was consumed by exactly one start).
    assert len([entry for entry in log if entry["event"] == "decision"]) == 4
    # Every logged start corresponds to a scheduled operation.
    assert [entry["op_id"] for entry in starts] == [item.op_id for item in engine.schedule()]


def test_event_log_records_truncation() -> None:
    engine = FakeDispatchEngine()
    simulate(engine, two_by_two(), FIFO(), horizon=5.0)
    assert engine.event_log()[-1]["event"] == "truncated"


def test_engine_kind_is_stable() -> None:
    assert FakeDispatchEngine().kind == "fake_dispatch"


def test_reset_rejects_a_non_instance_and_bad_seed() -> None:
    engine = FakeDispatchEngine()
    with pytest.raises(ValidationError):
        engine.reset("nope", 0)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        engine.reset(two_by_two(), -1)
    with pytest.raises(ValidationError):
        engine.reset(two_by_two(), 0, horizon=0.0)


def test_reset_clears_previous_run() -> None:
    engine = FakeDispatchEngine()
    simulate(engine, two_by_two(), FIFO())
    assert engine.completed_operations == 4
    engine.reset(two_by_two(), 0)
    assert engine.completed_operations == 0
    assert engine.schedule() == ()
    assert not engine.is_done()


def test_finished_engine_returns_no_more_decisions() -> None:
    engine = FakeDispatchEngine()
    simulate(engine, two_by_two(), FIFO())
    assert engine.is_done()
    assert engine.next_decision() is None


def test_ready_operation_fields_are_filled() -> None:
    instance = Instance(
        instance_id="fields",
        machines=(Machine("M1"),),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 2.0), Operation("J1_O2", "J1", 1, ("M1",), 3.0)),
                release_time=1.0,
                due_date=20.0,
                weight=2.5,
            ),
        ),
    )
    engine = FakeDispatchEngine()
    engine.reset(instance, 0)
    decision = engine.next_decision()
    assert decision.time == 1.0
    ready = decision.ready_operations[0]
    assert ready.op_id == "J1_O1"
    assert ready.index == 0
    assert ready.duration == 2.0
    assert ready.candidates == ("M1",)
    assert ready.ready_since == 1.0
    assert ready.job_release == 1.0
    assert ready.due_date == 20.0
    assert ready.effective_due == 20.0
    assert ready.weight == 2.5


def test_unused_machine_keeps_free_time_zero() -> None:
    instance = Instance(
        instance_id="idle_machine",
        machines=(Machine("M1"), Machine("M9")),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    engine = FakeDispatchEngine()
    simulate(engine, instance, FIFO())
    assert engine.current_state().machine_free_at == {"M1": 2.0, "M9": 0.0}
