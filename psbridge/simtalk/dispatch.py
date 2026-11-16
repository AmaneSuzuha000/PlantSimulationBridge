"""Dispatch and schedule-feed generators verified on Plant Simulation 13.

Three measured behaviors shape the code below:

* A Method's source is the attribute `Program`, and it is assigned through the
  method's own path -- `<method>.Program := "..."`. The documented
  `ref(<method>).Program` form works for READING but fails on assignment with
  "source contains a syntax error" (Informationflow.29.641; probe
  `method_program` / `delivery_table`).
* An exit control is a Method assigned to `<object>.ExitCtrl`; inside it,
  `@.move(<target>)` chooses the successor (MaterialFlowObjects12.20.270;
  probe `routing` -- the per-machine split follows the Program across
  identical runs, so the control really decides).
* A Source is configured with `MuSelection` / `Path` / `TimeOfGeneration` /
  `Number` / `Start`; there is no `MU` and no `Count` attribute
  (MaterialFlowObjects222.24.62-73; probe `mu_flow`, `schedule_native`).

The per-job Source form below expresses the same schedule a delivery table
would -- job j released at t_j with n_j units -- but uses only attributes whose
behaviour is measured. The TableFile delivery-table variant is documented but
its column data types could not be resolved on this build, so it is NOT used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from psbridge.core.validation import (
    format_simtalk_number,
    validate_coordinate,
    validate_identifier,
    validate_object_path,
    validate_simtalk_object_name,
)
from psbridge.errors import ValidationError

__all__ = [
    "MU_CLASS",
    "SCHEDULE_TIME_OF_GENERATION",
    "ScheduledJob",
    "build_exit_control_program",
    "build_queue_rule",
    "build_schedule_source_statements",
]

#: Default mobile-unit class moved in generated routing snippets.
#: Plant Simulation 13 provides `.MUs.Entity`, `.MUs.Container`, and `.MUs.Transporter`.
MU_CLASS = ".MUs.Entity"

#: The only TimeOfGeneration value that lets us set Number and Start
#: independently. Measured accepted set: "Interval adjustable",
#: "Number adjustable", "Delivery table", "Trigger".
SCHEDULE_TIME_OF_GENERATION = "Number adjustable"


@dataclass(frozen=True)
class ScheduledJob:
    """One release order: `units` MUs become available at `release` seconds."""

    name: str
    release: float
    units: int

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="job name")
        release = validate_coordinate(self.release, field_name="release time")
        if release < 0:
            raise ValidationError("release time must be >= 0, got " + repr(self.release))
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise ValidationError("units must be >= 1, got " + repr(self.units))


def build_schedule_source_statements(
    job: ScheduledJob,
    frame_path: str,
    *,
    source_class: str = ".MaterialFlow.Source",
    mu_class: str = MU_CLASS,
) -> tuple[str, ...]:
    """Statements that create one Source and make it release a job's units.

    Deterministic: the same job and frame always produce byte-identical output.
    """
    if not isinstance(job, ScheduledJob):
        raise ValidationError("job must be a ScheduledJob", details={"type": type(job).__name__})
    validate_simtalk_object_name(job.name, field_name="job name")
    frame_path = validate_object_path(frame_path, field_name="frame path")
    source_class = validate_object_path(source_class, field_name="source class")
    mu_class = validate_object_path(mu_class, field_name="MU class")
    path = frame_path + "." + job.name
    # createObject does not take a name, so the object arrives with a generated
    # one ("Source", "Source1", ...). Rename it by addressing the node that was
    # just added -- measured: rename then address-by-name works.
    rename = frame_path + ".node(" + frame_path + ".NumNodes).Name := " + '"' + job.name + '"' + ";"
    return (
        source_class + ".createObject(" + frame_path + ", 120, 240);",
        rename,
        path + '.MuSelection := "Constant";',
        path + ".Path := " + '"' + mu_class + '"' + ";",
        path + '.TimeOfGeneration := "' + SCHEDULE_TIME_OF_GENERATION + '";',
        path + ".Number := " + str(job.units) + ";",
        path + ".Start := " + _seconds(job.release) + ";",
    )


def build_queue_rule(candidates: Sequence[str]) -> str:
    """SimTalk body: send @ to whichever candidate has the least work in it.

    Work in progress is `statNumIn - statNumOut`, both measured to exist. Ties
    go to the earlier candidate, so the rule is deterministic.
    """
    validated = tuple(validate_object_path(candidate, field_name="candidate") for candidate in candidates)
    if len(validated) < 2:
        raise ValidationError("routing needs at least two candidate machines")
    if len(set(validated)) != len(validated):
        raise ValidationError("candidate machines must be distinct", details={"candidates": list(validated)})
    return _argmin(list(validated))


def _argmin(candidates: list[str]) -> str:
    """SimTalk that moves @ to the candidate with the least work in progress.

    Built only from nested `if` / `else` / `end;` and early `return true;`
    statements -- no `and` and no local variables -- because both are proven on
    Plant Simulation 13 (`jobshop.py` and `dispatch.py`). For two candidates,
    preserves the exact single `if ... else ... end;` form verified on the real
    product; for 3+ candidates, emits O(N^2) guarded blocks rather than an
    exponential O(2^N) decision tree. Ties go to the earlier candidate.
    """
    if len(candidates) == 1:
        return "@.move(" + candidates[0] + ");"
    if len(candidates) == 2:
        first, second = candidates
        return (
            "if " + _wip(first) + " <= " + _wip(second) + " then "
            + "@.move(" + first + "); else @.move(" + second + "); end;"
        )
    clauses: list[str] = []
    last_index = len(candidates) - 1
    for index, candidate in enumerate(candidates[:-1]):
        block = "@.move(" + candidate + "); return true;"
        for other_index in range(last_index, -1, -1):
            if other_index == index:
                continue
            op = " < " if other_index < index else " <= "
            block = "if " + _wip(candidate) + op + _wip(candidates[other_index]) + " then " + block + " end;"
        clauses.append(block)
    clauses.append("@.move(" + candidates[-1] + ");")
    return " ".join(clauses)


def build_exit_control_program(candidates: Sequence[str]) -> str:
    """A complete `Program` value for an exit control that dispatches by queue.

    The `:boolean is do ... end;` wrapper is mandatory -- a bare statement is
    rejected as a syntax error when assigned to `Program`.
    """
    return ":boolean is do " + build_queue_rule(candidates) + " return true; end;"


def _wip(path: str) -> str:
    return "(" + path + ".statNumIn - " + path + ".statNumOut)"


def _seconds(value: float) -> str:
    return format_simtalk_number(value, field_name="seconds")
