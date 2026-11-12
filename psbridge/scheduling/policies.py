"""Concrete dispatch policies: FIFO, SPT and EDD.

A policy is *only* a preference key. The engine adds the canonical tie-break
from :func:`psbridge.scheduling.policy.canonical_tiebreak`, so a policy never
has to worry about determinism -- it must merely be deterministic itself.

Registered names are stable: they are used by experiment specs, by run records
and by the generated SimTalk dispatch method, so they must never be renamed.

======  ===========  =====================================
name    class        preference key
======  ===========  =====================================
fifo    FIFOPolicy   `(ready_since,)`
spt     SPTPolicy    `(duration,)`
edd     EDDPolicy    `(effective_due,)`
======  ===========  =====================================

`effective_due` is `+inf` for a job without a due date, i.e. such a job is
always dispatched last among the ready operations of a decision point.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psbridge.errors import ValidationError
from psbridge.experiment.engine import ReadyOperation
from psbridge.scheduling.policy import DispatchPolicy

__all__ = [
    "POLICIES",
    "EDDPolicy",
    "FIFOPolicy",
    "SPTPolicy",
    "available_policies",
    "describe_policies",
    "get_policy",
]


class FIFOPolicy:
    """First in, first out: serve the operation that has been ready longest."""

    name = "fifo"
    description = "First in, first out: the operation that has been ready the longest wins."

    def preference(self, operation: ReadyOperation) -> tuple[Any, ...]:
        return (operation.ready_since,)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "FIFOPolicy()"


class SPTPolicy:
    """Shortest processing time first: the shortest ready operation wins."""

    name = "spt"
    description = "Shortest processing time: the ready operation with the smallest duration wins."

    def preference(self, operation: ReadyOperation) -> tuple[Any, ...]:
        return (operation.duration,)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SPTPolicy()"


class EDDPolicy:
    """Earliest due date first: the ready operation of the most urgent job wins."""

    name = "edd"
    description = "Earliest due date: the ready operation of the job with the earliest due date wins."

    def preference(self, operation: ReadyOperation) -> tuple[Any, ...]:
        return (operation.effective_due,)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "EDDPolicy()"


#: Registry name -> policy class. Values are classes so every caller gets a
#: fresh policy object and nothing mutable is shared between runs.
POLICIES: Mapping[str, type] = {
    FIFOPolicy.name: FIFOPolicy,
    SPTPolicy.name: SPTPolicy,
    EDDPolicy.name: EDDPolicy,
}


def available_policies() -> tuple[str, ...]:
    """Return every registered policy name in sorted order."""
    return tuple(sorted(POLICIES))


def get_policy(name: str) -> DispatchPolicy:
    """Instantiate a registered policy; unknown names raise ValidationError.

    Matching is exact (lower case): `"FIFO"` is not a registered name, so the
    string stored in a run record is always the string that is looked up.
    """
    if not isinstance(name, str):
        raise ValidationError(
            "policy name must be a string",
            details={"policy": repr(name), "available": list(available_policies())},
        )
    policy_class = POLICIES.get(name)
    if policy_class is None:
        raise ValidationError(
            "unknown dispatch policy",
            details={"policy": name, "available": list(available_policies())},
        )
    return policy_class()


def describe_policies() -> tuple[dict[str, str], ...]:
    """Return `{"name", "description"}` records for every registered policy."""
    return tuple(
        {"name": POLICY.name, "description": POLICY.description}
        for _, POLICY in sorted(POLICIES.items())
    )
