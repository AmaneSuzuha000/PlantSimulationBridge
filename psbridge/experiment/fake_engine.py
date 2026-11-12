"""Deterministic fake dispatch engine (reference implementation of S1-S9).

This engine implements the v0.1 semantics in psbridge.experiment.engine
literally. Where the specification leaves a degree of freedom, the choice is
pinned down below (also recorded in the module tests).

Interpretations pinned down
--------------------------
I1. "Ready" is machine-relative (S5). An operation is ready for machine m at
    time t when (a) it is the next unscheduled operation of its job, (b) its
    job predecessor finished at or before t (the first operation is ready at
    the job release time), (c) m is one of its candidate machines, and (d) it
    can finish by the horizon (I4). Without (c) a decision point could only
    ever produce a schedule that violates machine eligibility. In v0.1
    routing is single-candidate, so ready sets of different machines are
    disjoint and the S4 machine ordering is deterministic but unobservable
    in the produced schedule; it is still observable in the decision log.
I2. One event time is the minimum of (machine availability times, job ready
    times) strictly greater than the current time. Machines with an
    availability time <= t are the free machines at t and are visited in
    ascending machine_id order (S4). An idle machine with no ready operation
    simply waits for the next event time (S5).
I3. next_decision() is idempotent: calling it twice without apply() returns
    the same pending decision point and never advances time.
I4. Horizon truncation (S7) is applied per decision, not per run: an
    operation is offered at t only if t + duration <= horizon. The run ends
    when no operation can still be started; completed < total then signals
    truncation. This is the only reading under which the emitted decision
    points never contain an action that would immediately violate the
    horizon gate, which the RL environment (psbridge.scheduling.env)
    requires for a well-defined action mask.
I5. Event log entries are: reset, decision (one per (time, machine) pair that
    had a non-empty ready set), start (one per applied operation) and a final
    complete/truncated entry. The terminal entry is appended by the last
    successful apply() (complete) or by the first next_decision() call that
    cannot advance time (truncated), so a finished run always ends with it even
    if the caller never asks for another decision. Start entries are therefore
    in non-decreasing (time, machine_id) order, as S9 requires.
I6. state_hash is sha256 over the canonical JSON of {time,
    completed_operations, schedule}, exactly the three items S9 names.

The engine is a pure function of (instance, policy choices): it never reads
the global random module and seed is provenance only (S8).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from psbridge.errors import EngineUnsupported, SimulationError, ValidationError
from psbridge.experiment.engine import DecisionPoint, EngineState, ReadyOperation
from psbridge.scheduling.policy import canonical_tiebreak, select_operation
from psbridge.schema import Instance, ScheduledOperation, validate_number

ENGINE_KIND = "fake_dispatch"


class FakeDispatchEngine:
    """Event-time list-scheduling engine implementing DispatchEngine exactly."""

    def __init__(self) -> None:
        self._reset_state()

    # -- DispatchEngine protocol ----------------------------------------
    @property
    def kind(self) -> str:
        return ENGINE_KIND

    def reset(self, instance: Instance, seed: int, *, horizon: float | None = None) -> EngineState:
        if not isinstance(instance, Instance):
            raise ValidationError(
                "reset() expects an Instance", details={"type": type(instance).__name__}
            )
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValidationError("seed must be a non-negative integer", details={"seed": repr(seed)})
        horizon_value: float | None = None
        if horizon is not None:
            horizon_value = validate_number(horizon, field_name="horizon", strictly_positive=True)

        non_unit = [machine.machine_id for machine in instance.machines if machine.capacity != 1]
        if non_unit:
            raise EngineUnsupported(
                "v0.1 engine requires unit machine capacity (S1)",
                details={"machines": non_unit},
            )
        flexible = [operation.op_id for operation in instance.operations() if len(operation.candidates) != 1]
        if flexible:
            raise EngineUnsupported(
                "v0.1 engine does not choose among candidate machines (routing); "
                "every operation must have exactly one candidate",
                details={"operations": flexible[:20], "count": len(flexible)},
            )

        self._instance = instance
        self._seed = seed
        self._horizon = horizon_value
        self._machine_ids = instance.machine_ids()
        self._sorted_machine_ids = tuple(sorted(self._machine_ids))
        self._machine_free: dict[str, float] = {machine_id: 0.0 for machine_id in self._machine_ids}
        self._job_operations = {job.job_id: job.operations for job in instance.jobs}
        self._job_release = {job.job_id: float(job.release_time) for job in instance.jobs}
        self._job_due = {job.job_id: job.due_date for job in instance.jobs}
        self._job_weight = {job.job_id: float(job.weight) for job in instance.jobs}
        self._job_next = {job.job_id: 0 for job in instance.jobs}
        # S2: the first operation of a job is ready at its release time.
        self._job_ready = {job.job_id: float(job.release_time) for job in instance.jobs}
        self._incomplete_jobs: dict[str, None] = {}
        self._waiting_on_machine: dict[str, dict[str, None]] = {
            machine_id: {} for machine_id in self._machine_ids
        }
        for job in instance.jobs:
            if job.operations:
                self._incomplete_jobs[job.job_id] = None
                self._waiting_on_machine[job.operations[0].candidates[0]][job.job_id] = None
        self._total = instance.total_operations
        self._schedule: list[ScheduledOperation] = []
        self._completed = 0
        self._time = 0.0
        self._free_order: tuple[str, ...] = tuple(self._sorted_machine_ids)
        self._cursor = 0
        self._pending: DecisionPoint | None = None
        self._decision_index = 0
        self._done = False
        self._log: list[dict[str, Any]] = []
        self._log.append(
            {
                "event": "reset",
                "time": 0.0,
                "seed": seed,
                "instance_id": instance.instance_id,
                "total_operations": self._total,
                "horizon": horizon_value,
            }
        )
        return self.current_state()

    def next_decision(self) -> DecisionPoint | None:
        if self._pending is not None:
            return self._pending
        if self.is_done():
            return None
        while True:
            while self._cursor < len(self._free_order):
                machine_id = self._free_order[self._cursor]
                self._cursor += 1
                ready = self._ready_set(machine_id, self._time)
                if not ready:
                    continue
                decision = DecisionPoint(
                    decision_index=self._decision_index,
                    time=self._time,
                    machine_id=machine_id,
                    ready_operations=ready,
                    completed_operations=self._completed,
                    total_operations=self._total,
                )
                self._decision_index += 1
                self._pending = decision
                self._log.append(
                    {
                        "event": "decision",
                        "time": self._time,
                        "machine_id": machine_id,
                        "decision_index": decision.decision_index,
                        "ready": [operation.op_id for operation in ready],
                        "completed_operations": self._completed,
                    }
                )
                return decision
            if not self._advance_time():
                self._done = True
                self._log.append(
                    {
                        "event": "complete" if self._completed >= self._total else "truncated",
                        "time": self._time,
                        "completed_operations": self._completed,
                        "total_operations": self._total,
                    }
                )
                return None

    def apply(self, op_id: str, machine_id: str | None = None) -> EngineState:
        decision = self._pending
        if decision is None:
            raise ValidationError(
                "apply() requires a pending decision point; call next_decision() first",
                details={"op_id": op_id},
            )
        if not isinstance(op_id, str):
            raise ValidationError("op_id must be a string", details={"op_id": repr(op_id)})
        target_machine = decision.machine_id if machine_id is None else machine_id
        if target_machine != decision.machine_id:
            raise ValidationError(
                "the decision point is bound to a single machine",
                details={"machine_id": target_machine, "expected": decision.machine_id},
            )
        chosen: ReadyOperation | None = None
        for operation in decision.ready_operations:
            if operation.op_id == op_id:
                chosen = operation
                break
        if chosen is None:
            raise ValidationError(
                "operation is not offered at this decision point",
                details={"op_id": op_id, "machine_id": target_machine, "allowed": list(decision.action_ids)},
            )
        if chosen.job_id not in self._job_next:
            raise SimulationError("ready operation references an unknown job", details={"job_id": chosen.job_id})
        if self._job_next[chosen.job_id] != chosen.index:
            raise SimulationError(
                "ready operation index does not match the job cursor",
                details={"op_id": chosen.op_id, "index": chosen.index, "cursor": self._job_next[chosen.job_id]},
            )
        start = self._time
        end = start + chosen.duration
        if self._horizon is not None and end > self._horizon:
            raise SimulationError(
                "refusing to start an operation beyond the horizon (S7)",
                details={"op_id": chosen.op_id, "start": start, "end": end, "horizon": self._horizon},
            )
        scheduled = ScheduledOperation(
            op_id=chosen.op_id,
            job_id=chosen.job_id,
            machine_id=target_machine,
            start=start,
            end=end,
        )
        self._schedule.append(scheduled)
        self._machine_free[target_machine] = end
        next_index = chosen.index + 1
        self._job_next[chosen.job_id] = next_index
        self._job_ready[chosen.job_id] = end
        self._waiting_on_machine[target_machine].pop(chosen.job_id, None)
        job_ops = self._job_operations[chosen.job_id]
        if next_index < len(job_ops):
            next_machine = job_ops[next_index].candidates[0]
            self._waiting_on_machine[next_machine][chosen.job_id] = None
        else:
            self._incomplete_jobs.pop(chosen.job_id, None)
        self._completed += 1
        self._pending = None
        self._log.append(
            {
                "event": "start",
                "time": start,
                "end": end,
                "machine_id": target_machine,
                "op_id": chosen.op_id,
                "job_id": chosen.job_id,
                "decision_index": decision.decision_index,
                "completed_operations": self._completed,
            }
        )
        if self._completed >= self._total:
            # Last operation applied: close the event log immediately so a
            # completed run always ends with a "complete" entry, even when the
            # caller never asks for another decision point.
            self._done = True
            self._log.append(
                {
                    "event": "complete",
                    "time": start,
                    "completed_operations": self._completed,
                    "total_operations": self._total,
                }
            )
        return self.current_state()

    def is_done(self) -> bool:
        return self._done or self._completed >= self._total

    def current_state(self) -> EngineState:
        return EngineState(
            time=self._time,
            completed_operations=self._completed,
            total_operations=self._total,
            schedule=tuple(self._schedule),
            machine_free_at=dict(self._machine_free),
        )

    def schedule(self) -> tuple[ScheduledOperation, ...]:
        return tuple(self._schedule)

    def state_hash(self) -> str:
        payload = {
            "time": self._time,
            "completed_operations": self._completed,
            "schedule": [
                [item.op_id, item.job_id, item.machine_id, item.start, item.end]
                for item in self._schedule
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def event_log(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(entry) for entry in self._log)

    # -- extras used by tests / the runner ------------------------------
    @property
    def seed(self) -> int:
        return self._seed

    @property
    def truncated(self) -> bool:
        return self._done and self._completed < self._total

    @property
    def decision_count(self) -> int:
        return self._decision_index

    @property
    def completed_operations(self) -> int:
        return self._completed

    @property
    def total_operations(self) -> int:
        return self._total

    # -- internals ------------------------------------------------------
    def _reset_state(self) -> None:
        self._instance: Instance | None = None
        self._seed = 0
        self._horizon: float | None = None
        self._machine_ids: tuple[str, ...] = ()
        self._sorted_machine_ids: tuple[str, ...] = ()
        self._machine_free: dict[str, float] = {}
        self._job_operations: dict[str, tuple[Any, ...]] = {}
        self._job_release: dict[str, float] = {}
        self._job_due: dict[str, float | None] = {}
        self._job_weight: dict[str, float] = {}
        self._job_next: dict[str, int] = {}
        self._job_ready: dict[str, float] = {}
        self._incomplete_jobs: dict[str, None] = {}
        self._waiting_on_machine: dict[str, dict[str, None]] = {}
        self._total = 0
        self._schedule = []
        self._completed = 0
        self._time = 0.0
        self._free_order = ()
        self._cursor = 0
        self._pending = None
        self._decision_index = 0
        self._done = True
        self._log = []

    def _ready_set(self, machine_id: str, time: float) -> tuple[ReadyOperation, ...]:
        waiting = self._waiting_on_machine.get(machine_id)
        if not waiting:
            return ()
        ready: list[ReadyOperation] = []
        for job_id in waiting:
            ready_since = self._job_ready[job_id]
            if ready_since > time:
                continue
            index = self._job_next[job_id]
            operation = self._job_operations[job_id][index]
            if self._horizon is not None and time + operation.duration > self._horizon:
                continue
            ready.append(
                ReadyOperation(
                    op_id=operation.op_id,
                    job_id=job_id,
                    index=index,
                    duration=operation.duration,
                    candidates=operation.candidates,
                    ready_since=ready_since,
                    job_release=self._job_release[job_id],
                    due_date=self._job_due[job_id],
                    weight=self._job_weight[job_id],
                )
            )
        ready.sort(key=canonical_tiebreak)
        return tuple(ready)

    def _advance_time(self) -> bool:
        candidates: list[float] = []
        for availability in self._machine_free.values():
            if availability > self._time:
                candidates.append(availability)
        for job_id in self._incomplete_jobs:
            ready_since = self._job_ready[job_id]
            if ready_since > self._time:
                candidates.append(ready_since)
        if not candidates:
            return False
        next_time = min(candidates)
        self._time = next_time
        self._free_order = tuple(
            machine_id
            for machine_id in self._sorted_machine_ids
            if self._machine_free[machine_id] <= next_time
        )
        self._cursor = 0
        return True


def simulate(
    engine: FakeDispatchEngine,
    instance: Instance,
    policy: Any,
    seed: int = 0,
    *,
    horizon: float | None = None,
    max_steps: int | None = None,
) -> tuple[ScheduledOperation, ...]:
    """Run an engine to completion under a policy and return its schedule.

    Convenience helper for tests and demos; the experiment runner has its own
    loop so that it stays engine-agnostic.
    """
    engine.reset(instance, seed, horizon=horizon)
    budget = max_steps if max_steps is not None else instance.total_operations + 8
    steps = 0
    while not engine.is_done():
        decision = engine.next_decision()
        if decision is None:
            break
        steps += 1
        if steps > budget:
            raise SimulationError(
                "engine exceeded the step budget (possible non-termination)",
                details={"steps": steps, "budget": budget},
            )
        choice = select_operation(decision, policy)
        engine.apply(choice.op_id, decision.machine_id)
    return engine.schedule()
