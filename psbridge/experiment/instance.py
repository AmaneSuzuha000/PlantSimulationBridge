"""Strict instance I/O, validation and seeded duration variants.

Design notes
------------
* The only JSON <-> Instance conversion lives here, so every experiment reads
  exactly the schema in psbridge.schema. There is intentionally no CRUD
  beyond load/save.
* JSON is strict: Instance.from_dict rejects unknown fields, and the
  non-standard constants NaN / Infinity / -Infinity are rejected at parse time
  (Python's json module accepts them by default, which would smuggle
  non-finite durations into an experiment).
* save_instance is atomic: it writes a temporary file next to the target,
  fsyncs it and then calls os.replace. A crash therefore leaves either the
  previous file or the new one, never a half-written instance.
* sample_variant is deterministic per (instance, seed) and uses a private
  random.Random(seed). The global random module is never read or written, so
  sampling order cannot leak between experiments.

Seeded duration variants
------------------------
Every operation duration d is multiplied by uniform(1 - jitter, 1 + jitter),
drawn once per operation in instance.operations() order (jobs in instance
order, operations in index order). Because the draw sequence depends on the
operation order, variants are comparable only for the same base instance.
Durations stay strictly positive; the mathematical lower bound is
d * (1 - jitter) > 0, and a defensive clamp to 1e-9 covers subnormal inputs.

validate_instance re-checks structural invariants of an Instance object even
though the dataclasses already validate on construction: instances may come
from untrusted JSON, from pickled caches or from tests that mutate frozen
objects, and a re-check is cheap compared with a wrong experiment.

validate_engine_compatibility reports the two v0.1 engine capabilities
(unit machine capacity, single-candidate routing) separately, because a
flexibly routed instance is a perfectly valid FJSP instance even though the
v0.1 engine cannot schedule it.
"""

from __future__ import annotations

import json
import math
import os
import random
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from psbridge.errors import ValidationError
from psbridge.experiment.contracts import CheckResult, ValidityReport
from psbridge.schema import (
    SCHEMA_VERSION,
    Instance,
    validate_identifier,
    validate_number,
)

__all__ = [
    "MIN_DURATION",
    "instance_from_json",
    "load_instance",
    "sample_variant",
    "save_instance",
    "summarize_instance",
    "validate_engine_compatibility",
    "validate_instance",
]

#: Defensive floor for a jittered duration (see module docstring).
MIN_DURATION = 1e-9


def _reject_json_constant(token: str) -> Any:
    raise ValueError("non-finite JSON constant " + repr(token) + " is not allowed")


def instance_from_json(text: str, *, source: str = "<string>") -> Instance:
    """Parse strict JSON text into a validated Instance."""
    if not isinstance(text, str):
        raise ValidationError("instance JSON must be a string", details={"source": source})
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise ValidationError(
            "invalid instance JSON: " + source + ": " + str(exc), details={"source": source}
        ) from exc
    if not isinstance(data, Mapping):
        raise ValidationError("instance JSON must be an object", details={"source": source})
    try:
        return Instance.from_dict(data)
    except ValidationError as exc:
        raise ValidationError(
            "invalid instance in " + source + ": " + exc.message,
            details={**exc.details, "source": source},
        ) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(
            "invalid instance in " + source + ": " + str(exc), details={"source": source}
        ) from exc


