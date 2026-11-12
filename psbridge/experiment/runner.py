"""Batch experiment runner: the only place that turns a spec into evidence.

Guarantees
----------
* run_one() never raises for a runtime failure. Engine errors, reference
  oracle errors and validation problems are recorded as RunRecord rows with
  status "failed" or "invalid"; only programming errors of the runner's own
  callers (e.g. passing something that is not an ExperimentSpec) would raise.
* A run is "valid" only when the produced schedule passes every validity gate
  (psbridge.experiment.validity) and, if a reference oracle is supplied, the
  independently produced schedule matches exactly. Only valid runs carry
  metrics; invalid and failed runs have metrics=None, so an aggregate can
  never accidentally average a broken run into the results.
* run_id is a pure function of (experiment_id, base instance hash, policy,
  seed, replication), so repeating a batch reproduces the same identifiers and
  a results file can be joined across runs.

Interpretations pinned down
---------------------------
R1. Seeded duration variants come from psbridge.experiment.instance.sample_variant
    with the run's seed and the spec's duration_jitter; the variant's content
    hash is what Provenance.instance_hash records (not the base instance hash),
    because two seeds are genuinely different instances.
R2. The cross-check compares (op_id, machine_id, start, end) per operation.
    op_id and machine_id must match exactly; times are compared with
    math.isclose(rel_tol=abs_tol=1e-9) because two independent implementations
    may accumulate floats in a different order. Sorting is by op_id, so the
    comparison is order-insensitive.
R3. A reference oracle that raises is recorded as status "failed" with
    "reference oracle failed: ..." rather than "invalid": an oracle crash means
    we could not verify the run, which is not the same claim as "the schedule
    is wrong".
R4. If the reference and the engine disagree by having different operation
    sets (size differs), the run is invalid with CROSS_CHECK_MISMATCH and the
    differing op ids in the message.
R5. The provenance timestamp is the only non-deterministic field; it is
    recorded at run time in UTC (datetime.now(timezone.utc).isoformat()).
"""

from __future__ import annotations

import inspect
import math
import platform
import re
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

import psbridge
from psbridge.errors import BridgeError, EngineUnsupported, ValidationError
from psbridge.experiment.contracts import (
    RUN_STATUS_FAILED,
    RUN_STATUS_INVALID,
    RUN_STATUS_VALID,
    CheckResult,
    ExperimentSpec,
    Provenance,
    RunRecord,
    ValidityReport,
)
from psbridge.experiment.instance import sample_variant, validate_instance
from psbridge.experiment.metrics import compute_metrics
from psbridge.experiment.store import ResultStore
from psbridge.experiment.validity import TOLERANCE, validate_schedule
from psbridge.scheduling.policy import select_operation
from psbridge.schema import Instance, ScheduledOperation

__all__ = ["CROSS_CHECK_MISMATCH", "ExperimentRunner", "run_spec_records", "schedule_signature"]

CROSS_CHECK_MISMATCH = "CROSS_CHECK_MISMATCH"

_ENGINE_STEP_LIMIT = 10_000


def schedule_signature(schedule: Sequence[ScheduledOperation]) -> tuple[tuple[str, str, float, float], ...]:
    """Canonical comparison form used by the cross-check (sorted by op_id)."""
    return tuple(
        sorted(
            ((item.op_id, item.machine_id, float(item.start), float(item.end)) for item in schedule),
            key=lambda entry: (entry[0], entry[1], entry[2], entry[3]),
        )
    )


