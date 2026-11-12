"""Standard scheduling metrics with explicitly documented formulas.

All metrics are computed from a *complete* schedule (every instance operation
appears exactly once); a partial schedule would produce numbers that are not
comparable across runs, so compute_metrics raises ValidationError instead of
silently reporting misleading values.

Formulas
--------
Let J be the jobs, O_j the ordered operations of job j, and C_j the completion
time of the last operation of job j.

makespan          = max(end) over all scheduled operations (0 for an empty
                    schedule). Independent of the observation window.
C_j               = max(end of operations of job j)
flow_j            = C_j - release_time_j
total_flow_time   = sum_j flow_j
mean_flow_time    = total_flow_time / |J|   (0 when there are no jobs)
T_j               = max(0, C_j - due_date_j); 0 when the job has no due date
total_tardiness   = sum_j T_j
weighted_tardiness = sum_j weight_j * T_j
max_tardiness     = max_j T_j (0 when no job is tardy)
tardy_jobs        = #{ j : T_j > TARDINESS_TOLERANCE }   (tolerance 1e-9 so
                    float noise on an exactly-on-time job is not counted)
avg_wip           = (1 / W) * integral_0^W WIP(t) dt, where
                    WIP(t) = #{ j : release_j <= t < C_j } (released but not
                    completed), integrated exactly over the piecewise-constant
                    interval set [0, W]. W is the *observation window*: the
                    horizon when one is supplied, otherwise the makespan.
throughput        = |J| / W   (completed jobs per time unit; 0 when W = 0)
utilization[m]    = busy_m / W, busy_m = sum of durations of operations on m
                    (0 when W = 0). Every machine of the instance appears in
                    the mapping, including unused machines (0.0).

The observation window W = horizon when horizon is not None, otherwise W =
makespan. Using the horizon makes throughput and utilization comparable between
a run that finishes early and one that fills the planning window.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from psbridge.errors import ValidationError
from psbridge.experiment.contracts import Metrics
from psbridge.schema import Instance, ScheduledOperation, validate_number

__all__ = ["TARDINESS_TOLERANCE", "compute_metrics"]

#: Tardiness at or below this value is treated as on time (float noise only).
TARDINESS_TOLERANCE = 1e-9


def _coerce_schedule(schedule: Sequence[ScheduledOperation]) -> tuple[ScheduledOperation, ...]:
    if schedule is None:
        raise ValidationError("schedule must be a sequence of ScheduledOperation")
    items = tuple(schedule)
    for item in items:
        if not isinstance(item, ScheduledOperation):
            raise ValidationError(
                "schedule items must be ScheduledOperation instances",
                details={"type": type(item).__name__},
            )
    return items


def compute_metrics(
    instance: Instance,
    schedule: Sequence[ScheduledOperation],
    horizon: float | None = None,
) -> Metrics:
    """Compute Metrics for a complete schedule (see module docstring)."""
    if not isinstance(instance, Instance):
        raise ValidationError("compute_metrics expects an Instance")
    items = _coerce_schedule(schedule)
    window: float | None = None
    if horizon is not None:
        window = validate_number(horizon, field_name="horizon", strictly_positive=True)

    expected = {operation.op_id for operation in instance.operations()}
    seen: dict[str, int] = {}
    for item in items:
        seen[item.op_id] = seen.get(item.op_id, 0) + 1
    duplicates = sorted(op_id for op_id, count in seen.items() if count > 1)
    if duplicates:
        raise ValidationError(
            "metrics require every operation at most once", details={"duplicates": duplicates}
        )
    unknown = sorted(set(seen) - expected)
    if unknown:
        raise ValidationError(
            "metrics received operations that are not in the instance", details={"unknown": unknown}
        )
    missing = sorted(expected - set(seen))
    if missing:
        raise ValidationError(
            "metrics require a complete schedule", details={"missing": missing}
        )

    ends: dict[str, float] = {item.op_id: item.end for item in items}

    makespan = max((item.end for item in items), default=0.0)
    if not math.isfinite(makespan) or makespan < 0.0:
        raise ValidationError("makespan must be finite and non-negative", details={"makespan": makespan})

    completions: dict[str, float] = {}
    for job in instance.jobs:
        completions[job.job_id] = max(ends[operation.op_id] for operation in job.operations)

    total_flow = 0.0
    total_tardiness = 0.0
    weighted_tardiness = 0.0
    max_tardiness = 0.0
    tardy_jobs = 0
    for job in instance.jobs:
        completion = completions[job.job_id]
        total_flow += completion - float(job.release_time)
        tardiness = 0.0
        if job.due_date is not None:
            tardiness = max(0.0, completion - float(job.due_date))
            if tardiness <= TARDINESS_TOLERANCE:
                # A job finishing exactly on time must not accumulate float noise
                # in total_tardiness while tardy_jobs stays 0.
                tardiness = 0.0
        total_tardiness += tardiness
        weighted_tardiness += float(job.weight) * tardiness
        max_tardiness = max(max_tardiness, tardiness)
        if tardiness > TARDINESS_TOLERANCE:
            tardy_jobs += 1

    job_count = len(instance.jobs)
    observation = makespan if window is None else window

    if observation <= 0.0:
        avg_wip = 0.0
        throughput = 0.0
    else:
        avg_wip = _time_average_wip(instance, completions, observation)
        throughput = job_count / observation

    busy: dict[str, float] = {machine.machine_id: 0.0 for machine in instance.machines}
    for item in items:
        busy[item.machine_id] = busy.get(item.machine_id, 0.0) + item.duration
    if observation <= 0.0:
        utilization = {machine_id: 0.0 for machine_id in busy}
    else:
        utilization = {machine_id: value / observation for machine_id, value in busy.items()}

    return Metrics(
        makespan=float(makespan),
        total_tardiness=float(total_tardiness),
        weighted_tardiness=float(weighted_tardiness),
        max_tardiness=float(max_tardiness),
        tardy_jobs=int(tardy_jobs),
        total_flow_time=float(total_flow),
        mean_flow_time=float(total_flow / job_count) if job_count else 0.0,
        avg_wip=float(avg_wip),
        throughput=float(throughput),
        utilization=utilization,
    )


def _time_average_wip(
    instance: Instance,
    completions: dict[str, float],
    observation: float,
) -> float:
    """Exact integral of the piecewise-constant WIP count over [0, observation].

    By linearity of integration, int_0^W WIP(t) dt = sum_j max(0, min(C_j, W) - max(r_j, 0)),
    which computes the exact residency integral in O(|J|) time without midpoint
    ULP edge cases.
    """
    integral = math.fsum(
        max(0.0, min(completions[job.job_id], observation) - max(float(job.release_time), 0.0))
        for job in instance.jobs
    )
    return integral / observation
