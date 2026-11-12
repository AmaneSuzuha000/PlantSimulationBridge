"""MCP-ready tool layer: structured JSON in, structured JSON out.

These handlers form the seam wrapped by ``psbridge.mcp.server`` (newline-delimited
JSON-RPC 2.0 over stdio). Every handler:

* validates its arguments strictly (unknown keys, bad types, and non-finite
  numbers are rejected before touching any backend or COM session),
* returns {"ok": true, ...} on success,
* returns {"ok": false, "error": {code, message, details}} on user or bridge
  errors, and never raises for those,
* carries the validity/status of runs instead of hiding failures,
* routes live COM backend calls through a single-threaded STA ``ComExecutor``
  when using ``Win32ComBackend``.

Tool names follow the unified research-platform vocabulary
(``compare_policies``, ``run_experiment``, ``reproduce_run``, and direct
model/simulation controls).
"""

from __future__ import annotations

import atexit
import contextlib
import json
import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from psbridge import __version__
from psbridge.config import BridgeConfig
from psbridge.core import validation
from psbridge.core.ops import SafeOps
from psbridge.errors import BridgeError, ValidationError
from psbridge.experiment.contracts import ExperimentSpec, RunRecord
from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.instance import (
    load_instance,
    summarize_instance,
    validate_engine_compatibility,
)
from psbridge.experiment.instance import (
    validate_instance as validate_instance_report,
)
from psbridge.experiment.metrics import compute_metrics
from psbridge.experiment.runner import ExperimentRunner, schedule_signature
from psbridge.experiment.store import ResultStore, summarize_records
from psbridge.experiment.validity import validate_schedule
from psbridge.scheduling.policies import available_policies, get_policy
from psbridge.scheduling.policy import select_operation
from psbridge.scheduling.reference import reference_schedule
from psbridge.schema import Instance, ScheduledOperation
from psbridge.simtalk.model_spec import build_model_spec
from psbridge.simtalk.templates import (
    DEFAULT_FRAME_PATH,
    build_model_source,
    control_method_sources,
    dispatch_method_source,
    initialize_tables_source,
    read_results_source,
    validate_simtalk_path,
)

DEFAULT_WORK_DIR = ".psbridge-work/tools"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ok(**fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    payload.update(fields)
    return payload


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, BridgeError):
        return {"ok": False, "error": exc.to_dict()}
    return {
        "ok": False,
        "error": {"code": "BRIDGE_ERROR", "message": str(exc), "details": {}},
    }


def _arguments(arguments: Mapping[str, Any] | None, *, allowed: Sequence[str], required: Sequence[str]) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValidationError("tool arguments must be an object")
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValidationError("unknown tool arguments", details={"unknown": unknown, "allowed": list(allowed)})
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ValidationError("missing required tool arguments", details={"missing": missing})
    return dict(arguments)


def _instance_from(value: Any) -> Instance:
    if isinstance(value, Instance):
        return value
    if isinstance(value, Mapping):
        return Instance.from_dict(value)
    if isinstance(value, str):
        return load_instance(value)
    raise ValidationError("instance must be an instance object or a JSON file path", details={"type": type(value).__name__})