def _signatures_match(
    left: Sequence[ScheduledOperation], right: Sequence[ScheduledOperation]
) -> tuple[bool, str]:
    left_sig = schedule_signature(left)
    right_sig = schedule_signature(right)
    if len(left_sig) != len(right_sig):
        left_ids = {entry[0] for entry in left_sig}
        right_ids = {entry[0] for entry in right_sig}
        missing = sorted(left_ids - right_ids)
        extra = sorted(right_ids - left_ids)
        return False, "operation sets differ (missing=" + repr(missing[:10]) + ", extra=" + repr(extra[:10]) + ")"
    problems: list[str] = []
    for engine_entry, reference_entry in zip(left_sig, right_sig):
        op_id, machine_id, start, end = engine_entry
        ref_op, ref_machine, ref_start, ref_end = reference_entry
        if op_id != ref_op:
            problems.append("op set ordering mismatch at " + op_id + " vs " + ref_op)
            continue
        if machine_id != ref_machine:
            problems.append(op_id + " machine " + machine_id + " != " + ref_machine)
            continue
        if not math.isclose(start, ref_start, rel_tol=TOLERANCE, abs_tol=TOLERANCE):
            problems.append(op_id + " start " + repr(start) + " != " + repr(ref_start))
        elif not math.isclose(end, ref_end, rel_tol=TOLERANCE, abs_tol=TOLERANCE):
            problems.append(op_id + " end " + repr(end) + " != " + repr(ref_end))
        if len(problems) >= 5:
            break
    if problems:
        return False, "; ".join(problems[:5])
    return True, ""


def _engine_kind(engine: Any) -> str:
    try:
        kind = engine.kind
    except Exception:  # noqa: BLE001 - provenance must never break a run
        return type(engine).__name__
    return str(kind)


def _apply_engine(engine: Any, op_id: str, machine_id: str) -> Any:
    """Call engine.apply, tolerating engines that only accept op_id (protocol allows)."""
    apply_fn = engine.apply
    try:
        signature = inspect.signature(apply_fn)
    except (TypeError, ValueError):
        return apply_fn(op_id, machine_id)
    accepts_machine = False
    for parameter in signature.parameters.values():
        if parameter.name == "machine_id":
            accepts_machine = True
            break
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            accepts_machine = True
            break
    if accepts_machine:
        return apply_fn(op_id, machine_id)
    return apply_fn(op_id)


def _resolve_policy(policy: Any, policy_name: str) -> Any:
    """Return a dispatch policy object from an object, a name or None."""
    if policy is not None and not isinstance(policy, str):
        if not hasattr(policy, "preference"):
            raise ValidationError(
                "policy object must provide preference(operation)",
                details={"type": type(policy).__name__},
            )
        return policy
    name = policy if isinstance(policy, str) else policy_name
    try:
        from psbridge.scheduling.policies import (
            get_policy,  # lazy: another module owns it
        )
    except Exception as exc:  # - missing sibling module is not a crash
        raise ValidationError(
            "no policy object supplied and psbridge.scheduling.policies is unavailable: " + str(exc),
            details={"policy": name},
        ) from exc
    return get_policy(name)


def _policy_name(policy: Any, fallback: str) -> str:
    if policy is None:
        return fallback
    if isinstance(policy, str):
        return policy
    name = getattr(policy, "name", None)
    return str(name) if isinstance(name, str) and name else fallback


def _reference_call(
    reference: Callable[..., Any],
    instance: Instance,
    policy: Any,
    horizon: float | None,
) -> Any:
    """Call the reference oracle, forwarding horizon only if it accepts one."""
    try:
        signature = inspect.signature(reference)
    except (TypeError, ValueError):
        return reference(instance, policy, horizon=horizon)
    accepts_horizon = False
    for parameter in signature.parameters.values():
        if parameter.name == "horizon" or parameter.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_horizon = True
            break
    if accepts_horizon:
        return reference(instance, policy, horizon=horizon)
    return reference(instance, policy)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_run_id_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120]


