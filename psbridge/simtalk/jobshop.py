"""Generate a job shop whose buffers absorb parts that cannot advance.

Why this shape exists
---------------------
A part that finishes an operation and cannot enter its next machine normally
stays on the current machine. The machine keeps its capacity, so the other job
can never take it: two jobs that visit the same two machines in opposite orders
deadlock on unit-capacity machines. A zero-time "buffer" made from a
SingleProc with ``ProcTime=0`` behaves the same way because a zero-time station
still holds capacity.

The fix is a real buffer between operations, large enough that the move out of
a machine always succeeds:

    Source_j -> M(op1) --done--> Buf_j_2 -> M(op2) --done--> ... -> Drain

A part therefore never holds a machine while it waits.

Per-operation durations
-----------------------
When every operation targeting a machine shares a single duration, the generator
sets ``M_<machine>.ProcTime`` statically. When any machine must execute
operations with different durations, the generator defines ``OpDuration`` on
``.MUs.Entity``, stamps each MU before moving it onto its next machine, and
attaches an ``EntranceCtrl`` (with ``EntranceCtrlBeforeActions := true``) on
each multi-duration machine so ``?.ProcTime := @.OpDuration`` runs before
Plant Simulation samples the station's processing time.
"""
from __future__ import annotations

from dataclasses import dataclass

from psbridge.core.validation import (
    validate_coordinate,
    validate_identifier,
    validate_object_path,
)
from psbridge.errors import ValidationError

MU_CLASS = ".MUs.Entity"
BUFFER_CAPACITY = 1000
JOB_ID_ATTRIBUTE = "JobId"
OP_INDEX_ATTRIBUTE = "OpIndex"
OP_DURATION_ATTRIBUTE = "OpDuration"


@dataclass(frozen=True)
class Operation:
    """One operation of a job: which machine performs it, and for how long."""

    machine: str
    duration: float

    def __post_init__(self) -> None:
        validate_identifier(self.machine, field_name="machine name")
        duration = validate_coordinate(self.duration, field_name="operation duration")
        if duration < 0:
            raise ValidationError(
                "operation duration must not be negative",
                details={"machine": self.machine, "duration": self.duration},
            )


@dataclass(frozen=True)
class Job:
    """A job released as a whole batch, visiting its operations in order."""

    name: str
    units: int
    start: float
    interval: float
    operations: tuple[Operation, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="job name")
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise ValidationError(
                "a job needs at least one unit", details={"job": self.name, "units": repr(self.units)}
            )
        start = validate_coordinate(self.start, field_name="job start")
        if start < 0:
            raise ValidationError("job start must not be negative", details={"job": self.name, "start": self.start})
        interval = validate_coordinate(self.interval, field_name="job interval")
        if interval < 0:
            raise ValidationError(
                "job interval must not be negative", details={"job": self.name, "interval": self.interval}
            )
        if not isinstance(self.operations, tuple):
            object.__setattr__(self, "operations", tuple(self.operations))
        if not self.operations:
            raise ValidationError("a job needs at least one operation", details={"job": self.name})
        for operation in self.operations:
            if not isinstance(operation, Operation):
                raise ValidationError(
                    "job operations must be Operation instances",
                    details={"job": self.name, "type": type(operation).__name__},
                )


def _seconds(value: float) -> str:
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _quote(text: str) -> str:
    return '"' + text + '"'


def machine_durations(jobs) -> dict[str, tuple[float, ...]]:
    """Map each machine name to the distinct operation durations it executes."""
    seen: dict[str, list[float]] = {}
    for job in jobs:
        for operation in job.operations:
            bucket = seen.setdefault(operation.machine, [])
            if not any(abs(existing - operation.duration) <= 1e-9 for existing in bucket):
                bucket.append(operation.duration)
    return {machine: tuple(values) for machine, values in seen.items()}


def machine_schedule(jobs, *, strict: bool = False) -> dict[str, float]:
    """Map each machine name to its representative duration (or raise when ``strict=True`` and mixed)."""
    by_machine = machine_durations(jobs)
    if strict:
        for machine, values in by_machine.items():
            if len(values) > 1:
                raise ValidationError(
                    "one machine cannot run two different operation durations",
                    details={"machine": machine, "first": values[0], "second": values[1]},
                )
    return {machine: values[0] for machine, values in by_machine.items()}


