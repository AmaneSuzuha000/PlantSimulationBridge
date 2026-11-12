"""Independent reference list-scheduling oracle for the dispatch semantics.

Independence rule
-----------------
This module is implemented from the specification in
:mod:`psbridge.experiment.engine` (S1-S9) only. It deliberately shares no code
with the fake engine: the whole point of having two implementations is that a
disagreement is evidence of a bug, so the oracle must not inherit one.

A list-scheduling oracle must not be a copy of the engine
----------------------------------------------------------
There is no event heap here. The oracle keeps two plain mappings --
`machine_free[machine_id]` and `job_ready[job_id]` -- and repeatedly scans
all machines and all jobs:

1. advance the clock to the earliest time at which an idle machine has a ready
   operation (S5: a waiting machine resumes at the next machine event or the
   next ready time),
2. walk the machines in ascending `machine_id` order and dispatch at most one
   operation per machine at that time (S4).

All comparisons are exact float comparisons. Times are produced by adding
durations, so two implementations that follow the same rule produce bit-equal
values; an epsilon would only hide a disagreement.

Supported subset (v0.1)
-----------------------
Every operation must have exactly one candidate machine and every machine must
have unit capacity; anything else raises
:class:`~psbridge.errors.EngineUnsupported` (S1 / routing note). The oracle
never guesses a routing rule.

Interpretation note (must stay in sync with the engine)
-------------------------------------------------------
`ReadyOperation.ready_since` is the time at which the operation *became* ready:
`max(0, job.release_time)` for the first operation of a job and the finish time
of the previous operation otherwise. It can be smaller than the decision time.
This is the only reading that makes the FIFO preference and the canonical
tie-break meaningful, and it is the single interpretation the cross-check
depends on.

Beyond the schedule
-------------------
:func:`brute_force_optimal_makespan` and :func:`lower_bound_makespan` are
test-only helpers used to prove that the reference schedule cannot beat the
true optimum of a tiny instance. They are *not* run metrics: metrics are
computed from a finished schedule by `psbridge.experiment.metrics`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psbridge.errors import EngineUnsupported, SimulationError, ValidationError
from psbridge.experiment.engine import DecisionPoint, ReadyOperation
from psbridge.scheduling.policy import DispatchPolicy, select_operation
from psbridge.schema import Instance, ScheduledOperation, validate_number

__all__ = [
    "ReferenceDecision",
    "ReferenceRun",
    "ReferenceStepper",
    "brute_force_optimal_makespan",
    "lower_bound_makespan",
    "reference_run",
    "reference_schedule",
]


def _validate_supported(instance: Instance) -> tuple[str, ...]:
    """Validate the v0.1 subset and return machine ids in ascending order."""
    if not isinstance(instance, Instance):
        raise ValidationError(
            "instance must be a psbridge.schema.Instance",
            details={"type": type(instance).__name__},
        )
    for machine in instance.machines:
        if machine.capacity != 1:
            raise EngineUnsupported(
                "reference oracle supports unit machine capacity only (S1)",
                details={"machine_id": machine.machine_id, "capacity": machine.capacity},
            )
    for operation in instance.operations():
        if len(operation.candidates) != 1:
            raise EngineUnsupported(
                "reference oracle supports single-candidate routing only (v0.1)",
                details={"op_id": operation.op_id, "candidates": list(operation.candidates)},
            )
    return tuple(sorted(machine.machine_id for machine in instance.machines))


def _validate_policy(policy: Any) -> DispatchPolicy:
    if policy is None:
        raise ValidationError("policy must be a DispatchPolicy, got None")
    name = getattr(policy, "name", None)
    preference = getattr(policy, "preference", None)
    if not isinstance(name, str) or not name:
        raise ValidationError(
            "policy must expose a non-empty string 'name'",
            details={"type": type(policy).__name__},
        )
    if not callable(preference):
        raise ValidationError("policy must expose a callable 'preference'", details={"policy": name})
    return policy


@dataclass(frozen=True)
class ReferenceDecision:
    """One dispatch decision recorded by the reference oracle."""

    decision_index: int
    time: float
    machine_id: str
    chosen_op_id: str
    ready_op_ids: tuple[str, ...]
    completed_operations: int
    total_operations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_index": self.decision_index,
            "time": self.time,
            "machine_id": self.machine_id,
            "chosen_op_id": self.chosen_op_id,
            "ready_op_ids": list(self.ready_op_ids),
            "completed_operations": self.completed_operations,
            "total_operations": self.total_operations,
        }


@dataclass(frozen=True)
class ReferenceRun:
    """Result of :func:`reference_run` (schedule plus decision log)."""

    instance_id: str
    policy_name: str
    schedule: tuple[ScheduledOperation, ...]
    decisions: tuple[ReferenceDecision, ...]
    completed_operations: int
    total_operations: int
    truncated: bool
    final_time: float

    @property
    def complete(self) -> bool:
        return self.completed_operations == self.total_operations


class ReferenceStepper:
    """Incremental, scan-based dispatcher implementing S1-S9.

    The class is usable step by step (`next_decision` / `apply`) because
    the RL environment and the cross-check replay need the same semantics
    without going through a policy first.

    `next_decision()` is idempotent: as long as the returned decision has not
    been applied, calling it again returns an equal decision for the same
    machine and time.
    """

    def __init__(self, instance: Instance, *, horizon: float | None = None) -> None:
        self._machine_order = _validate_supported(instance)
        if horizon is not None:
            self._horizon = validate_number(horizon, field_name="horizon", strictly_positive=True)
        else:
            self._horizon = None
        self._instance = instance
        self._jobs = tuple(instance.jobs)
        self._job_order = tuple(job.job_id for job in self._jobs)
        self._op_by_id = {operation.op_id: operation for operation in instance.operations()}
        self._total = instance.total_operations
        self._cursor = {job_id: 0 for job_id in self._job_order}
        self._job_ready = {job.job_id: max(0.0, job.release_time) for job in self._jobs}
        self._machine_free = {machine_id: 0.0 for machine_id in self._machine_order}
        self._schedule: list[ScheduledOperation] = []
        self._event_log: list[dict[str, Any]] = []
        self._completed = 0
        self._time = 0.0
        self._pending: DecisionPoint | None = None
        self._time_open = False
        self._machine_pos = 0
        self._truncated = False

    # -- read-only views -------------------------------------------------
    @property
    def instance(self) -> Instance:
        return self._instance

    @property
    def completed_operations(self) -> int:
        return self._completed

    @property
    def total_operations(self) -> int:
        return self._total

    @property
    def current_time(self) -> float:
        return self._time

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def done(self) -> bool:
        return self._truncated or self._completed >= self._total

    @property
    def pending_decision(self) -> DecisionPoint | None:
        return self._pending

    def schedule(self) -> tuple[ScheduledOperation, ...]:
        return tuple(self._schedule)

    def machine_free_at(self) -> dict[str, float]:
        return {machine_id: self._machine_free[machine_id] for machine_id in self._machine_order}

    def event_log(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(entry) for entry in self._event_log)

    def fingerprint(self) -> str:
        """sha256 over the canonical schedule, the clock and the progress counter.

        This mirrors the *intent* of S9 but uses an independent serialisation:
        it is not comparable with another engine's `state_hash` and must never
        be used as a cross-implementation equality check.
        """
        payload = {
            "version": "psbridge.reference.v1",
            "instance_id": self._instance.instance_id,
            "time": self._time,
            "completed_operations": self._completed,
            "schedule": [
                {
                    "op_id": item.op_id,
                    "job_id": item.job_id,
                    "machine_id": item.machine_id,
                    "start": item.start,
                    "end": item.end,
                }
                for item in self._schedule
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- decision loop ---------------------------------------------------
    def _ready_operations(self, machine_id: str, time: float) -> tuple[ReadyOperation, ...]:
        """Ready set of one machine at one time, in instance (job) order."""
        ready: list[ReadyOperation] = []
        for job in self._jobs:
            index = self._cursor[job.job_id]
            if index >= len(job.operations):
                continue
            operation = job.operations[index]
            if operation.candidates[0] != machine_id:
                continue
            if self._job_ready[job.job_id] > time:
                continue
            ready.append(
                ReadyOperation(
                    op_id=operation.op_id,
                    job_id=job.job_id,
                    index=operation.index,
                    duration=operation.duration,
                    candidates=operation.candidates,
                    ready_since=self._job_ready[job.job_id],
                    job_release=job.release_time,
                    due_date=job.due_date,
                    weight=job.weight,
                )
            )
        return tuple(ready)

    def _next_event_time(self, time: float) -> float:
        """Earliest time after `time` at which anything can change (S5)."""
        candidates: list[float] = []
        for machine_id in self._machine_order:
            if self._machine_free[machine_id] > time:
                candidates.append(self._machine_free[machine_id])
        for job in self._jobs:
            if self._cursor[job.job_id] < len(job.operations) and self._job_ready[job.job_id] > time:
                candidates.append(self._job_ready[job.job_id])
        if not candidates:
            raise SimulationError(
                "reference oracle cannot advance: no machine event and no ready time left",
                details={
                    "instance_id": self._instance.instance_id,
                    "time": time,
                    "completed_operations": self._completed,
                    "total_operations": self._total,
                },
            )
        return min(candidates)

    def next_decision(self) -> DecisionPoint | None:
        """Return the next decision (S4/S5), or None when finished/truncated."""
        if self._pending is not None:
            return self._pending
        if self.done:
            return None
        while True:
            if not self._time_open:
                while True:
                    found = False
                    for machine_id in self._machine_order:
                        if self._machine_free[machine_id] > self._time:
                            continue
                        if self._ready_operations(machine_id, self._time):
                            found = True
                            break
                    if found:
                        break
                    self._time = self._next_event_time(self._time)
                self._machine_pos = 0
                self._time_open = True
            while self._machine_pos < len(self._machine_order):
                machine_id = self._machine_order[self._machine_pos]
                self._machine_pos += 1
                if self._machine_free[machine_id] > self._time:
                    continue
                ready = self._ready_operations(machine_id, self._time)
                if not ready:
                    continue
                decision = DecisionPoint(
                    decision_index=len(self._event_log),
                    time=self._time,
                    machine_id=machine_id,
                    ready_operations=ready,
                    completed_operations=self._completed,
                    total_operations=self._total,
                )
                self._pending = decision
                return decision
            # every machine at this time has been considered
            self._time_open = False

    def apply(self, op_id: str, machine_id: str | None = None) -> ScheduledOperation | None:
        """Dispatch `op_id` on the pending decision machine.

        Returns the created :class:`ScheduledOperation`, or None when the
        horizon rule S7 refused to start the operation (the oracle then stops
        truncated). Illegal actions raise :class:`~psbridge.errors.ValidationError`.
        """
        decision = self._pending
        if decision is None:
            raise ValidationError("no pending decision: call next_decision() first")
        if machine_id is not None and machine_id != decision.machine_id:
            raise ValidationError(
                "machine_id does not match the pending decision",
                details={"machine_id": machine_id, "decision_machine_id": decision.machine_id},
            )
        if op_id not in decision.action_ids:
            raise ValidationError(
                "operation is not in the ready set of the pending decision",
                details={"op_id": op_id, "ready_operations": list(decision.action_ids)},
            )
        operation = self._op_by_id.get(op_id)
        if operation is None:
            raise ValidationError("unknown operation", details={"op_id": op_id})
        start = decision.time
        end = start + operation.duration
        if self._horizon is not None and end > self._horizon:
            self._truncated = True
            self._pending = None
            return None
        scheduled = ScheduledOperation(
            op_id=operation.op_id,
            job_id=operation.job_id,
            machine_id=decision.machine_id,
            start=start,
            end=end,
        )
        self._schedule.append(scheduled)
        self._event_log.append(
            {
                "event": "start",
                "time": start,
                "machine_id": decision.machine_id,
                "op_id": operation.op_id,
                "job_id": operation.job_id,
                "decision_index": decision.decision_index,
            }
        )
        self._cursor[operation.job_id] += 1
        self._job_ready[operation.job_id] = end
        self._machine_free[decision.machine_id] = end
        self._completed += 1
        self._pending = None
        return scheduled


def reference_run(
    instance: Instance,
    policy: DispatchPolicy,
    *,
    horizon: float | None = None,
) -> ReferenceRun:
    """Run the oracle with `policy` and return the full :class:`ReferenceRun`."""
    policy = _validate_policy(policy)
    stepper = ReferenceStepper(instance, horizon=horizon)
    decisions: list[ReferenceDecision] = []
    while True:
        decision = stepper.next_decision()
        if decision is None:
            break
        chosen = select_operation(decision, policy)
        scheduled = stepper.apply(chosen.op_id, decision.machine_id)
        if scheduled is None:
            break
        decisions.append(
            ReferenceDecision(
                decision_index=decision.decision_index,
                time=decision.time,
                machine_id=decision.machine_id,
                chosen_op_id=chosen.op_id,
                ready_op_ids=decision.action_ids,
                completed_operations=decision.completed_operations,
                total_operations=decision.total_operations,
            )
        )
    schedule = stepper.schedule()
    final_time = max([stepper.current_time] + [item.end for item in schedule])
    return ReferenceRun(
        instance_id=instance.instance_id,
        policy_name=policy.name,
        schedule=schedule,
        decisions=tuple(decisions),
        completed_operations=stepper.completed_operations,
        total_operations=instance.total_operations,
        truncated=stepper.truncated,
        final_time=final_time,
    )


def reference_schedule(
    instance: Instance,
    policy: DispatchPolicy,
    *,
    horizon: float | None = None,
) -> tuple[ScheduledOperation, ...]:
    """Return the reference schedule for `instance` under `policy`.

    The tuple is in dispatch order, which is non-decreasing in
    `(start, machine_id)` because S4 walks machines in ascending id order at
    every event time.
    """
    return reference_run(instance, policy, horizon=horizon).schedule


# -- exact helpers for tiny instances (tests only) ----------------------------

def lower_bound_makespan(instance: Instance) -> float:
    """A cheap, provable lower bound on the optimal makespan.

    `max(max machine load, max (release + total job processing time))`.
    """
    if not isinstance(instance, Instance):
        raise ValidationError("instance must be a psbridge.schema.Instance")
    load: dict[str, float] = {machine.machine_id: 0.0 for machine in instance.machines}
    for operation in instance.operations():
        load[operation.candidates[0]] += operation.duration
    machine_bound = max(load.values(), default=0.0)
    job_bound = max((job.release_time + job.processing_time for job in instance.jobs), default=0.0)
    return max(machine_bound, job_bound)


def _earliest_makespan(
    op_ids: Sequence[str],
    durations: Mapping[str, float],
    edges: Sequence[tuple[str, str]],
    releases: Mapping[str, float],
) -> float | None:
    """Longest-path makespan for one machine ordering; None if infeasible."""
    successor: dict[str, list[str]] = {op_id: [] for op_id in op_ids}
    indegree: dict[str, int] = {op_id: 0 for op_id in op_ids}
    for predecessor, successor_id in edges:
        successor[predecessor].append(successor_id)
        indegree[successor_id] += 1
    start: dict[str, float] = {op_id: releases.get(op_id, 0.0) for op_id in op_ids}
    queue = [op_id for op_id in op_ids if indegree[op_id] == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        finish = start[current] + durations[current]
        for following in successor[current]:
            start[following] = max(start[following], finish)
            indegree[following] -= 1
            if indegree[following] == 0:
                queue.append(following)
    if visited != len(op_ids):
        return None  # cyclic precedence: that machine ordering is infeasible
    return max(start[op_id] + durations[op_id] for op_id in op_ids)


def brute_force_optimal_makespan(instance: Instance, *, max_combinations: int = 200_000) -> float:
    """Exact optimal makespan of a tiny instance by enumerating machine orders.

    Every active schedule is fixed by one total order per machine, and the
    optimal schedule is always active, so the minimum over all machine orders
    is the true optimum. The enumeration is capped by `max_combinations`;
    larger instances raise ValidationError instead of silently returning a
    bound.
    """
    machine_order = _validate_supported(instance)
    if isinstance(max_combinations, bool) or not isinstance(max_combinations, int) or max_combinations < 1:
        raise ValidationError("max_combinations must be a positive integer")
    operations = instance.operations()
    if not operations:
        return 0.0
    durations = {operation.op_id: operation.duration for operation in operations}
    by_machine: dict[str, list[str]] = {machine_id: [] for machine_id in machine_order}
    for operation in operations:
        by_machine[operation.candidates[0]].append(operation.op_id)
    combinations = 1
    for machine_id in machine_order:
        combinations *= math.factorial(len(by_machine[machine_id]))
    if combinations > max_combinations:
        raise ValidationError(
            "instance is too large for exhaustive machine-order search",
            details={"combinations": combinations, "max_combinations": max_combinations},
        )
    job_chains = [
        (job.job_id, [operation.op_id for operation in job.operations], job.release_time)
        for job in instance.jobs
    ]
    best = math.inf
    for choice in itertools.product(
        *[itertools.permutations(by_machine[machine_id]) for machine_id in machine_order]
    ):
        edges: list[tuple[str, str]] = []
        releases: dict[str, float] = {}
        for _job_id, chain, release in job_chains:
            if release > releases.get(chain[0], 0.0):
                releases[chain[0]] = release
            for predecessor, successor_id in itertools.pairwise(chain):
                edges.append((predecessor, successor_id))
        for sequence in choice:
            for predecessor, successor_id in itertools.pairwise(sequence):
                edges.append((predecessor, successor_id))
        makespan = _earliest_makespan(list(durations), durations, edges, releases)
        if makespan is not None and makespan < best:
            best = makespan
    if not math.isfinite(best):
        raise SimulationError(
            "no feasible machine ordering exists for this instance",
            details={"instance_id": instance.instance_id},
        )
    return best
