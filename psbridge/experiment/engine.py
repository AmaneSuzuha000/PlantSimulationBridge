"""DispatchEngine contract and the deterministic v0.1 simulation semantics.

Two independent implementations exist on purpose:

* FakeDispatchEngine (psbridge.experiment.fake_engine) - the reference engine
  used by the runner and by the reproducibility tests,
* reference list-scheduling oracle (psbridge.scheduling.reference) - written
  independently from the same specification, used to cross-check results.

If the two disagree on any instance, the run is invalid (CROSS_CHECK_MISMATCH).

Deterministic semantics (authoritative; both implementations must follow it)
---------------------------------------------------------------------------
S1. Time starts at 0. Every machine is available from t=0. Machine capacity
    is 1 in v0.1; a non-unit capacity is rejected with EngineUnsupported.
S2. A job releases at release_time. Operation k of a job becomes ready when
    operation k-1 finishes; for k=0 it becomes ready at max(0, release_time).
S3. Operations are non-preemptive and occupy their machine for their whole
    duration. Buffers are unlimited.
S4. The event queue contains machine availability times. At any event time t,
    machines that are free are processed in ascending machine_id order.
S5. For a free machine m at time t: if at least one operation is ready at t,
    the engine emits one decision point (t, m, ready set). The caller chooses
    one op; the engine schedules it on m at t. If no operation is ready, the
    machine waits until the earliest (next machine event, next ready time).
S6. Selection ordering is: policy preference key first, then the canonical
    tie-break in psbridge.scheduling.policy.select_operation. This makes the
    whole simulation a pure function of (instance, policy).
S7. If horizon is set, an operation whose end would exceed the horizon is not
    started; the engine stops with completed_operations < total_operations and
    the runner must mark the run invalid (truncated).
S8. seed is provenance only in v0.1: the v0.1 engine is deterministic and must
    produce identical schedules for identical instances regardless of seed.
    Stochastic processing times are produced upstream by the experiment layer
    (duration_jitter, seeded per run), so multi-seed studies remain meaningful.
S9. Every engine records an event log with start events in non-decreasing
    (time, machine_id) order; state_hash is a sha256 over the canonical
    schedule plus current time and completed count.

Routing: v0.1 engines accept operations with exactly one candidate machine.
Multi-candidate routing raises EngineUnsupported; extending to routing choice
is a v0.2 task and requires an explicit machine-selection rule in S5.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from psbridge.schema import Instance, ScheduledOperation


@dataclass(frozen=True)
class ReadyOperation:
    """An operation that may start right now."""

    op_id: str
    job_id: str
    index: int
    duration: float
    candidates: tuple[str, ...]
    ready_since: float
    job_release: float
    due_date: float | None
    weight: float

    @property
    def effective_due(self) -> float:
        return float("inf") if self.due_date is None else self.due_date


@dataclass(frozen=True)
class DecisionPoint:
    """A single dispatch decision: one free machine, one ready set."""

    decision_index: int
    time: float
    machine_id: str
    ready_operations: tuple[ReadyOperation, ...]
    completed_operations: int
    total_operations: int

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(operation.op_id for operation in self.ready_operations)


@dataclass(frozen=True)
class EngineState:
    """Immutable snapshot returned after reset/apply."""

    time: float
    completed_operations: int
    total_operations: int
    schedule: tuple[ScheduledOperation, ...]
    machine_free_at: Mapping[str, float] = field(default_factory=dict)


@runtime_checkable
class DispatchEngine(Protocol):
    """Deterministic dispatch simulation engine (see module docstring)."""

    @property
    def kind(self) -> str:
        ...

    def reset(self, instance: Instance, seed: int, *, horizon: float | None = None) -> EngineState:
        ...

    def next_decision(self) -> DecisionPoint | None:
        """Advance internal time to the next decision; None if finished/truncated."""

    def apply(self, op_id: str, machine_id: str | None = None) -> EngineState:
        ...

    def is_done(self) -> bool:
        ...

    def current_state(self) -> EngineState:
        ...

    def schedule(self) -> tuple[ScheduledOperation, ...]:
        ...

    def state_hash(self) -> str:
        ...

    def event_log(self) -> tuple[Mapping[str, Any], ...]:
        ...