def build_job_shop_statements(jobs, frame_path: str, *, drain: str = "Drain1") -> list[str]:
    """Return the SimTalk statements that build and run the job shop.

    The caller executes them in order. Object creation comes first and method
    creation last, because an information-flow object is only reachable through
    node() and that handle has to be captured the moment the object exists.
    """
    frame_path = validate_object_path(frame_path, field_name="frame_path")
    drain = validate_identifier(drain, field_name="drain name")
    jobs = list(jobs)
    if not jobs:
        raise ValidationError("a job shop needs at least one job")
    seen_jobs: set[str] = set()
    for job in jobs:
        if not isinstance(job, Job):
            raise ValidationError("jobs must be Job instances", details={"type": type(job).__name__})
        validate_identifier(job.name, field_name="job name")
        if job.name in seen_jobs:
            raise ValidationError("duplicate job name in job shop", details={"job": job.name})
        seen_jobs.add(job.name)

    by_machine = machine_durations(jobs)
    heterogeneous_machines = {name for name, values in by_machine.items() if len(values) > 1}
    use_dynamic_durations = bool(heterogeneous_machines)
    durations = {name: values[0] for name, values in by_machine.items()}

    reserved_names = {"M_" + name for name in durations}
    for job in jobs:
        reserved_names.add("Src_" + job.name)
        for step in range(2, len(job.operations) + 1):
            reserved_names.add("Buf_" + job.name + "_" + str(step))
    if drain in reserved_names:
        raise ValidationError("drain name collides with generated object name", details={"drain": drain})
    statements: list[str] = []

    statements.append(MU_CLASS + '.createAttr(' + _quote(JOB_ID_ATTRIBUTE) + ', "integer");')
    statements.append(MU_CLASS + '.createAttr(' + _quote(OP_INDEX_ATTRIBUTE) + ', "integer");')
    if use_dynamic_durations:
        statements.append(MU_CLASS + '.createAttr(' + _quote(OP_DURATION_ATTRIBUTE) + ', "real");')

    # -- material flow objects ------------------------------------------
    statements.append(".MaterialFlow.Drain.createObject(" + frame_path + ", 900, 200);")
    statements.append(frame_path + ".node(" + frame_path + ".NumNodes).Name := " + _quote(drain) + ";")
    drain_path = frame_path + "." + drain

    for name in sorted(durations):
        statements.append(".MaterialFlow.SingleProc.createObject(" + frame_path + ", 400, 200);")
        statements.append(frame_path + ".node(" + frame_path + ".NumNodes).Name := " + _quote("M_" + name) + ";")
        statements.append(frame_path + ".M_" + name + ".ProcTime := " + _seconds(durations[name]) + ";")
        if name in heterogeneous_machines:
            statements.append(frame_path + ".M_" + name + ".EntranceCtrlBeforeActions := true;")

    for job in jobs:
        statements.append(".MaterialFlow.Source.createObject(" + frame_path + ", 100, 200);")
        statements.append(frame_path + ".node(" + frame_path + ".NumNodes).Name := " + _quote("Src_" + job.name) + ";")
        # One buffer per operation after the first: that is what a part waits in.
        for step in range(2, len(job.operations) + 1):
            statements.append(".MaterialFlow.PlaceBuffer.createObject(" + frame_path + ", 600, 300);")
            statements.append(frame_path + ".node(" + frame_path + ".NumNodes).Name := "
                              + _quote("Buf_" + job.name + "_" + str(step)) + ";")
            statements.append(frame_path + ".Buf_" + job.name + "_" + str(step)
                              + ".Capacity := " + str(BUFFER_CAPACITY) + ";")

    # A machine pulls from the buffer in front of it; without this connector the
    # buffer fills and never drains.
    for job in jobs:
        for step in range(2, len(job.operations) + 1):
            statements.append(".MaterialFlow.Connector.connect("
                              + frame_path + ".Buf_" + job.name + "_" + str(step) + ", "
                              + frame_path + ".M_" + job.operations[step - 1].machine + ");")

    # -- methods --------------------------------------------------------
    # A method handle from node(NumNodes) is wired to its owner right after
    # creation so subsequent createObject calls cannot shift the index.
    def new_method(x: int, y: int, body: str, owner: str, *, control: str = "ExitCtrl") -> list[str]:
        return [
            ".InformationFlow.&Method.createObject(" + frame_path + ", " + str(x) + ", " + str(y) + ");",
            frame_path + ".node(" + frame_path + ".NumNodes).Program := "
            + _quote(":boolean is do " + body + " return true; end;") + ";",
            owner + "." + control + " := " + frame_path + ".node(" + frame_path + ".NumNodes);",
        ]

    for name in sorted(heterogeneous_machines):
        statements.extend(
            new_method(
                400,
                500,
                "?.ProcTime := @." + OP_DURATION_ATTRIBUTE + ";",
                frame_path + ".M_" + name,
                control="EntranceCtrl",
            )
        )

    for job_number, job in enumerate(jobs, start=1):
        statements.extend(
            new_method(
                100,
                600,
                _init_body(job_number, job, frame_path, include_duration=use_dynamic_durations),
                frame_path + ".Src_" + job.name,
            )
        )
    guards_by_machine = _build_routing_guards(
        jobs, frame_path, drain_path, include_duration=use_dynamic_durations
    )
    for name in sorted(durations):
        guards = guards_by_machine.get(name)
        if not guards:
            raise ValidationError("a machine has no operation", details={"machine": name})
        statements.extend(
            new_method(400, 600, " ".join(guards), frame_path + ".M_" + name)
        )

    # -- release schedule ----------------------------------------------
    for job in jobs:
        source = frame_path + ".Src_" + job.name
        statements.extend([
            source + '.MuSelection := "Constant";',
            source + '.Path := "' + MU_CLASS + '";',
            source + '.TimeOfGeneration := "Number adjustable";',
            source + ".Number := " + str(job.units) + ";",
            source + ".Interval := " + _seconds(job.interval) + ";",
            source + ".Start := " + _seconds(job.start) + ";",
        ])
    return statements


