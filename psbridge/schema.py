"""Domain model for scheduling instances (the stable contract).

Scope of v0.1
-------------
Classic flexible job-shop data:
  * jobs with an ordered route of operations,
  * every operation lists one or more eligible machines (routing flexibility),
  * non-preemptive processing, job release times, job due dates,
  * machines with unit capacity.

Extension points (documented, not implemented in v0.1): worker coupling,
transport resources (AGV / forklift), breakdowns, setup matrices, batching.
All identifiers must be valid SimTalk identifiers so that generated SimTalk
never needs string escaping or interpolation of untrusted text.

Validation is strict and fail-fast: invalid instances raise InstanceInvalid /
ValidationError instead of producing silently wrong schedules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from psbridge.errors import ObjectNotFound, ValidationError

SCHEMA_VERSION = "1"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_IDENTIFIER_LENGTH = 64


def validate_identifier(value: Any, *, field_name: str) -> str:
    """Validate a SimTalk-safe identifier and return it unchanged."""
    if not isinstance(value, str) or not value:
        raise ValidationError(field_name + " must be a non-empty string", details={"field": field_name, "value": repr(value)})
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            field_name + " is longer than " + str(MAX_IDENTIFIER_LENGTH) + " characters",
            details={"field": field_name, "value": value},
        )
    if not IDENTIFIER_RE.match(value):
        raise ValidationError(
            field_name + " must match ^[A-Za-z_][A-Za-z0-9_]*\\Z (SimTalk-safe identifier)",
            details={"field": field_name, "value": value},
        )
    return value


def validate_number(value: Any, *, field_name: str, minimum: float | None = None, strictly_positive: bool = False) -> float:
    """Validate a finite number and return it as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field_name + " must be a number", details={"field": field_name, "value": repr(value)})
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(field_name + " must be finite", details={"field": field_name, "value": repr(value)})
    if strictly_positive and result <= 0:
        raise ValidationError(field_name + " must be > 0", details={"field": field_name, "value": result})
    if minimum is not None and result < minimum:
        raise ValidationError(
            field_name + " must be >= " + repr(minimum), details={"field": field_name, "value": result}
        )
    return result


@dataclass(frozen=True)
class Operation:
    """One processing step of a job."""

    op_id: str
    job_id: str
    index: int
    candidates: tuple[str, ...]
    duration: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "op_id", validate_identifier(self.op_id, field_name="op_id"))
        object.__setattr__(self, "job_id", validate_identifier(self.job_id, field_name="job_id"))
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValidationError("operation index must be a non-negative integer", details={"op_id": self.op_id})
        if not isinstance(self.candidates, tuple):
            object.__setattr__(self, "candidates", tuple(self.candidates))
        if not self.candidates:
            raise ValidationError("operation has no eligible machines", details={"op_id": self.op_id})
        seen: set[str] = set()
        for machine_id in self.candidates:
            validate_identifier(machine_id, field_name="candidate machine_id")
            if machine_id in seen:
                raise ValidationError("duplicate eligible machine", details={"op_id": self.op_id, "machine_id": machine_id})
            seen.add(machine_id)
        object.__setattr__(self, "duration", validate_number(self.duration, field_name="duration", strictly_positive=True))


@dataclass(frozen=True)
class ScheduledOperation:
    """An operation placed on a concrete machine with concrete times."""

    op_id: str
    job_id: str
    machine_id: str
    start: float
    end: float

    def __post_init__(self) -> None:
        validate_identifier(self.op_id, field_name="op_id")
        validate_identifier(self.job_id, field_name="job_id")
        validate_identifier(self.machine_id, field_name="machine_id")
        start = validate_number(self.start, field_name="start", minimum=0.0)
        end = validate_number(self.end, field_name="end", minimum=0.0)
        if end <= start:
            raise ValidationError("scheduled operation must have end > start", details={"op_id": self.op_id, "start": start, "end": end})

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Job:
    """A job: ordered operations plus release/due information."""

    job_id: str
    operations: tuple[Operation, ...]
    release_time: float = 0.0
    due_date: float | None = None
    weight: float = 1.0

    def __post_init__(self) -> None:
        validate_identifier(self.job_id, field_name="job_id")
        if not isinstance(self.operations, tuple):
            object.__setattr__(self, "operations", tuple(self.operations))
        if not self.operations:
            raise ValidationError("job has no operations", details={"job_id": self.job_id})
        for position, operation in enumerate(self.operations):
            if not isinstance(operation, Operation):
                raise ValidationError("job operations must be Operation instances", details={"job_id": self.job_id})
            if operation.job_id != self.job_id:
                raise ValidationError(
                    "operation job_id mismatch", details={"job_id": self.job_id, "op_id": operation.op_id, "op_job_id": operation.job_id}
                )
            if operation.index != position:
                raise ValidationError(
                    "operation indexes must be contiguous starting at 0",
                    details={"job_id": self.job_id, "position": position, "index": operation.index},
                )
        object.__setattr__(self, "release_time", validate_number(self.release_time, field_name="release_time", minimum=0.0))
        if self.due_date is not None:
            object.__setattr__(self, "due_date", validate_number(self.due_date, field_name="due_date", minimum=0.0))
            if self.due_date < self.release_time:
                raise ValidationError("due_date must be >= release_time", details={"job_id": self.job_id})
        object.__setattr__(self, "weight", validate_number(self.weight, field_name="weight", strictly_positive=True))

    @property
    def processing_time(self) -> float:
        return sum(operation.duration for operation in self.operations)

    def operation(self, index: int) -> Operation:
        return self.operations[index]


