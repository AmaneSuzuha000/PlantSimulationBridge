"""Data contracts for experiments and run records.

These records are the only thing the runner writes to disk. A run that fails
a validity gate is still recorded, but with status 'invalid' or 'failed', so
invalid experiments can never be silently counted as evidence.

Status semantics
----------------
valid   : all gates passed; may be used as experiment evidence.
invalid : simulation finished but a postcondition failed (precedence, overlap,
          horizon truncation, cross-check mismatch, ...).
failed  : the run raised an exception before or during simulation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from psbridge.errors import ExperimentInvalid
from psbridge.schema import (
    Instance,
    ScheduledOperation,
    validate_identifier,
    validate_number,
)

RUN_STATUS_VALID = "valid"
RUN_STATUS_INVALID = "invalid"
RUN_STATUS_FAILED = "failed"
RUN_STATUSES = (RUN_STATUS_VALID, RUN_STATUS_INVALID, RUN_STATUS_FAILED)


@dataclass(frozen=True)
class ExperimentSpec:
    """One experiment: an instance, one dispatch policy, and seeds."""

    experiment_id: str
    instance: Instance
    policy: str
    seeds: tuple[int, ...] = (0,)
    duration_jitter: float = 0.0
    horizon: float | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        validate_identifier(self.experiment_id, field_name="experiment_id")
        validate_identifier(self.policy, field_name="policy")
        if not isinstance(self.seeds, tuple):
            object.__setattr__(self, "seeds", tuple(self.seeds))
        if not self.seeds:
            raise ExperimentInvalid("experiment needs at least one seed")
        for seed in self.seeds:
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                raise ExperimentInvalid("seeds must be non-negative integers", details={"seed": repr(seed)})
        jitter = validate_number(self.duration_jitter, field_name="duration_jitter", minimum=0.0)
        if jitter >= 1.0:
            raise ExperimentInvalid("duration_jitter must be < 1.0", details={"duration_jitter": jitter})
        object.__setattr__(self, "duration_jitter", jitter)
        if self.horizon is not None:
            object.__setattr__(self, "horizon", validate_number(self.horizon, field_name="horizon", strictly_positive=True))
        if not isinstance(self.notes, str):
            raise ExperimentInvalid("notes must be a string")

    @property
    def run_count(self) -> int:
        return len(self.seeds)


@dataclass(frozen=True)
class CheckResult:
    """One validity gate outcome."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ValidityReport:
    """Collection of gates; a report is valid only if every gate passed."""

    checks: tuple[CheckResult, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "checks": [check.to_dict() for check in self.checks]}

    @classmethod
    def from_checks(cls, checks: tuple[CheckResult, ...]) -> ValidityReport:
        return cls(checks=tuple(checks))


@dataclass(frozen=True)
class Metrics:
    """Standard scheduling metrics computed from a finished schedule."""

    makespan: float
    total_tardiness: float
    weighted_tardiness: float
    max_tardiness: float
    tardy_jobs: int
    total_flow_time: float
    mean_flow_time: float
    avg_wip: float
    throughput: float
    utilization: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "makespan",
            "total_tardiness",
            "weighted_tardiness",
            "max_tardiness",
            "total_flow_time",
            "mean_flow_time",
            "avg_wip",
            "throughput",
        ):
            object.__setattr__(self, name, validate_number(getattr(self, name), field_name=name, minimum=0.0))
        if isinstance(self.tardy_jobs, bool) or not isinstance(self.tardy_jobs, int) or self.tardy_jobs < 0:
            raise ExperimentInvalid("tardy_jobs must be a non-negative integer")
        utilization = {str(key): validate_number(value, field_name="utilization", minimum=0.0) for key, value in dict(self.utilization).items()}
        object.__setattr__(self, "utilization", utilization)

    def to_dict(self) -> dict[str, Any]:
        return {
            "makespan": self.makespan,
            "total_tardiness": self.total_tardiness,
            "weighted_tardiness": self.weighted_tardiness,
            "max_tardiness": self.max_tardiness,
            "tardy_jobs": self.tardy_jobs,
            "total_flow_time": self.total_flow_time,
            "mean_flow_time": self.mean_flow_time,
            "avg_wip": self.avg_wip,
            "throughput": self.throughput,
            "utilization": dict(self.utilization),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Metrics:
        return cls(
            makespan=data["makespan"],
            total_tardiness=data["total_tardiness"],
            weighted_tardiness=data["weighted_tardiness"],
            max_tardiness=data["max_tardiness"],
            tardy_jobs=data["tardy_jobs"],
            total_flow_time=data["total_flow_time"],
            mean_flow_time=data["mean_flow_time"],
            avg_wip=data["avg_wip"],
            throughput=data["throughput"],
            utilization=dict(data.get("utilization", {})),
        )


@dataclass(frozen=True)
class Provenance:
    """Everything needed to check that a result is reproducible."""

    bridge_version: str
    instance_hash: str
    policy: str
    seed: int
    duration_jitter: float
    backend: str
    engine_kind: str
    timestamp: str
    python_version: str
    platform: str
    model_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_version": self.bridge_version,
            "instance_hash": self.instance_hash,
            "policy": self.policy,
            "seed": self.seed,
            "duration_jitter": self.duration_jitter,
            "backend": self.backend,
            "engine_kind": self.engine_kind,
            "timestamp": self.timestamp,
            "python_version": self.python_version,
            "platform": self.platform,
            "model_hash": self.model_hash,
        }


@dataclass(frozen=True)
class RunRecord:
    """One reproducible run; the unit of stored evidence."""

    run_id: str
    experiment_id: str
    instance_id: str
    instance_hash: str
    policy: str
    seed: int
    replication: int
    status: str
    metrics: Metrics | None
    schedule: tuple[ScheduledOperation, ...]
    validity: ValidityReport
    provenance: Provenance
    backend: str
    engine_kind: str
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ExperimentInvalid("unknown run status", details={"status": self.status})
        if not isinstance(self.schedule, tuple):
            object.__setattr__(self, "schedule", tuple(self.schedule))
        if isinstance(self.replication, bool) or not isinstance(self.replication, int) or self.replication < 0:
            raise ExperimentInvalid("replication must be a non-negative integer")
        if self.status == RUN_STATUS_VALID:
            if self.metrics is None or not self.validity.valid:
                raise ExperimentInvalid("valid run requires metrics and a passing validity report")
            if self.error is not None:
                raise ExperimentInvalid("valid run cannot carry an error")
        if self.status in (RUN_STATUS_INVALID, RUN_STATUS_FAILED) and self.metrics is not None and self.error is not None:
            raise ExperimentInvalid("invalid/failed run should not report metrics with an error")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "instance_id": self.instance_id,
            "instance_hash": self.instance_hash,
            "policy": self.policy,
            "seed": self.seed,
            "replication": self.replication,
            "status": self.status,
            "metrics": None if self.metrics is None else self.metrics.to_dict(),
            "schedule": [
                {
                    "op_id": item.op_id,
                    "job_id": item.job_id,
                    "machine_id": item.machine_id,
                    "start": item.start,
                    "end": item.end,
                }
                for item in self.schedule
            ],
            "validity": self.validity.to_dict(),
            "provenance": self.provenance.to_dict(),
            "backend": self.backend,
            "engine_kind": self.engine_kind,
            "error": self.error,
        }