def _policy_names(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    policies = arguments.get("policies")
    if policies is None:
        return ("fifo", "spt", "edd")
    if isinstance(policies, (str, bytes)) or not isinstance(policies, Sequence):
        raise ValidationError("policies must be a list of policy names")
    names: list[str] = []
    for item in policies:
        if not isinstance(item, str):
            raise ValidationError("each policy name must be a string", details={"policy": repr(item)})
        names.append(item)
    unknown = [name for name in names if name not in available_policies()]
    if unknown:
        raise ValidationError("unknown policies", details={"unknown": unknown, "available": list(available_policies())})
    return tuple(names)


def _policy_name(arguments: Mapping[str, Any]) -> str:
    policy = arguments.get("policy")
    if not isinstance(policy, str) or policy not in available_policies():
        raise ValidationError("unknown policy", details={"policy": policy, "available": list(available_policies())})
    return policy


def _seeds(arguments: Mapping[str, Any]) -> tuple[int, ...]:
    seeds = arguments.get("seeds", [0])
    if not isinstance(seeds, Sequence) or isinstance(seeds, (str, bytes)):
        raise ValidationError("seeds must be a list of non-negative integers")
    result = []
    for seed in seeds:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValidationError("seeds must be non-negative integers", details={"seed": repr(seed)})
        result.append(seed)
    if not result:
        raise ValidationError("seeds must not be empty")
    return tuple(result)


def _int(arguments: Mapping[str, Any], name: str, default: int | None = None) -> int:
    if name not in arguments:
        if default is None:
            raise ValidationError(name + " is required", details={"field": name})
        return default
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(name + " must be an integer", details={name: repr(value)})
    return value


def _number(arguments: Mapping[str, Any], name: str, default: float) -> float:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(name + " must be a number", details={name: repr(value)})
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(name + " must be finite", details={name: repr(value)})
    return number


def _optional_number(arguments: Mapping[str, Any], name: str) -> float | None:
    value = arguments.get(name)
    if value is None:
        return None
    return _number(arguments, name, 0.0)


def _str(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(name + " must be a non-empty string", details={name: repr(value)})
    if len(value) > 4096 or validation.CONTROL_CHAR_RE.search(value):
        raise ValidationError(name + " contains invalid characters or exceeds maximum length", details={"field": name})
    return value


def _name_or_path(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field_name + " must be a non-empty string", details={field_name: repr(value)})
    if value.startswith("."):
        return validation.validate_object_path(value, field_name=field_name)
    return validation.validate_identifier(value, field_name=field_name)


def _work_dir(arguments: Mapping[str, Any]) -> Path:
    value = arguments.get("work_dir", DEFAULT_WORK_DIR)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("work_dir must be a non-empty string")
    if len(value) > 4096 or validation.CONTROL_CHAR_RE.search(value):
        raise ValidationError("work_dir contains invalid characters or exceeds maximum length")
    return Path(value)


def _compact(record: RunRecord) -> dict[str, Any]:
    metrics = record.metrics
    return {
        "run_id": record.run_id,
        "policy": record.policy,
        "seed": record.seed,
        "replication": record.replication,
        "status": record.status,
        "makespan": None if metrics is None else metrics.makespan,
        "total_tardiness": None if metrics is None else metrics.total_tardiness,
        "tardy_jobs": None if metrics is None else metrics.tardy_jobs,
        "validity_failures": [check.name for check in record.validity.failures()],
        "error": record.error,
    }


def _runner(work_dir: Path) -> tuple[ExperimentRunner, ResultStore, BridgeConfig]:
    config = BridgeConfig(work_dir=work_dir)
    dirs = config.ensure_work_dirs()
    store = ResultStore(dirs["results"])
    return ExperimentRunner(FakeDispatchEngine(), config, store), store, config


def _run_policies(
    instance: Instance,
    policies: Sequence[str],
    seeds: Sequence[int],
    jitter: float,
    horizon: float | None,
    work_dir: Path,
) -> list[RunRecord]:
    runner, _store, _config = _runner(work_dir)
    records: list[RunRecord] = []
    for policy_name in policies:
        spec = ExperimentSpec(
            experiment_id="tool_" + policy_name,
            instance=instance,
            policy=policy_name,
            seeds=tuple(seeds),
            duration_jitter=jitter,
            horizon=horizon,
        )
        records.extend(
            runner.run_spec(spec, [get_policy(policy_name)], replications=1, reference=reference_schedule)
        )
    return records


def _drive_engine(instance: Instance, policy_name: str) -> tuple[ScheduledOperation, ...]:
    engine = FakeDispatchEngine()
    engine.reset(instance, seed=0)
    policy = get_policy(policy_name)
    while True:
        decision = engine.next_decision()
        if decision is None:
            break
        engine.apply(select_operation(decision, policy).op_id)
    return tuple(engine.schedule())


def _schedule_dicts(schedule: Iterable[ScheduledOperation]) -> list[dict[str, Any]]:
    return [
        {"op_id": item.op_id, "job_id": item.job_id, "machine_id": item.machine_id, "start": item.start, "end": item.end}
        for item in schedule
    ]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------
def _bridge_info(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    _arguments(arguments, allowed=(), required=())
    return _ok(
        version=__version__,
        policies=list(available_policies()),
        backends=["fake", "win32"],
        engines=["fake_dispatch"],
        tools=[spec["name"] for spec in TOOLS],
        plant_simulation_verified=True,
        note="Win32 COM automation and generated SimTalk templates are verified against Siemens Plant Simulation 13.",
    )


def _validate_instance_tool(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("instance",), required=("instance",))
    instance = _instance_from(args["instance"])
    report = validate_instance_report(instance)
    compatibility = validate_engine_compatibility(instance)
    payload = _ok(
        instance_id=instance.instance_id,
        instance_hash=instance.content_hash(),
        total_operations=instance.total_operations,
        job_count=len(instance.jobs),
        machine_count=len(instance.machines),
        summary=summarize_instance(instance),
        valid=report.valid,
        checks=[check.to_dict() for check in report.checks],
    )
    if not compatibility.valid:
        payload["engine_compatibility"] = {
            "compatible": False,
            "checks": [check.to_dict() for check in compatibility.checks],
        }
    else:
        payload["engine_compatibility"] = {"compatible": True, "checks": []}
    return payload


def _compare_policies(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("instance", "policies", "seeds", "jitter", "horizon", "work_dir"),
        required=("instance",),
    )
    instance = _instance_from(args["instance"])
    policies = _policy_names(args)
    seeds = _seeds(args)
    jitter = _number(args, "jitter", 0.0)
    horizon = _optional_number(args, "horizon")
    work_dir = _work_dir(args)
    records = _run_policies(instance, policies, seeds, jitter, horizon, work_dir)
    summary = summarize_records(record.to_dict() for record in records)
    return _ok(
        instance_id=instance.instance_id,
        instance_hash=instance.content_hash(),
        policies=list(policies),
        seeds=list(seeds),
        jitter=jitter,
        horizon=horizon,
        run_count=len(records),
        valid_runs=sum(1 for record in records if record.status == "valid"),
        summary=summary,
        runs=[_compact(record) for record in records],
        store=str(Path(work_dir).resolve()),
    )


def _run_experiment(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("instance", "policy", "seeds", "jitter", "horizon", "work_dir"),
        required=("instance", "policy"),
    )
    instance = _instance_from(args["instance"])
    policy = _policy_name(args)
    records = _run_policies(
        instance,
        (policy,),
        _seeds(args),
        _number(args, "jitter", 0.0),
        _optional_number(args, "horizon"),
        _work_dir(args),
    )
    return _ok(
        instance_id=instance.instance_id,
        policy=policy,
        run_count=len(records),
        runs=[_compact(record) for record in records],
        summary=summarize_records(record.to_dict() for record in records),
    )


def _reproduce_run(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("instance", "policy", "seed", "jitter", "replications", "work_dir"),
        required=("instance", "policy"),
    )
    instance = _instance_from(args["instance"])
    policy_name = _policy_name(args)
    seed = _int(args, "seed", 0)
    if seed < 0:
        raise ValidationError("seed must be a non-negative integer", details={"seed": seed})
    jitter = _number(args, "jitter", 0.0)
    replications = _int(args, "replications", 3)
    if replications < 2:
        raise ValidationError("replications must be >= 2 for a reproducibility check")
    work_dir = _work_dir(args)
    runner, _store, _config = _runner(work_dir)
    spec = ExperimentSpec(
        experiment_id="repro_" + policy_name,
        instance=instance,
        policy=policy_name,
        seeds=(seed,),
        duration_jitter=jitter,
    )
    policy = get_policy(policy_name)
    runs = [runner.run_one(spec, seed=seed, replication=index, policy=policy, reference=reference_schedule) for index in range(replications)]
    signatures = [schedule_signature(record.schedule) for record in runs]
    metrics = [None if record.metrics is None else record.metrics.to_dict() for record in runs]
    identical = len({tuple(signature) for signature in signatures}) == 1 and len({json.dumps(item, sort_keys=True) for item in metrics}) == 1
    return _ok(
        instance_id=instance.instance_id,
        policy=policy_name,
        seed=seed,
        jitter=jitter,
        replications=replications,
        identical=identical,
        statuses=[record.status for record in runs],
        signatures=[list(signature) for signature in signatures],
        metrics=metrics,
    )


def _generate_simtalk(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("instance", "policy", "frame_path"), required=("instance", "policy"))
    instance = _instance_from(args["instance"])
    policy = _policy_name(args)
    frame_path = validate_simtalk_path(args["frame_path"]) if "frame_path" in args else DEFAULT_FRAME_PATH
    model_spec = build_model_spec(instance)
    controls = control_method_sources(frame_path=frame_path)
    sources = {
        "build_model.simtalk": build_model_source(model_spec, frame_path=frame_path),
        "initialize_tables.simtalk": initialize_tables_source(instance, frame_path=frame_path),
        "dispatch_method.simtalk": dispatch_method_source(policy, frame_path=frame_path),
        "on_source_exit.simtalk": controls["OnSourceExit"],
        "on_station_enter.simtalk": controls["OnStationEnter"],
        "on_station_exit.simtalk": controls["OnStationExit"],
        "read_results.simtalk": read_results_source(frame_path=frame_path),
    }
    return _ok(verified=True, policy=policy, frame_path=frame_path, sources=sources)


def _schedule_vs_reference(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("instance", "policy"), required=("instance", "policy"))
    instance = _instance_from(args["instance"])
    policy_name = _policy_name(args)
    policy = get_policy(policy_name)
    engine_schedule = _drive_engine(instance, policy_name)
    oracle_schedule = tuple(reference_schedule(instance, policy))
    engine_validity = validate_schedule(instance, engine_schedule)
    oracle_validity = validate_schedule(instance, oracle_schedule)
    engine_metrics = compute_metrics(instance, engine_schedule) if engine_validity.valid else None
    oracle_metrics = compute_metrics(instance, oracle_schedule) if oracle_validity.valid else None
    agreement = schedule_signature(engine_schedule) == schedule_signature(oracle_schedule)
    return _ok(
        instance_id=instance.instance_id,
        policy=policy_name,
        agree=agreement,
        engine_valid=engine_validity.valid,
        reference_valid=oracle_validity.valid,
        engine_schedule=_schedule_dicts(engine_schedule),
        reference_schedule=_schedule_dicts(oracle_schedule),
        engine_metrics=None if engine_metrics is None else engine_metrics.to_dict(),
        reference_metrics=None if oracle_metrics is None else oracle_metrics.to_dict(),
    )


def _list_runs(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("work_dir",), required=())
    work_dir = _work_dir(args)
    store = ResultStore(Path(work_dir) / "results")
    rows = store.read_run_dicts(strict=False)
    compact = [
        {
            "run_id": row.get("run_id"),
            "experiment_id": row.get("experiment_id"),
            "policy": row.get("policy"),
            "seed": row.get("seed"),
            "status": row.get("status"),
            "makespan": None if not row.get("metrics") else row["metrics"].get("makespan"),
        }
        for row in rows
    ]
    return _ok(work_dir=str(Path(work_dir).resolve()), run_count=len(compact), runs=compact, malformed_lines=list(store.malformed_lines))


def _experiment_summary(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("work_dir",), required=())
    work_dir = _work_dir(args)
    store = ResultStore(Path(work_dir) / "results")
    rows = store.read_run_dicts(strict=False)
    return _ok(work_dir=str(Path(work_dir).resolve()), run_count=len(rows), summary=store.summarize(rows))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# live-model tools (one shared session, serialised on a COM STA executor)
# ---------------------------------------------------------------------------
_SESSION: dict[str, Any] = {"ops": None, "executor": None}
_SESSION_LOCK = threading.Lock()


def close_session() -> None:
    """Disconnect the shared live session and close its COM STA executor."""
    with _SESSION_LOCK:
        ops = _SESSION.get("ops")
        executor = _SESSION.get("executor")
        _SESSION["ops"] = None
        _SESSION["executor"] = None
    try:
        if ops is not None and hasattr(ops, "disconnect"):
            ops.disconnect()
    finally:
        if executor is not None:
            executor.close()


def _close_session_atexit() -> None:
    with contextlib.suppress(Exception):
        close_session()


atexit.register(_close_session_atexit)


def _session(arguments: Mapping[str, Any]) -> Any:
    """Return the live SafeOps, connecting on first use.

    One session per process: the COM server is a single application instance,
    so a second SafeOps over the same machine would fight the first over which
    model is loaded. All backend calls are serialized through a dedicated
    single-thread COM STA executor.
    """
    with _SESSION_LOCK:
        ops = _SESSION.get("ops")
        if ops is None:
            stale_executor = _SESSION.get("executor")
            if stale_executor is not None:
                _SESSION["executor"] = None
                stale_executor.close()

            from psbridge.com.executor import ComExecutor
            from psbridge.com.win32 import (
                Win32ComBackend,
                pythoncom_finalizer,
                pythoncom_initializer,
            )

            # A model root is not optional: BridgeConfig refuses backend="win32"
            # without one, and it is the boundary that stops the bridge being
            # pointed at any file on the machine.
            raw_root = arguments.get("model_root")
            if not isinstance(raw_root, str) or not raw_root.strip():
                raise ValidationError(
                    "the first model call must pass model_root (the directory models may be loaded from)",
                    details={"field": "model_root"},
                )
            if len(raw_root) > 4096 or validation.CONTROL_CHAR_RE.search(raw_root):
                raise ValidationError("model_root contains invalid characters or exceeds maximum length")
            config = BridgeConfig(work_dir=_work_dir(arguments), model_root=Path(raw_root), backend="win32")
            executor = ComExecutor(
                name="psbridge-mcp",
                default_timeout=config.com_timeout_s,
                default_operation_timeout=config.operation_timeout_s,
                initializer=pythoncom_initializer(),
                finalizer=pythoncom_finalizer(),
            )
            backend = Win32ComBackend(model_root=config.model_root, max_table_cells=config.max_table_read_cells)
            ops = SafeOps(backend, config, executor=executor, protect_original=False)
            try:
                ops.ensure_connected()
            except BaseException:
                executor.close()
                raise
            _SESSION["ops"] = ops
            _SESSION["executor"] = executor
            return ops

    raw_root = arguments.get("model_root")
    if raw_root is not None:
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValidationError("model_root must be a non-empty string", details={"field": "model_root"})
        if len(raw_root) > 4096 or validation.CONTROL_CHAR_RE.search(raw_root):
            raise ValidationError("model_root contains invalid characters or exceeds maximum length")
        config = getattr(ops, "config", None)
        if config is not None and config.model_root is not None:
            requested = Path(raw_root).expanduser().resolve()
            configured = config.model_root.expanduser().resolve()
            if requested != configured:
                raise ValidationError(
                    "model_root differs from the active session's model_root",
                    details={"requested": str(requested), "configured": str(configured)},
                )
    ops.ensure_connected()
    return ops


def _frame(arguments: Mapping[str, Any]) -> str | None:
    """The caller's frame, or None to let SafeOps derive the loaded model's root.

    Substituting DEFAULT_FRAME_PATH here would defeat that derivation and
    reintroduce the "unknown object" / "identifier Model unknown" failures
    on every model not literally named Model.
    """
    if "frame_path" not in arguments or arguments["frame_path"] is None:
        return None
    value = arguments["frame_path"]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("frame_path must be a non-empty string", details={"frame_path": repr(value)})
    return validation.validate_object_path(value, field_name="frame_path")


def _validate_session_args(arguments: Mapping[str, Any]) -> None:
    """Validate optional session-scoping fields before touching the COM session."""
    if "work_dir" in arguments and arguments["work_dir"] is not None:
        _work_dir(arguments)
    raw_root = arguments.get("model_root")
    if raw_root is not None:
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValidationError("model_root must be a non-empty string", details={"field": "model_root"})
        if len(raw_root) > 4096 or validation.CONTROL_CHAR_RE.search(raw_root):
            raise ValidationError("model_root contains invalid characters or exceeds maximum length")


def _model_status(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "work_dir"), required=())
    _validate_session_args(args)
    ops = _session(args)
    return _ok(backend="win32", connected=True, model_path=ops.model_path)


def _model_open(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "path", "work_dir"), required=("path",))
    _validate_session_args(args)
    path = _str(args, "path")
    ops = _session(args)
    fingerprint = ops.load_model(path)
    return _ok(path=fingerprint.path, sha256=fingerprint.sha256, size=fingerprint.size)


def _model_list_objects(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "frame_path", "work_dir"), required=())
    _validate_session_args(args)
    frame = _frame(args)
    ops = _session(args)
    objects = ops.list_objects(frame)
    return _ok(
        frame=frame,
        count=len(objects),
        objects=[{"path": item.path, "name": item.name, "class_name": item.class_name} for item in objects],
    )