def _init_body(job_number: int, job: Job, frame_path: str, *, include_duration: bool = False) -> str:
    """The program a source runs: stamp the part and send it to its first machine."""
    duration_stmt = ""
    if include_duration:
        duration_stmt = "@." + OP_DURATION_ATTRIBUTE + " := " + _seconds(job.operations[0].duration) + "; "
    return (
        "@." + JOB_ID_ATTRIBUTE + " := " + str(job_number) + "; "
        "@." + OP_INDEX_ATTRIBUTE + " := 1; "
        + duration_stmt
        + "@.move(" + frame_path + ".M_" + job.operations[0].machine + ");"
    )


def _build_routing_guards(
    jobs: list[Job],
    frame_path: str,
    drain_path: str,
    *,
    include_duration: bool = False,
) -> dict[str, list[str]]:
    """Build per-machine flat routing guards in a single pass over jobs/operations.

    The statements are flat on purpose: each candidate is an independent `if/end`
    whose body ends with `return true;`, so only the matching branch runs and no
    else-chain association can loop or stall during runtime dispatch.
    """
    guards_by_machine: dict[str, list[str]] = {}
    for number, job in enumerate(jobs, start=1):
        op_count = len(job.operations)
        for step, operation in enumerate(job.operations, start=1):
            if step < op_count:
                duration_stmt = ""
                if include_duration:
                    duration_stmt = (
                        "@." + OP_DURATION_ATTRIBUTE + " := "
                        + _seconds(job.operations[step].duration) + "; "
                    )
                action = (
                    "@." + OP_INDEX_ATTRIBUTE + " := " + str(step + 1) + "; "
                    + duration_stmt
                    + "@.move(" + frame_path + ".Buf_" + job.name + "_" + str(step + 1) + "); return true;"
                )
            else:
                action = "@.move(" + drain_path + "); return true;"
            guard = (
                "if @." + JOB_ID_ATTRIBUTE + " = " + str(number) + " then if @."
                + OP_INDEX_ATTRIBUTE + " = " + str(step) + " then " + action + " end; end;"
            )
            guards_by_machine.setdefault(operation.machine, []).append(guard)
    return guards_by_machine
