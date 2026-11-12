"""Tests for the FIFO / SPT / EDD policies and the registry."""

from __future__ import annotations

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.engine import ReadyOperation
from psbridge.scheduling.policies import (
    POLICIES,
    EDDPolicy,
    FIFOPolicy,
    SPTPolicy,
    available_policies,
    describe_policies,
    get_policy,
)


def ready(*, op_id="J1_O1", job_id="J1", index=0, duration=1.0, ready_since=0.0, due_date=None, release=0.0, weight=1.0):
    return ReadyOperation(
        op_id=op_id,
        job_id=job_id,
        index=index,
        duration=duration,
        candidates=("M1",),
        ready_since=ready_since,
        job_release=release,
        due_date=due_date,
        weight=weight,
    )


def test_registry_names_are_stable() -> None:
    assert available_policies() == ("edd", "fifo", "spt")
    assert set(POLICIES) == {"fifo", "spt", "edd"}
    assert get_policy("fifo").name == "fifo"
    assert get_policy("spt").name == "spt"
    assert get_policy("edd").name == "edd"


@pytest.mark.parametrize("name", ["fifo", "spt", "edd"])
def test_every_policy_is_a_dispatch_policy(name: str) -> None:
    policy = get_policy(name)
    assert policy.name == name
    assert isinstance(policy.description, str) and policy.description
    assert callable(policy.preference)


@pytest.mark.parametrize("name", ["FIFO", "SPT", "Edd", "", " fifo", "random", "fcfs", "ls"])
def test_unknown_policy_rejected(name: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        get_policy(name)
    assert excinfo.value.code == "VALIDATION_ERROR"
    assert "available" in excinfo.value.details


def test_non_string_policy_rejected() -> None:
    with pytest.raises(ValidationError):
        get_policy(None)  # type: ignore[arg-type]


def test_get_policy_returns_fresh_instances() -> None:
    assert get_policy("fifo") is not get_policy("fifo")
    assert isinstance(get_policy("fifo"), FIFOPolicy)
    assert isinstance(get_policy("spt"), SPTPolicy)
    assert isinstance(get_policy("edd"), EDDPolicy)


def test_describe_policies_is_sorted_and_complete() -> None:
    described = describe_policies()
    assert [item["name"] for item in described] == ["edd", "fifo", "spt"]
    for item in described:
        assert item["description"] == get_policy(item["name"]).description


def test_preference_keys_are_exactly_the_documented_tuples() -> None:
    operation = ready(duration=4.0, ready_since=2.5, due_date=7.0)
    assert FIFOPolicy().preference(operation) == (2.5,)
    assert SPTPolicy().preference(operation) == (4.0,)
    assert EDDPolicy().preference(operation) == (7.0,)


def test_edd_without_due_date_sorts_last() -> None:
    no_due = ready(due_date=None)
    with_due = ready(op_id="J2_O1", job_id="J2", due_date=5.0)
    assert EDDPolicy().preference(no_due) == (float("inf"),)
    assert EDDPolicy().preference(with_due) < EDDPolicy().preference(no_due)


def test_policies_never_do_tie_breaks_themselves() -> None:
    """A policy key is a 1-tuple; the canonical tie-break is added by the selector."""
    operation = ready()
    for policy in (FIFOPolicy(), SPTPolicy(), EDDPolicy()):
        assert isinstance(policy.preference(operation), tuple)
        assert len(policy.preference(operation)) == 1


def test_policy_instances_are_stateless() -> None:
    first = get_policy("spt")
    second = get_policy("spt")
    assert first.preference(ready(duration=1.0)) == second.preference(ready(duration=1.0))
    assert repr(first) == "SPTPolicy()"