def _model_object_info(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "path", "work_dir"), required=("path",))
    _validate_session_args(args)
    path = validation.validate_object_path(args["path"], field_name="path")
    ops = _session(args)
    info = ops.object_info(path)
    return _ok(path=info.path, name=info.name, class_name=info.class_name)


def _model_create_device(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "class_name", "name", "x", "y", "frame_path", "work_dir"),
        required=("class_name", "name"),
    )
    _validate_session_args(args)
    validation.validate_class_name(args["class_name"])
    name = validation.validate_identifier(args["name"], field_name="name")
    x = _number(args, "x", 0.0)
    y = _number(args, "y", 0.0)
    frame = _frame(args)
    ops = _session(args)
    info = ops.create_device(
        args["class_name"],
        name,
        x=x,
        y=y,
        frame_path=frame,
    )
    return _ok(path=info.path, name=info.name, class_name=info.class_name)


def _model_delete_device(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "target", "confirm", "frame_path", "work_dir"),
        required=("target", "confirm"),
    )
    _validate_session_args(args)
    confirm = args["confirm"]
    if confirm is not True:
        raise ValidationError(
            "delete_device requires confirm=True (destructive operation)",
            details={"confirm": repr(confirm)},
        )
    target = _name_or_path(args["target"], field_name="target")
    frame = _frame(args)
    ops = _session(args)
    ops.delete_device(target, confirm=confirm, frame_path=frame)
    return _ok(target=target, deleted=True)


