"""Post-hoc validity gates for a produced schedule.

The runner only accepts a run as evidence when every gate here passes. The
gates are deliberately independent of the engine: any engine (fake, reference
or SimTalk-driven) must be able to feed its schedule in.

Tolerance
---------
Float comparisons use math.isclose with rel_tol = abs_tol = 1e-9 (TOLERANCE
below). That is tight enough to catch a real precedence or duration violation
and loose enough to survive float accumulation in long schedules.

Gates
-----
all_operations_scheduled  every instance operation appears in the schedule
no_unknown_operations     the schedule contains no operation outside the instance
no_duplicate_operations   no operation is scheduled twice
no_machine_overlap        intervals on one machine do not overlap (touching
                          endpoints are allowed: end == next start)
duration_match            end - start matches the instance duration
machine_eligibility       the chosen machine is a candidate of the operation
precedence                op k starts at or after op k-1 of the same job ends
release_time              the first operation of a job starts at or after the
                          job release time
horizon_respected         (only when a horizon is given) every end <= horizon
non_negative_times        start >= 0 and end > start for every entry
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from psbridge.errors import ValidationError
from psbridge.experiment.contracts import CheckResult, ValidityReport
from psbridge.schema import Instance, ScheduledOperation, validate_number

__all__ = ["TOLERANCE", "validate_schedule"]

TOLERANCE = 1e-9


def _close(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=TOLERANCE, abs_tol=TOLERANCE)


def validate_schedule(
    instance: Instance,
    schedule: Sequence[ScheduledOperation],
    horizon: float | None = None,
) -> ValidityReport:
    """Run every validity gate and return a ValidityReport (always all gates)."""
    if not isinstance(instance, Instance):
        raise ValidationError("validate_schedule expects an Instance")
    if schedule is None:
        raise ValidationError("schedule must be a sequence of ScheduledOperation")
    items = tuple(schedule)
    bad_types = [type(item).__name__ for item in items if not isinstance(item, ScheduledOperation)]
    if bad_types:
        raise ValidationError(
            "schedule items must be ScheduledOperation instances", details={"types": bad_types}
        )
    horizon_value: float | None = None
    if horizon is not None:
        horizon_value = validate_number(horizon, field_name="horizon", strictly_positive=True)

    checks: list[CheckResult] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append(CheckResult(name=name, passed=bool(passed), detail=detail))

    operations = {operation.op_id: operation for operation in instance.operations()}
    by_id: dict[str, list[ScheduledOperation]] = {}
    for item in items:
        by_id.setdefault(item.op_id, []).append(item)

    unknown = sorted(set(by_id) - set(operations))
    add(
        "no_unknown_operations",
        not unknown,
        "" if not unknown else "not in instance: " + ", ".join(unknown[:10]),
    )

    duplicates = sorted(op_id for op_id, entries in by_id.items() if len(entries) > 1)
    add(
        "no_duplicate_operations",
        not duplicates,
        "" if not duplicates else "scheduled more than once: " + ", ".join(duplicates[:10]),
    )

    missing = sorted(set(operations) - set(by_id))
    add(
        "all_operations_scheduled",
        not missing,
        "" if not missing else "missing: " + ", ".join(missing[:10]),
    )

    time_problems: list[str] = []
    for item in items:
        if not item.start >= 0.0 or not item.end > item.start:
            time_problems.append(item.op_id)
    add(
        "non_negative_times",
        not time_problems,
        "" if not time_problems else "invalid times: " + ", ".join(time_problems[:10]),
    )

    duration_problems: list[str] = []
    for item in items:
        operation = operations.get(item.op_id)
        if operation is None:
            continue
        if not _close(item.end - item.start, operation.duration):
            duration_problems.append(
                item.op_id + " (" + repr(item.end - item.start) + " != " + repr(operation.duration) + ")"
            )
    add(
        "duration_match",
        not duration_problems,
        "" if not duration_problems else "; ".join(duration_problems[:5]),
    )

    eligibility_problems: list[str] = []
    for item in items:
        operation = operations.get(item.op_id)
        if operation is None:
            continue
        if item.machine_id not in operation.candidates:
            eligibility_problems.append(
                item.op_id + " on " + item.machine_id + " not in " + ",".join(operation.candidates)
            )
    add(
        "machine_eligibility",
        not eligibility_problems,
        "" if not eligibility_problems else "; ".join(eligibility_problems[:5]),
    )

    precedence_problems: list[str] = []
    for job in instance.jobs:
        previous_end: float | None = None
        for operation in job.operations:
            entries = by_id.get(operation.op_id)
            if not entries:
                previous_end = None
                continue
            for current in entries:
                if previous_end is not None and current.start < previous_end - TOLERANCE:
                    precedence_problems.append(
                        operation.op_id
                        + " starts at "
                        + repr(current.start)
                        + " before predecessor end "
                        + repr(previous_end)
                    )
            previous_end = entries[0].end
    add(
        "precedence",
        not precedence_problems,
        "" if not precedence_problems else "; ".join(precedence_problems[:5]),
    )

    release_problems: list[str] = []
    for job in instance.jobs:
        first = job.operations[0]
        entries = by_id.get(first.op_id)
        if not entries:
            continue
        for entry in entries:
            if entry.start < float(job.release_time) - TOLERANCE:
                release_problems.append(
                    first.op_id + " starts at " + repr(entry.start) + " before release " + repr(job.release_time)
                )
    add(
        "release_time",
        not release_problems,
        "" if not release_problems else "; ".join(release_problems[:5]),
    )

    overlap_problems: list[str] = []
    by_machine: dict[str, list[ScheduledOperation]] = {}
    for item in items:
        by_machine.setdefault(item.machine_id, []).append(item)
    for machine_id, entries in by_machine.items():
        ordered = sorted(entries, key=lambda entry: (entry.start, entry.end, entry.op_id))
        for previous, current in itertools.pairwise(ordered):
            if current.start < previous.end - TOLERANCE:
                overlap_problems.append(
                    machine_id
                    + ": "
                    + previous.op_id
                    + " ["
                    + repr(previous.start)
                    + ", "
                    + repr(previous.end)
                    + "] overlaps "
                    + current.op_id
                    + " starting "
                    + repr(current.start)
                )
    add(
        "no_machine_overlap",
        not overlap_problems,
        "" if not overlap_problems else "; ".join(overlap_problems[:5]),
    )

    if horizon_value is None:
        add("horizon_respected", True, "no horizon")
    else:
        horizon_problems = [item.op_id for item in items if item.end > horizon_value + TOLERANCE]
        add(
            "horizon_respected",
            not horizon_problems,
            "" if not horizon_problems else "beyond horizon: " + ", ".join(horizon_problems[:10]),
        )

    return ValidityReport.from_checks(tuple(checks))
