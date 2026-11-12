"""Dispatch policy contract and the canonical selection rule.

A policy maps a decision point to a preference order over the ready
operations. The engine must never contain policy logic itself; it only asks
for a choice. This keeps "simulation semantics" and "decision rule" separate,
which is exactly what the FJSP/RL comparison needs.

The canonical tie-break below is part of the contract: two runs of the same
policy on the same instance must produce identical schedules, and the
independent reference oracle must agree with the engine down to operation
start times.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from psbridge.experiment.engine import DecisionPoint, ReadyOperation


@runtime_checkable
class DispatchPolicy(Protocol):
    name: str
    description: str

    def preference(self, operation: ReadyOperation) -> tuple[Any, ...]:
        """Lower tuple sorts earlier; must be deterministic and total."""


def canonical_tiebreak(operation: ReadyOperation) -> tuple[Any, ...]:
    """Deterministic fallback ordering shared by every policy."""
    return (
        operation.ready_since,
        operation.job_release,
        operation.effective_due,
        operation.job_id,
        operation.index,
        operation.op_id,
    )


def select_operation(decision: DecisionPoint, policy: DispatchPolicy) -> ReadyOperation:
    """Return the operation the policy prefers, using the canonical tie-break."""
    if not decision.ready_operations:
        raise ValueError("decision point has no ready operations")
    return min(
        decision.ready_operations,
        key=lambda operation: (policy.preference(operation), canonical_tiebreak(operation)),
    )