def _model_connect_objects(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "source", "target", "frame_path", "work_dir"),
        required=("source", "target"),
    )
    _validate_session_args(args)
    source = _name_or_path(args["source"], field_name="source")
    target = _name_or_path(args["target"], field_name="target")
    frame = _frame(args)
    ops = _session(args)
    ops.connect_objects(source, target, frame_path=frame)
    return _ok(source=source, target=target, connected=True)


def _model_get_property(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "path", "attribute", "frame_path", "work_dir"),
        required=("path", "attribute"),
    )
    _validate_session_args(args)
    path = _name_or_path(args["path"], field_name="path")
    attribute = validation.validate_identifier(args["attribute"], field_name="attribute")
    frame = _frame(args)
    ops = _session(args)
    value = ops.get_property(path, attribute, frame_path=frame)
    return _ok(path=path, attribute=attribute, value=value)


def _model_set_property(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "path", "attribute", "value", "frame_path", "work_dir"),
        required=("path", "attribute", "value"),
    )
    _validate_session_args(args)
    path = _name_or_path(args["path"], field_name="path")
    attribute = validation.validate_identifier(args["attribute"], field_name="attribute")
    frame = _frame(args)
    ops = _session(args)
    value = ops.set_property(
        path, attribute, args["value"], frame_path=frame
    )
    return _ok(path=path, attribute=attribute, value=value)