@dataclass(frozen=True)
class Machine:
    """A machine with unit capacity (v0.1)."""

    machine_id: str
    capacity: int = 1

    def __post_init__(self) -> None:
        validate_identifier(self.machine_id, field_name="machine_id")
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity < 1:
            raise ValidationError("machine capacity must be a positive integer", details={"machine_id": self.machine_id, "capacity": self.capacity})


@dataclass(frozen=True)
class Instance:
    """A complete scheduling instance."""

    instance_id: str
    jobs: tuple[Job, ...]
    machines: tuple[Machine, ...]
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_identifier(self.instance_id, field_name="instance_id")
        if not isinstance(self.jobs, tuple):
            object.__setattr__(self, "jobs", tuple(self.jobs))
        if not isinstance(self.machines, tuple):
            object.__setattr__(self, "machines", tuple(self.machines))
        if not self.jobs:
            raise ValidationError("instance has no jobs")
        if not self.machines:
            raise ValidationError("instance has no machines")
        machine_ids: set[str] = set()
        for machine in self.machines:
            if not isinstance(machine, Machine):
                raise ValidationError("machines must be Machine instances")
            if machine.machine_id in machine_ids:
                raise ValidationError("duplicate machine_id", details={"machine_id": machine.machine_id})
            machine_ids.add(machine.machine_id)
        job_ids: set[str] = set()
        op_ids: set[str] = set()
        for job in self.jobs:
            if not isinstance(job, Job):
                raise ValidationError("jobs must be Job instances")
            if job.job_id in job_ids:
                raise ValidationError("duplicate job_id", details={"job_id": job.job_id})
            job_ids.add(job.job_id)
            for operation in job.operations:
                if operation.op_id in op_ids:
                    raise ValidationError("duplicate op_id", details={"op_id": operation.op_id})
                op_ids.add(operation.op_id)
                unknown = [machine_id for machine_id in operation.candidates if machine_id not in machine_ids]
                if unknown:
                    raise ValidationError(
                        "operation references unknown machines",
                        details={"op_id": operation.op_id, "unknown_machines": unknown},
                    )
        if not isinstance(self.meta, Mapping):
            raise ValidationError("instance meta must be a mapping")
        meta = dict(self.meta)
        try:
            json.dumps(meta, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValidationError("instance meta must be JSON-serialisable", details={"error": str(exc)}) from exc
        object.__setattr__(self, "meta", meta)

    # -- lookups ---------------------------------------------------------
    def machine_ids(self) -> tuple[str, ...]:
        return tuple(machine.machine_id for machine in self.machines)

    def job(self, job_id: str) -> Job:
        for job in self.jobs:
            if job.job_id == job_id:
                return job
        raise ObjectNotFound("unknown job", details={"job_id": job_id})

    def operation(self, op_id: str) -> Operation:
        for job in self.jobs:
            for operation in job.operations:
                if operation.op_id == op_id:
                    return operation
        raise ObjectNotFound("unknown operation", details={"op_id": op_id})

    def operations(self) -> tuple[Operation, ...]:
        return tuple(operation for job in self.jobs for operation in job.operations)

    @property
    def total_operations(self) -> int:
        return sum(len(job.operations) for job in self.jobs)

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "machines": [{"machine_id": machine.machine_id, "capacity": machine.capacity} for machine in self.machines],
            "jobs": [
                {
                    "job_id": job.job_id,
                    "release_time": job.release_time,
                    "due_date": job.due_date,
                    "weight": job.weight,
                    "operations": [
                        {
                            "op_id": operation.op_id,
                            "index": operation.index,
                            "candidates": list(operation.candidates),
                            "duration": operation.duration,
                        }
                        for operation in job.operations
                    ],
                }
                for job in self.jobs
            ],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Instance:
        if not isinstance(data, Mapping):
            raise ValidationError("instance JSON must be an object")
        allowed = {"schema_version", "instance_id", "machines", "jobs", "meta"}
        unknown = set(data) - allowed
        if unknown:
            raise ValidationError("unknown instance fields", details={"unknown": sorted(unknown)})
        if "instance_id" not in data:
            raise ValidationError("instance_id is required")
        jobs_raw = data.get("jobs", ())
        if not isinstance(jobs_raw, Sequence) or isinstance(jobs_raw, (str, bytes)):
            raise ValidationError("instance jobs must be an array")
        jobs: list[Job] = []
        for job_index, job_data in enumerate(jobs_raw):
            if not isinstance(job_data, Mapping):
                raise ValidationError("each job entry must be an object", details={"job_index": job_index})
            if "job_id" not in job_data:
                raise ValidationError("job entry is missing 'job_id'", details={"job_index": job_index})
            job_unknown = set(job_data) - {"job_id", "release_time", "due_date", "weight", "operations"}
            if job_unknown:
                raise ValidationError(
                    "unknown job fields",
                    details={"job_id": job_data.get("job_id"), "unknown": sorted(job_unknown)},
                )
            ops_raw = job_data.get("operations", ())
            if not isinstance(ops_raw, Sequence) or isinstance(ops_raw, (str, bytes)):
                raise ValidationError(
                    "job operations must be an array",
                    details={"job_id": job_data.get("job_id")},
                )
            operations: list[Operation] = []
            for op_index, operation_data in enumerate(ops_raw):
                if not isinstance(operation_data, Mapping):
                    raise ValidationError(
                        "each operation entry must be an object",
                        details={"job_id": job_data.get("job_id"), "operation_index": op_index},
                    )
                op_unknown = set(operation_data) - {"op_id", "index", "candidates", "duration"}
                if op_unknown:
                    raise ValidationError("unknown operation fields", details={"unknown": sorted(op_unknown)})
                missing_op = [k for k in ("op_id", "index", "candidates", "duration") if k not in operation_data]
                if missing_op:
                    raise ValidationError(
                        "operation entry is missing required fields",
                        details={"job_id": job_data.get("job_id"), "missing": missing_op},
                    )
                candidates_raw = operation_data["candidates"]
                if not isinstance(candidates_raw, Sequence) or isinstance(candidates_raw, (str, bytes)):
                    raise ValidationError(
                        "operation candidates must be an array",
                        details={"op_id": operation_data.get("op_id")},
                    )
                operations.append(
                    Operation(
                        op_id=operation_data["op_id"],
                        job_id=job_data["job_id"],
                        index=operation_data["index"],
                        candidates=tuple(candidates_raw),
                        duration=operation_data["duration"],
                    )
                )
            jobs.append(
                Job(
                    job_id=job_data["job_id"],
                    operations=tuple(operations),
                    release_time=job_data.get("release_time", 0.0),
                    due_date=job_data.get("due_date"),
                    weight=job_data.get("weight", 1.0),
                )
            )
        machines_raw = data.get("machines", ())
        if not isinstance(machines_raw, Sequence) or isinstance(machines_raw, (str, bytes)):
            raise ValidationError("instance machines must be an array")
        machines: list[Machine] = []
        for machine_index, machine_data in enumerate(machines_raw):
            if not isinstance(machine_data, Mapping):
                raise ValidationError("each machine entry must be an object", details={"machine_index": machine_index})
            machine_unknown = set(machine_data) - {"machine_id", "capacity"}
            if machine_unknown:
                raise ValidationError("unknown machine fields", details={"unknown": sorted(machine_unknown)})
            if "machine_id" not in machine_data:
                raise ValidationError("machine entry is missing 'machine_id'", details={"machine_index": machine_index})
            machines.append(
                Machine(machine_id=machine_data["machine_id"], capacity=machine_data.get("capacity", 1))
            )
        meta_raw = data.get("meta", {})
        if not isinstance(meta_raw, Mapping):
            raise ValidationError("instance meta must be a mapping")
        return cls(
            instance_id=data["instance_id"],
            jobs=tuple(jobs),
            machines=tuple(machines),
            meta=dict(meta_raw),
        )

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def with_durations(self, durations: Mapping[str, float]) -> Instance:
        """Return a copy with overridden operation durations (used for seed variants)."""
        unknown = set(durations) - {operation.op_id for operation in self.operations()}
        if unknown:
            raise ValidationError("unknown operation ids in duration override", details={"op_ids": sorted(unknown)})
        new_jobs: list[Job] = []
        for job in self.jobs:
            new_operations = tuple(
                Operation(
                    op_id=operation.op_id,
                    job_id=operation.job_id,
                    index=operation.index,
                    candidates=operation.candidates,
                    duration=durations.get(operation.op_id, operation.duration),
                )
                for operation in job.operations
            )
            new_jobs.append(
                Job(
                    job_id=job.job_id,
                    operations=new_operations,
                    release_time=job.release_time,
                    due_date=job.due_date,
                    weight=job.weight,
                )
            )
        return Instance(instance_id=self.instance_id, jobs=tuple(new_jobs), machines=self.machines, meta=dict(self.meta))

    def job_of(self, op_id: str) -> Job:
        return self.job(self.operation(op_id).job_id)


def iter_operations(instance: Instance) -> Iterable[Operation]:
    for job in instance.jobs:
        yield from job.operations