def load_instance(path: str | os.PathLike[str]) -> Instance:
    """Load an instance from a UTF-8 JSON file (strict schema)."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    return instance_from_json(text, source=str(target))


def save_instance(instance: Instance, path: str | os.PathLike[str]) -> Path:
    """Write an instance as UTF-8 JSON, atomically, and return the path."""
    if not isinstance(instance, Instance):
        raise ValidationError(
            "save_instance expects an Instance", details={"type": type(instance).__name__}
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        instance.to_dict(), indent=2, ensure_ascii=False, allow_nan=False, sort_keys=False
    ) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _identifier_ok(value: Any) -> bool:
    try:
        validate_identifier(value, field_name="identifier")
    except ValidationError:
        return False
    return True


def _positive_finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def validate_instance(instance: Instance) -> ValidityReport:
    """Re-check the structural invariants of an instance and report every gate.

    Every gate is always present in the returned report (passed or failed), so
    callers can diff reports between instances.
    """
    checks: list[CheckResult] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append(CheckResult(name=name, passed=bool(passed), detail=detail))

    if not isinstance(instance, Instance):
        add("instance_type", False, "expected Instance, got " + type(instance).__name__)
        return ValidityReport.from_checks(tuple(checks))
    add("instance_type", True, "Instance")

    add(
        "instance_id",
        _identifier_ok(instance.instance_id),
        "" if _identifier_ok(instance.instance_id) else "instance_id is not a SimTalk identifier",
    )

    machines = getattr(instance, "machines", ())
    jobs = getattr(instance, "jobs", ())
    add("machines_present", bool(machines), str(len(machines)) + " machines" if machines else "no machines")
    add("jobs_present", bool(jobs), str(len(jobs)) + " jobs" if jobs else "no jobs")

    machine_ids = [getattr(machine, "machine_id", None) for machine in machines]
    duplicate_machines = sorted(str(mid) for mid, count in Counter(machine_ids).items() if count > 1)
    bad_machines = [str(mid) for mid in machine_ids if not _identifier_ok(mid)]
    add(
        "unique_machine_ids",
        not duplicate_machines,
        "" if not duplicate_machines else "duplicates: " + ", ".join(duplicate_machines),
    )
    add(
        "machine_identifiers",
        not bad_machines,
        "" if not bad_machines else "invalid: " + ", ".join(bad_machines),
    )
    bad_capacity = [
        str(getattr(machine, "machine_id", "?"))
        for machine in machines
        if isinstance(getattr(machine, "capacity", None), bool)
        or not isinstance(getattr(machine, "capacity", None), int)
        or getattr(machine, "capacity", 0) < 1
    ]
    add(
        "machine_capacity",
        not bad_capacity,
        "" if not bad_capacity else "invalid capacity: " + ", ".join(bad_capacity),
    )

    job_ids = [getattr(job, "job_id", None) for job in jobs]
    duplicate_jobs = sorted(str(jid) for jid, count in Counter(job_ids).items() if count > 1)
    bad_jobs = [str(jid) for jid in job_ids if not _identifier_ok(jid)]
    add(
        "unique_job_ids",
        not duplicate_jobs,
        "" if not duplicate_jobs else "duplicates: " + ", ".join(duplicate_jobs),
    )
    add("job_identifiers", not bad_jobs, "" if not bad_jobs else "invalid: " + ", ".join(bad_jobs))

    known_machines = {mid for mid in machine_ids if isinstance(mid, str)}
    problems: dict[str, list[str]] = {
        "operations_present": [],
        "operation_identifiers": [],
        "unique_operation_ids": [],
        "operation_index_contiguous": [],
        "operation_job_consistency": [],
        "duration_positive_finite": [],
        "operation_candidates": [],
        "release_times_valid": [],
        "due_dates_valid": [],
        "weights_positive": [],
    }
    seen_ops: dict[str, int] = {}
    for job in jobs:
        job_id = getattr(job, "job_id", "?")
        operations = getattr(job, "operations", ())
        if not operations:
            problems["operations_present"].append(str(job_id))
        for position, operation in enumerate(operations):
            op_id = getattr(operation, "op_id", None)
            if not _identifier_ok(op_id):
                problems["operation_identifiers"].append(str(op_id))
                continue
            seen_ops[op_id] = seen_ops.get(op_id, 0) + 1
            if getattr(operation, "index", None) != position:
                problems["operation_index_contiguous"].append(
                    str(op_id) + " (index " + repr(getattr(operation, "index", None)) + " at position " + str(position) + ")"
                )
            if getattr(operation, "job_id", None) != job_id:
                problems["operation_job_consistency"].append(
                    str(op_id) + " -> " + str(getattr(operation, "job_id", None)) + " != " + str(job_id)
                )
            if not _positive_finite(getattr(operation, "duration", None)):
                problems["duration_positive_finite"].append(str(op_id))
            candidates = getattr(operation, "candidates", ())
            candidates = tuple(candidates) if candidates else ()
            if not candidates:
                problems["operation_candidates"].append(str(op_id) + " (empty)")
            else:
                unknown = [str(mid) for mid in candidates if mid not in known_machines]
                if unknown:
                    problems["operation_candidates"].append(str(op_id) + " unknown " + ",".join(unknown))
                if len(set(candidates)) != len(candidates):
                    problems["operation_candidates"].append(str(op_id) + " duplicate candidates")
        release = getattr(job, "release_time", None)
        if (
            isinstance(release, bool)
            or not isinstance(release, (int, float))
            or not math.isfinite(float(release))
            or float(release) < 0.0
        ):
            problems["release_times_valid"].append(str(job_id))
        due = getattr(job, "due_date", None)
        if due is not None:
            due_ok = (
                not isinstance(due, bool)
                and isinstance(due, (int, float))
                and math.isfinite(float(due))
                and float(due) >= 0.0
                and (not isinstance(release, (int, float)) or isinstance(release, bool) or float(due) >= float(release))
            )
            if not due_ok:
                problems["due_dates_valid"].append(str(job_id))
        weight = getattr(job, "weight", None)
        if not _positive_finite(weight):
            problems["weights_positive"].append(str(job_id))

    duplicates = sorted(op_id for op_id, count in seen_ops.items() if count > 1)
    add("operations_present", not problems["operations_present"], _describe(problems["operations_present"]))
    add("operation_identifiers", not problems["operation_identifiers"], _describe(problems["operation_identifiers"]))
    add("unique_operation_ids", not duplicates, _describe(duplicates))
    add(
        "operation_index_contiguous",
        not problems["operation_index_contiguous"],
        _describe(problems["operation_index_contiguous"]),
    )
    add(
        "operation_job_consistency",
        not problems["operation_job_consistency"],
        _describe(problems["operation_job_consistency"]),
    )
    add(
        "duration_positive_finite",
        not problems["duration_positive_finite"],
        _describe(problems["duration_positive_finite"]),
    )
    add(
        "operation_candidates",
        not problems["operation_candidates"],
        _describe(problems["operation_candidates"]),
    )
    add("release_times_valid", not problems["release_times_valid"], _describe(problems["release_times_valid"]))
    add("due_dates_valid", not problems["due_dates_valid"], _describe(problems["due_dates_valid"]))
    add("weights_positive", not problems["weights_positive"], _describe(problems["weights_positive"]))

    try:
        json.dumps(dict(instance.meta), allow_nan=False, sort_keys=True)
        meta_ok, meta_detail = True, ""
    except (TypeError, ValueError) as exc:
        meta_ok, meta_detail = False, str(exc)
    add("meta_json_serialisable", meta_ok, meta_detail)

    return ValidityReport.from_checks(tuple(checks))


def validate_engine_compatibility(instance: Instance) -> ValidityReport:
    """Report v0.1 engine capabilities (S1 unit capacity, single-candidate routing)."""
    if not isinstance(instance, Instance):
        raise ValidationError("validate_engine_compatibility expects an Instance")
    non_unit = [machine.machine_id for machine in instance.machines if machine.capacity != 1]
    flexible = [operation.op_id for operation in instance.operations() if len(operation.candidates) != 1]
    checks = (
        CheckResult(
            name="unit_machine_capacity",
            passed=not non_unit,
            detail="" if not non_unit else "capacity != 1 for: " + _describe(non_unit),
        ),
        CheckResult(
            name="single_candidate_routing",
            passed=not flexible,
            detail="" if not flexible else "multiple candidates for: " + _describe(flexible),
        ),
    )
    return ValidityReport.from_checks(checks)


def _describe(items: list[Any], limit: int = 5) -> str:
    if not items:
        return ""
    head = ", ".join(str(item) for item in items[:limit])
    if len(items) > limit:
        head += " (+" + str(len(items) - limit) + " more)"
    return head


def sample_variant(instance: Instance, seed: int, jitter: float = 0.0) -> Instance:
    """Return a deterministic duration variant for (instance, seed, jitter).

    Uses random.Random(seed) only; the global random state is untouched.
    jitter == 0 returns the instance unchanged (no draw is consumed).
    """
    if not isinstance(instance, Instance):
        raise ValidationError("sample_variant expects an Instance", details={"type": type(instance).__name__})
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValidationError("seed must be a non-negative integer", details={"seed": repr(seed)})
    jitter_value = validate_number(jitter, field_name="jitter", minimum=0.0)
    if jitter_value >= 1.0:
        raise ValidationError("jitter must be < 1.0", details={"jitter": jitter_value})
    if jitter_value == 0.0:
        return instance
    rng = random.Random(seed)
    durations: dict[str, float] = {}
    for operation in instance.operations():
        factor = rng.uniform(1.0 - jitter_value, 1.0 + jitter_value)
        value = operation.duration * factor
        if not math.isfinite(value) or value <= 0.0:
            value = MIN_DURATION
        durations[operation.op_id] = value
    return instance.with_durations(durations)


def summarize_instance(instance: Instance) -> dict[str, Any]:
    """Return a compact, JSON-serialisable description of an instance."""
    if not isinstance(instance, Instance):
        raise ValidationError("summarize_instance expects an Instance")
    operations = instance.operations()
    durations = [operation.duration for operation in operations]
    due_dates = [job.due_date for job in instance.jobs if job.due_date is not None]
    release_times = [job.release_time for job in instance.jobs]
    return {
        "instance_id": instance.instance_id,
        "schema_version": SCHEMA_VERSION,
        "content_hash": instance.content_hash(),
        "job_count": len(instance.jobs),
        "machine_count": len(instance.machines),
        "operation_count": len(operations),
        "flexible_operation_count": sum(1 for operation in operations if len(operation.candidates) > 1),
        "total_processing_time": float(sum(durations)),
        "min_duration": float(min(durations)),
        "max_duration": float(max(durations)),
        "mean_duration": float(sum(durations) / len(durations)),
        "min_release_time": float(min(release_times)),
        "max_release_time": float(max(release_times)),
        "jobs_with_due_date": len(due_dates),
        "min_due_date": None if not due_dates else float(min(due_dates)),
        "max_due_date": None if not due_dates else float(max(due_dates)),
        "total_weight": float(sum(job.weight for job in instance.jobs)),
        "meta_keys": sorted(str(key) for key in instance.meta),
    }