def _simulation_status(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "work_dir"), required=())
    _validate_session_args(args)
    ops = _session(args)
    status = ops.simulation_status()
    return _ok(
        running=status.running,
        finished=status.finished,
        state=status.state,
        sim_time=status.sim_time,
        end_time=status.end_time,
    )


def _simulation_start(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "work_dir", "end_time", "statistics_start"),
        required=(),
    )
    _validate_session_args(args)
    end_time = _optional_number(args, "end_time")
    statistics_start = 0.0 if "statistics_start" not in args else _optional_number(args, "statistics_start")
    if statistics_start is not None and statistics_start < 0.0:
        raise ValidationError("statistics_start must be >= 0", details={"statistics_start": statistics_start})
    ops = _session(args)
    ops.start_simulation(
        end_time=end_time,
        statistics_start=statistics_start,
    )
    # Measured: start is asynchronous, so this reports that it began, not that
    # anything has been simulated yet.
    return _ok(started=True, returns_immediately=True)


def _simulation_stop(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "work_dir"), required=())
    _validate_session_args(args)
    ops = _session(args)
    ops.stop_simulation()
    status = ops.simulation_status()
    return _ok(stopped=True, state=status.state, sim_time=status.sim_time)


def _simulation_reset(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(arguments, allowed=("model_root", "work_dir"), required=())
    _validate_session_args(args)
    ops = _session(args)
    ops.reset_simulation()
    return _ok(reset=True)


def _simulation_run(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("model_root", "work_dir", "end_time", "timeout",
                 "statistics_start"),
        required=(),
    )
    _validate_session_args(args)
    end_time = _optional_number(args, "end_time")
    timeout = _number(args, "timeout", 300.0)
    if timeout <= 0.0:
        raise ValidationError("timeout must be positive", details={"timeout": timeout})
    statistics_start = 0.0 if "statistics_start" not in args else _optional_number(args, "statistics_start")
    if statistics_start is not None and statistics_start < 0.0:
        raise ValidationError("statistics_start must be >= 0", details={"statistics_start": statistics_start})
    ops = _session(args)
    status = ops.run_simulation(
        end_time=end_time,
        timeout=timeout,
        statistics_start=statistics_start,
    )
    return _ok(
        finished=status.finished,
        state=status.state,
        sim_time=status.sim_time,
        end_time=status.end_time,
    )


def _method_create(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("name", "source", "frame_path", "model_root", "work_dir"),
        required=("name", "source"),
    )
    _validate_session_args(args)
    name = validation.validate_identifier(args["name"], field_name="name")
    source = validation.validate_simtalk_source(args["source"], field_name="source")
    frame = _frame(args)
    ops = _session(args)
    path = ops.create_method(name, source, frame_path=frame)
    return _ok(path=path, name=name)


def _method_set_source(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("path", "source", "model_root", "work_dir"),
        required=("path", "source"),
    )
    _validate_session_args(args)
    path = validation.validate_object_handle(args["path"], field_name="path")
    source = validation.validate_simtalk_source(args["source"], field_name="source")
    ops = _session(args)
    written = ops.set_method_source(path, source)
    return _ok(path=path, length=len(written))


def _method_get_source(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("path", "model_root", "work_dir"),
        required=("path",),
    )
    _validate_session_args(args)
    path = validation.validate_object_handle(args["path"], field_name="path")
    ops = _session(args)
    source = ops.get_method_source(path)
    return _ok(path=path, source=source)


def _object_set_control(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("path", "control", "method", "model_root", "work_dir"),
        required=("path", "control", "method"),
    )
    _validate_session_args(args)
    path = validation.validate_object_path(args["path"], field_name="path")
    control = validation.validate_control_name(args["control"])
    method = validation.validate_object_handle(args["method"], field_name="method")
    ops = _session(args)
    value = ops.set_object_control(path, control, method)
    return _ok(path=path, control=control, method=value)


def _mu_create_attribute(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("name", "kind", "model_root", "work_dir"),
        required=("name", "kind"),
    )
    _validate_session_args(args)
    name = validation.validate_identifier(args["name"], field_name="name")
    kind = validation.validate_mu_attribute_kind(args["kind"])
    ops = _session(args)
    ops.create_mu_attribute(name, kind)
    return _ok(name=name, kind=kind)


def _simulation_set_source(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("path", "units", "interval", "start", "frame_path", "model_root", "work_dir"),
        required=("path", "units", "interval"),
    )
    _validate_session_args(args)
    path = validation.validate_object_path(args["path"], field_name="path")
    units = _int(args, "units")
    if units < 1:
        raise ValidationError("units must be a positive integer", details={"units": units})
    interval = _number(args, "interval", 1.0)
    if interval <= 0.0:
        raise ValidationError("interval must be a positive number of seconds", details={"interval": interval})
    start = _number(args, "start", 0.0)
    if start < 0.0:
        raise ValidationError("start must be zero or a positive number of seconds", details={"start": start})
    _ = _frame(args)
    ops = _session(args)
    ops.configure_source(
        path,
        units=units,
        interval=interval,
        start=start,
    )
    return _ok(path=path, units=units)


def _model_read_counters(arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    args = _arguments(
        arguments,
        allowed=("objects", "frame_path", "model_root", "work_dir"),
        required=("objects",),
    )
    _validate_session_args(args)
    names = args["objects"]
    if not isinstance(names, (list, tuple)) or not names:
        raise ValidationError("objects must be a non-empty list of names")
    identifiers = [validation.validate_identifier(name, field_name="object name") for name in names]
    frame = _frame(args)
    ops = _session(args)
    counters = ops.read_counters(identifiers, frame_path=frame)
    return _ok(counters=counters)


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "bridge_info",
        "description": "Return bridge version, available policies/backends, and verification status.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": _bridge_info,
    },
    {
        "name": "validate_instance",
        "description": "Validate a scheduling instance and report engine compatibility and summary statistics.",
        "input_schema": {
            "type": "object",
            "properties": {"instance": {"type": ["object", "string"]}},
            "required": ["instance"],
            "additionalProperties": False,
        },
        "handler": _validate_instance_tool,
    },
    {
        "name": "compare_policies",
        "description": "Run FIFO/SPT/EDD (or a subset) over seeds and compare standard metrics with reference cross-check.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance": {"type": ["object", "string"]},
                "policies": {"type": "array", "items": {"type": "string"}},
                "seeds": {"type": "array", "items": {"type": "integer"}},
                "jitter": {"type": "number"},
                "horizon": {"type": "number"},
                "work_dir": {"type": "string"},
            },
            "required": ["instance"],
            "additionalProperties": False,
        },
        "handler": _compare_policies,
    },
    {
        "name": "run_experiment",
        "description": "Run one dispatch policy over one or more seeds and persist reproducible records.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance": {"type": ["object", "string"]},
                "policy": {"type": "string"},
                "seeds": {"type": "array", "items": {"type": "integer"}},
                "jitter": {"type": "number"},
                "horizon": {"type": "number"},
                "work_dir": {"type": "string"},
            },
            "required": ["instance", "policy"],
            "additionalProperties": False,
        },
        "handler": _run_experiment,
    },
    {
        "name": "reproduce_run",
        "description": "Run the same instance/policy/seed several times and verify identical schedules and metrics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance": {"type": ["object", "string"]},
                "policy": {"type": "string"},
                "seed": {"type": "integer"},
                "jitter": {"type": "number"},
                "replications": {"type": "integer"},
                "work_dir": {"type": "string"},
            },
            "required": ["instance", "policy"],
            "additionalProperties": False,
        },
        "handler": _reproduce_run,
    },
    {
        "name": "generate_simtalk",
        "description": "Generate verified model, table, dispatch, control, and result SimTalk templates for Plant Simulation 13.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instance": {"type": ["object", "string"]},
                "policy": {"type": "string"},
                "frame_path": {"type": "string"},
            },
            "required": ["instance", "policy"],
            "additionalProperties": False,
        },
        "handler": _generate_simtalk,
    },
    {
        "name": "schedule_vs_reference",
        "description": "Compare the dispatch engine schedule with the independent reference oracle for one policy.",
        "input_schema": {
            "type": "object",
            "properties": {"instance": {"type": ["object", "string"]}, "policy": {"type": "string"}},
            "required": ["instance", "policy"],
            "additionalProperties": False,
        },
        "handler": _schedule_vs_reference,
    },
    {
        "name": "list_runs",
        "description": "List stored run records under a work directory.",
        "input_schema": {
            "type": "object",
            "properties": {"work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _list_runs,
    },
    {
        "name": "experiment_summary",
        "description": "Aggregate stored run records (counts by status/policy, metric aggregates, validity failures).",
        "input_schema": {
            "type": "object",
            "properties": {"work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _experiment_summary,
    },
    {
        "name": "model_status",
        "description": "Report whether a model session is connected and which model it tracks.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _model_status,
    },
    {
        "name": "model_open",
        "description": "Load a .spp model into the running Plant Simulation and fingerprint the file.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": _model_open,
    },
    {
        "name": "model_list_objects",
        "description": "List the objects in a frame (name and real class path per object).",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _model_list_objects,
    },
    {
        "name": "model_object_info",
        "description": "Return one object's name and real class path.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": _model_object_info,
    },
    {
        "name": "model_create_device",
        "description": "Create a device from the measured class allow-list and return its path.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "class_name": {"type": "string"}, "name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["class_name", "name"],
            "additionalProperties": False,
        },
        "handler": _model_create_device,
    },
    {
        "name": "model_delete_device",
        "description": "Delete one object. Requires confirm=true; never deletes the frame.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "target": {"type": "string"}, "confirm": {"type": "boolean"}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["target", "confirm"],
            "additionalProperties": False,
        },
        "handler": _model_delete_device,
    },
    {
        "name": "model_connect_objects",
        "description": "Connect a source to a target with a MaterialFlow connector.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "source": {"type": "string"}, "target": {"type": "string"}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["source", "target"],
            "additionalProperties": False,
        },
        "handler": _model_connect_objects,
    },
    {
        "name": "model_get_property",
        "description": "Read one writable attribute of an object.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "path": {"type": "string"}, "attribute": {"type": "string"}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path", "attribute"],
            "additionalProperties": False,
        },
        "handler": _model_get_property,
    },
    {
        "name": "model_set_property",
        "description": "Write one attribute declared writable for that object class, then read it back.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "path": {"type": "string"}, "attribute": {"type": "string"}, "value": {}, "frame_path": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path", "attribute", "value"],
            "additionalProperties": False,
        },
        "handler": _model_set_property,
    },
    {
        "name": "simulation_status",
        "description": "Report whether the EventController runs, the simulated time and the end period.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _simulation_status,
    },
    {
        "name": "simulation_start",
        "description": "Start the EventController. Returns immediately (measured: start is asynchronous).",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}, "end_time": {"type": "number"}, "statistics_start": {"type": ["number", "null"]}},
            "additionalProperties": False,
        },
        "handler": _simulation_start,
    },
    {
        "name": "simulation_stop",
        "description": "Stop the EventController and report where it stopped.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _simulation_stop,
    },
    {
        "name": "simulation_reset",
        "description": "Reset the EventController without touching the model.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "additionalProperties": False,
        },
        "handler": _simulation_reset,
    },
    {
        "name": "simulation_run",
        "description": "Start and block until the controller stops, polling isRunning.",
        "input_schema": {
            "type": "object",
            "properties": {"model_root": {"type": "string"}, "work_dir": {"type": "string"}, "end_time": {"type": "number"}, "timeout": {"type": "number"}, "statistics_start": {"type": ["number", "null"]}},
            "additionalProperties": False,
        },
        "handler": _simulation_run,
    },
    {
        "name": "method_create",
        "description": "Create a Method object in a frame and write its SimTalk source.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "source": {"type": "string"}, "frame_path": {"type": "string"}, "model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["name", "source"],
            "additionalProperties": False,
        },
        "handler": _method_create,
    },
    {
        "name": "method_set_source",
        "description": "Replace a Method's SimTalk source; the write is read back and compared.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "source": {"type": "string"}, "model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path", "source"],
            "additionalProperties": False,
        },
        "handler": _method_set_source,
    },
    {
        "name": "method_get_source",
        "description": "Read a Method's SimTalk source.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": _method_get_source,
    },
    {
        "name": "object_set_control",
        "description": "Attach a Method as an object's EntranceCtrl, ExitCtrl or InitCtrl.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "control": {"type": "string"}, "method": {"type": "string"}, "model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["path", "control", "method"],
            "additionalProperties": False,
        },
        "handler": _object_set_control,
    },
    {
        "name": "mu_create_attribute",
        "description": "Declare a custom attribute on the MU class (e.g. OpIndex, integer).",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"}, "model_root": {"type": "string"}, "work_dir": {"type": "string"}},
            "required": ["name", "kind"],
            "additionalProperties": False,
        },
        "handler": _mu_create_attribute,
    },
    {
        "name": "simulation_set_source",
        "description": "Make a Source emit a fixed number of parts, starting at a given time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "units": {"type": "integer"},
                "interval": {"type": "number"},
                "start": {"type": "number"},
                "frame_path": {"type": "string"},
                "model_root": {"type": "string"},
                "work_dir": {"type": "string"},
            },
            "required": ["path", "units", "interval"],
            "additionalProperties": False,
        },
        "handler": _simulation_set_source,
    },
    {
        "name": "model_read_counters",
        "description": "Read StatNumIn, StatNumOut and StatWorkingTime for named objects in a frame.",
        "input_schema": {
            "type": "object",
            "properties": {
            "objects": {"type": "array", "items": {"type": "string"}},
            "frame_path": {"type": "string"},
            "model_root": {"type": "string"},
            "work_dir": {"type": "string"}
            },
            "required": ["objects"],
            "additionalProperties": False,
        },
        "handler": _model_read_counters,
    },
)

_TOOL_BY_NAME: dict[str, dict[str, Any]] = {spec["name"]: spec for spec in TOOLS}


def tool_names() -> tuple[str, ...]:
    return tuple(spec["name"] for spec in TOOLS)


def call_tool(name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Dispatch one tool call; never raises for user/bridge errors."""
    spec = _TOOL_BY_NAME.get(name)
    if spec is None:
        return {
            "ok": False,
            "error": {"code": "UNKNOWN_TOOL", "message": "unknown tool", "details": {"name": name, "available": list(tool_names())}},
        }
    try:
        return spec["handler"](arguments)
    except BridgeError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001 - tool boundary must not leak exceptions
        return _error(exc)
