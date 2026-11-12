"""Tests for psbridge.experiment.validity (one test per gate)."""

from __future__ import annotations

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.validity import TOLERANCE, validate_schedule
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


def instance_2x2() -> Instance:
    return Instance(
        instance_id="v_2x2",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 3.0), Operation("J1_O2", "J1", 1, ("M2",), 2.0)),
            ),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 2.0), Operation("J2_O2", "J2", 1, ("M2",), 4.0)),
                release_time=1.0,
            ),
        ),
    )


def good_schedule() -> tuple[ScheduledOperation, ...]:
    return (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )


def failure_names(report) -> set[str]:
    return {check.name for check in report.failures()}


def test_valid_schedule_passes_every_gate() -> None:
    report = validate_schedule(instance_2x2(), good_schedule())
    assert report.valid
    names = {check.name for check in report.checks}
    assert names == {
        "no_unknown_operations",
        "no_duplicate_operations",
        "all_operations_scheduled",
        "non_negative_times",
        "duration_match",
        "machine_eligibility",
        "precedence",
        "release_time",
        "no_machine_overlap",
        "horizon_respected",
    }


def test_touching_intervals_do_not_overlap() -> None:
    # J1_O1 ends at 3.0 and J2_O1 starts at 3.0: allowed.
    report = validate_schedule(instance_2x2(), good_schedule())
    assert report.valid


def test_missing_operation_is_caught() -> None:
    report = validate_schedule(instance_2x2(), good_schedule()[:-1])
    assert not report.valid
    assert "all_operations_scheduled" in failure_names(report)


def test_unknown_operation_is_caught() -> None:
    schedule = good_schedule() + (ScheduledOperation("J9_O1", "J9", "M1", 9.0, 10.0),)
    report = validate_schedule(instance_2x2(), schedule)
    assert "no_unknown_operations" in failure_names(report)


def test_duplicate_operation_is_caught() -> None:
    schedule = good_schedule() + (ScheduledOperation("J1_O1", "J1", "M1", 9.0, 12.0),)
    report = validate_schedule(instance_2x2(), schedule)
    assert "no_duplicate_operations" in failure_names(report)


def test_precedence_violation_is_caught() -> None:
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
        ScheduledOperation("J1_O2", "J1", "M2", 2.0, 4.0),  # starts before 3.0
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )
    report = validate_schedule(instance_2x2(), schedule)
    assert "precedence" in failure_names(report)


def test_release_time_violation_is_caught() -> None:
    instance = Instance(
        instance_id="rel",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),), release_time=5.0),),
    )
    report = validate_schedule(instance, (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),))
    assert "release_time" in failure_names(report)
    ok = validate_schedule(instance, (ScheduledOperation("J1_O1", "J1", "M1", 5.0, 7.0),))
    assert ok.valid


def test_machine_eligibility_violation_is_caught() -> None:
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M2", 0.0, 3.0),  # J1_O1 is M1-only
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )
    report = validate_schedule(instance_2x2(), schedule)
    assert "machine_eligibility" in failure_names(report)


def test_machine_overlap_is_caught() -> None:
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
        ScheduledOperation("J2_O1", "J2", "M1", 2.0, 4.0),  # overlaps J1_O1
        ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )
    report = validate_schedule(instance_2x2(), schedule)
    assert "no_machine_overlap" in failure_names(report)


def test_duration_mismatch_is_caught() -> None:
    schedule = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 2.0),  # duration 2 != 3
        ScheduledOperation("J2_O1", "J2", "M1", 3.0, 5.0),
        ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),
        ScheduledOperation("J2_O2", "J2", "M2", 5.0, 9.0),
    )
    report = validate_schedule(instance_2x2(), schedule)
    assert "duration_match" in failure_names(report)


def test_duration_tolerance_is_one_nanosecond() -> None:
    instance = Instance(
        instance_id="tol",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 1.0),)),),
    )
    within = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 1.0 + TOLERANCE / 10.0),)
    assert validate_schedule(instance, within).valid
    outside = (ScheduledOperation("J1_O1", "J1", "M1", 0.0, 1.0 + TOLERANCE * 100.0),)
    assert "duration_match" in failure_names(validate_schedule(instance, outside))


def test_horizon_violation_is_caught() -> None:
    report = validate_schedule(instance_2x2(), good_schedule(), horizon=5.0)
    assert not report.valid
    assert "horizon_respected" in failure_names(report)
    assert validate_schedule(instance_2x2(), good_schedule(), horizon=9.0).valid


def test_horizon_gate_reports_no_horizon_when_absent() -> None:
    report = validate_schedule(instance_2x2(), good_schedule())
    horizon_checks = [check for check in report.checks if check.name == "horizon_respected"]
    assert len(horizon_checks) == 1
    assert horizon_checks[0].passed
    assert horizon_checks[0].detail == "no horizon"


def test_bad_arguments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_schedule("nope", good_schedule())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validate_schedule(instance_2x2(), None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validate_schedule(instance_2x2(), ("nope",))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validate_schedule(instance_2x2(), good_schedule(), horizon=-1.0)


def test_unscheduled_precedence_gap_is_not_reported_as_precedence() -> None:
    # Missing operations are reported by all_operations_scheduled; the
    # precedence gate must not crash or double-report when a job's operations
    # are only partially scheduled.
    schedule = (ScheduledOperation("J1_O2", "J1", "M2", 3.0, 5.0),)
    report = validate_schedule(instance_2x2(), schedule)
    assert "all_operations_scheduled" in failure_names(report)
    assert "precedence" not in failure_names(report)


def test_report_is_json_serialisable() -> None:
    import json

    payload = validate_schedule(instance_2x2(), good_schedule()).to_dict()
    json.dumps(payload)
    assert payload["valid"] is True
    assert all(check["passed"] for check in payload["checks"])