class ExperimentRunner:
    """Drives a DispatchEngine over experiment specs and records RunRecords."""

    def __init__(self, engine: Any, config: Any, store: ResultStore | None = None) -> None:
        if not hasattr(engine, "reset") or not hasattr(engine, "next_decision"):
            raise ValidationError(
                "engine must implement the DispatchEngine protocol",
                details={"type": type(engine).__name__},
            )
        self.engine = engine
        self.config = config
        self.store = store
        self._backend = str(getattr(config, "backend", "fake"))

    # -- single run -----------------------------------------------------
    def run_one(
        self,
        spec: ExperimentSpec,
        seed: int,
        replication: int = 0,
        policy: Any = None,
        reference: Callable[..., Any] | None = None,
    ) -> RunRecord:
        """Run one (spec, seed, replication, policy) and return its RunRecord.

        Never raises for runtime failures; see the module docstring.
        """
        return self._run_one_impl(
            spec,
            seed=seed,
            replication=replication,
            policy=policy,
            reference=reference,
            prepared=None,
        )

    def _run_one_impl(
        self,
        spec: ExperimentSpec,
        *,
        seed: int,
        replication: int = 0,
        policy: Any = None,
        reference: Callable[..., Any] | None = None,
        prepared: tuple[str, Instance, str, ValidityReport] | None = None,
    ) -> RunRecord:
        if not isinstance(spec, ExperimentSpec):
            raise ValidationError(
                "run_one expects an ExperimentSpec", details={"type": type(spec).__name__}
            )
        if isinstance(replication, bool) or not isinstance(replication, int) or replication < 0:
            raise ValidationError(
                "replication must be a non-negative integer", details={"replication": repr(replication)}
            )
        base_instance = spec.instance
        base_hash = prepared[0] if prepared is not None else base_instance.content_hash()
        policy_label = _policy_name(policy, spec.policy)
        run_id = self._run_id(spec.experiment_id, base_hash, policy_label, seed, replication)
        valid_seed = isinstance(seed, int) and not isinstance(seed, bool)
        seed_value = seed if valid_seed else 0

        instance = base_instance
        provenance = None
        try:
            if not valid_seed or seed < 0:
                raise ValidationError("seed must be a non-negative integer", details={"seed": repr(seed)})
            if prepared is not None:
                _, instance, instance_hash, instance_report = prepared
            else:
                instance = sample_variant(base_instance, seed, spec.duration_jitter)
                instance_hash = instance.content_hash()
                instance_report = None
            provenance = self._provenance(instance_hash, policy_label, seed, spec.duration_jitter)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            return self._failed_record(
                run_id=run_id,
                spec=spec,
                policy_label=policy_label,
                seed=seed_value,
                replication=replication,
                instance_hash=base_hash,
                error=self._describe_error(exc),
            )

        try:
            if instance_report is None:
                instance_report = validate_instance(instance)
            if not instance_report.valid:
                return self._invalid_record(
                    run_id=run_id,
                    spec=spec,
                    policy_label=policy_label,
                    seed=seed_value,
                    replication=replication,
                    instance_hash=instance_hash,
                    validity=instance_report,
                    schedule=(),
                    error="INSTANCE_INVALID: " + self._failure_names(instance_report),
                    provenance=provenance,
                )

            resolved_policy = _resolve_policy(policy, spec.policy)
            schedule = self._simulate(instance, seed_value, resolved_policy, spec.horizon)
            validity = validate_schedule(instance, schedule, spec.horizon)
            if not validity.valid:
                return self._invalid_record(
                    run_id=run_id,
                    spec=spec,
                    policy_label=policy_label,
                    seed=seed_value,
                    replication=replication,
                    instance_hash=instance_hash,
                    validity=validity,
                    schedule=schedule,
                    error="VALIDITY_FAILED: " + self._failure_names(validity),
                    provenance=provenance,
                )

            if reference is not None:
                try:
                    reference_schedule = _reference_call(reference, instance, resolved_policy, spec.horizon)
                except Exception as exc:  # noqa: BLE001 - see R3
                    return self._failed_record(
                        run_id=run_id,
                        spec=spec,
                        policy_label=policy_label,
                        seed=seed_value,
                        replication=replication,
                        instance_hash=instance_hash,
                        error="reference oracle failed: " + self._describe_error(exc),
                        schedule=schedule,
                        validity=validity,
                        provenance=provenance,
                    )
                reference_tuple = tuple(reference_schedule)
                matched, detail = _signatures_match(schedule, reference_tuple)
                if not matched:
                    return self._invalid_record(
                        run_id=run_id,
                        spec=spec,
                        policy_label=policy_label,
                        seed=seed_value,
                        replication=replication,
                        instance_hash=instance_hash,
                        validity=ValidityReport.from_checks(
                            validity.checks
                            + (
                                CheckResult(name="cross_check_matches_reference", passed=False, detail=detail),
                            )
                        ),
                        schedule=schedule,
                        error=CROSS_CHECK_MISMATCH + ": " + detail,
                        provenance=provenance,
                    )
                validity = ValidityReport.from_checks(
                    validity.checks
                    + (CheckResult(name="cross_check_matches_reference", passed=True, detail=""),)
                )

            metrics = compute_metrics(instance, schedule, spec.horizon)
            record = RunRecord(
                run_id=run_id,
                experiment_id=spec.experiment_id,
                instance_id=instance.instance_id,
                instance_hash=instance_hash,
                policy=policy_label,
                seed=seed_value,
                replication=replication,
                status=RUN_STATUS_VALID,
                metrics=metrics,
                schedule=tuple(schedule),
                validity=validity,
                provenance=provenance,
                backend=self._backend,
                engine_kind=_engine_kind(self.engine),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001 - runtime failure -> failed record
            return self._failed_record(
                run_id=run_id,
                spec=spec,
                policy_label=policy_label,
                seed=seed_value,
                replication=replication,
                instance_hash=instance_hash,
                error=self._describe_error(exc),
            )

        self._store(record)
        return record

    # -- batches --------------------------------------------------------
    def run_spec(
        self,
        spec: ExperimentSpec,
        policies: Sequence[Any],
        replications: int = 1,
        reference: Callable[..., Any] | None = None,
    ) -> list[RunRecord]:
        """Run every (seed, policy, replication) combination of one spec."""
        if not isinstance(spec, ExperimentSpec):
            raise ValidationError("run_spec expects an ExperimentSpec")
        if isinstance(replications, bool) or not isinstance(replications, int) or replications < 1:
            raise ValidationError("replications must be a positive integer", details={"replications": repr(replications)})
        policy_list = list(policies)
        if not policy_list:
            raise ValidationError("run_spec needs at least one policy")
        records: list[RunRecord] = []
        base_hash = spec.instance.content_hash()
        custom_run_one = type(self).run_one is not ExperimentRunner.run_one
        for seed in spec.seeds:
            prepared: tuple[str, Instance, str, ValidityReport] | None = None
            if not custom_run_one and isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0:
                try:
                    variant = sample_variant(spec.instance, seed, spec.duration_jitter)
                    variant_hash = variant.content_hash()
                    variant_report = validate_instance(variant)
                    prepared = (base_hash, variant, variant_hash, variant_report)
                except Exception:  # noqa: BLE001 - let _run_one_impl record the failure
                    prepared = None
            for policy in policy_list:
                for replication in range(replications):
                    if custom_run_one:
                        records.append(
                            self.run_one(
                                spec,
                                seed=seed,
                                replication=replication,
                                policy=policy,
                                reference=reference,
                            )
                        )
                    else:
                        records.append(
                            self._run_one_impl(
                                spec,
                                seed=seed,
                                replication=replication,
                                policy=policy,
                                reference=reference,
                                prepared=prepared,
                            )
                        )
        if self.store is not None:
            self.store.write_summary(spec.experiment_id, records)
        return records

    def run_batch(
        self,
        specs: Sequence[ExperimentSpec],
        policies: Sequence[Any],
        replications: int = 1,
        reference: Callable[..., Any] | None = None,
    ) -> list[RunRecord]:
        """Run several specs and return all records in (spec, seed, policy) order."""
        spec_list = list(specs)
        if not spec_list:
            raise ValidationError("run_batch needs at least one spec")
        records: list[RunRecord] = []
        for spec in spec_list:
            records.extend(self.run_spec(spec, policies, replications=replications, reference=reference))
        return records

    # -- internals ------------------------------------------------------
    def _simulate(
        self,
        instance: Instance,
        seed: int,
        policy: Any,
        horizon: float | None,
    ) -> tuple[ScheduledOperation, ...]:
        engine = self.engine
        engine.reset(instance, seed, horizon=horizon)
        steps = 0
        limit = max(instance.total_operations * 4 + 64, _ENGINE_STEP_LIMIT)
        while not engine.is_done():
            decision = engine.next_decision()
            if decision is None:
                break
            steps += 1
            if steps > limit:
                raise EngineUnsupported(
                    "engine did not terminate within the step budget",
                    details={"steps": steps, "limit": limit},
                )
            choice = select_operation(decision, policy)
            _apply_engine(engine, choice.op_id, decision.machine_id)
        return tuple(engine.schedule())

    def _run_id(
        self,
        experiment_id: str,
        instance_hash: str,
        policy_label: str,
        seed: int,
        replication: int,
    ) -> str:
        seed_part = str(seed) if isinstance(seed, int) and not isinstance(seed, bool) else "invalid"
        return "-".join(
            (
                _safe_run_id_part(experiment_id),
                instance_hash[:12],
                _safe_run_id_part(policy_label),
                "s" + seed_part,
                "r" + str(replication),
            )
        )

    def _provenance(
        self,
        instance_hash: str,
        policy_label: str,
        seed: int,
        duration_jitter: float,
    ) -> Provenance:
        return Provenance(
            bridge_version=str(getattr(psbridge, "__version__", "unknown")),
            instance_hash=instance_hash,
            policy=policy_label,
            seed=seed,
            duration_jitter=float(duration_jitter),
            backend=self._backend,
            engine_kind=_engine_kind(self.engine),
            timestamp=_utc_now(),
            python_version=sys.version.split()[0],
            platform=platform.platform(),
            model_hash=None,
        )

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        if isinstance(exc, BridgeError):
            return exc.code + ": " + exc.message
        return type(exc).__name__ + ": " + str(exc)

    @staticmethod
    def _failure_names(report: ValidityReport) -> str:
        names = [check.name for check in report.failures()]
        return ", ".join(names) if names else "no checks ran"

    def _store(self, record: RunRecord) -> None:
        if self.store is not None:
            self.store.append_run(record)

    def _invalid_record(
        self,
        *,
        run_id: str,
        spec: ExperimentSpec,
        policy_label: str,
        seed: int,
        replication: int,
        instance_hash: str,
        validity: ValidityReport,
        schedule: Sequence[ScheduledOperation],
        error: str,
        provenance: Provenance | None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=run_id,
            experiment_id=spec.experiment_id,
            instance_id=spec.instance.instance_id,
            instance_hash=instance_hash,
            policy=policy_label,
            seed=seed,
            replication=replication,
            status=RUN_STATUS_INVALID,
            metrics=None,
            schedule=tuple(schedule),
            validity=validity,
            provenance=provenance if provenance is not None else self._fallback_provenance(instance_hash, policy_label, seed, spec.duration_jitter),
            backend=self._backend,
            engine_kind=_engine_kind(self.engine),
            error=error,
        )
        self._store(record)
        return record

    def _failed_record(
        self,
        *,
        run_id: str,
        spec: ExperimentSpec,
        policy_label: str,
        seed: int,
        replication: int,
        instance_hash: str,
        error: str,
        schedule: Sequence[ScheduledOperation] = (),
        validity: ValidityReport | None = None,
        provenance: Provenance | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=run_id,
            experiment_id=spec.experiment_id,
            instance_id=spec.instance.instance_id,
            instance_hash=instance_hash,
            policy=policy_label,
            seed=seed,
            replication=replication,
            status=RUN_STATUS_FAILED,
            metrics=None,
            schedule=tuple(schedule),
            validity=validity if validity is not None else ValidityReport.from_checks(()),
            provenance=provenance if provenance is not None else self._fallback_provenance(instance_hash, policy_label, seed, spec.duration_jitter),
            backend=self._backend,
            engine_kind=_engine_kind(self.engine),
            error=error,
        )
        self._store(record)
        return record

    def _fallback_provenance(
        self, instance_hash: str, policy_label: str, seed: int, duration_jitter: float
    ) -> Provenance:
        try:
            return self._provenance(instance_hash, policy_label, seed, duration_jitter)
        except Exception:  # noqa: BLE001 - last-resort provenance
            return Provenance(
                bridge_version=str(getattr(psbridge, "__version__", "unknown")),
                instance_hash=instance_hash,
                policy=policy_label,
                seed=seed,
                duration_jitter=0.0,
                backend=self._backend,
                engine_kind="unknown",
                timestamp=_utc_now(),
                python_version=sys.version.split()[0],
                platform=platform.platform(),
                model_hash=None,
            )


def run_spec_records(
    engine: Any,
    config: Any,
    spec: ExperimentSpec,
    policies: Sequence[Any],
    replications: int = 1,
    reference: Callable[..., Any] | None = None,
    store: ResultStore | None = None,
) -> list[RunRecord]:
    """Convenience wrapper: build a runner and run one spec."""
    return ExperimentRunner(engine, config, store=store).run_spec(
        spec, policies, replications=replications, reference=reference
    )
