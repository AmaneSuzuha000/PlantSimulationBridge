"""Real-machine Plant Simulation 13 acceptance harness.

Two modes:

* default (dry run): generate the model spec and all SimTalk sources into the
  work directory, compute the Python reference schedule, and print a JSON
  summary without touching COM.
* --execute --confirm-real-machine: connect to Plant Simulation 13, clone the
  configured ``.spp`` model into an isolated work directory, apply the model/
  table/method/control sources, run the simulation, read back
  ``Table_Results``, and verify row-for-row equality against the independent
  Python reference oracle.

Safety rules enforced here:
  * the original model file is never written to (verified by SHA-256 before and
    after; any change is a hard failure);
  * a model clone is mandatory before any generated SimTalk is applied;
  * failures are recorded in full, never swallowed.

Exit codes: 0 passed, 1 verification/bridge failure, 2 usage or missing model.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Allow running this file directly (python scripts/real_machine_smoke.py)
# without installing the package.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from psbridge.config import BridgeConfig
from psbridge.errors import BridgeError
from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.instance import load_instance
from psbridge.experiment.metrics import compute_metrics
from psbridge.experiment.validity import validate_schedule
from psbridge.scheduling.policies import get_policy
from psbridge.scheduling.policy import select_operation
from psbridge.scheduling.reference import reference_schedule
from psbridge.schema import Instance
from psbridge.simtalk.model_spec import RESULT_COLUMNS, RESULT_TABLE, build_model_spec
from psbridge.simtalk.templates import (
    DEFAULT_FRAME_PATH,
    build_model_source,
    control_method_sources,
    dispatch_method_source,
    initialize_tables_source,
    method_sources,
    read_results_source,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_schedule(instance: Instance, policy_name: str):
    """Schedule from the deterministic engine (independent oracle cross-check)."""
    engine = FakeDispatchEngine()
    engine.reset(instance, seed=0)
    policy = get_policy(policy_name)
    while True:
        decision = engine.next_decision()
        if decision is None:
            break
        engine.apply(select_operation(decision, policy).op_id)
    return tuple(engine.schedule())


def prepare_sources(
    instance: Instance,
    policy_name: str,
    work_dir: Path,
    *,
    frame_path: str = DEFAULT_FRAME_PATH,
) -> dict[str, Path]:
    """Generate the SimTalk files that build, initialize, control, and read a model."""
    spec = build_model_spec(instance)
    controls = control_method_sources(frame_path=frame_path)
    sources = {
        "build_model.simtalk": build_model_source(spec, frame_path=frame_path),
        "initialize_tables.simtalk": initialize_tables_source(instance, frame_path=frame_path),
        "dispatch_method.simtalk": dispatch_method_source(policy_name, frame_path=frame_path),
        "on_source_exit.simtalk": controls["OnSourceExit"],
        "on_station_enter.simtalk": controls["OnStationEnter"],
        "on_station_exit.simtalk": controls["OnStationExit"],
        "read_results.simtalk": read_results_source(frame_path=frame_path),
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, source in sources.items():
        path = work_dir / name
        path.write_text(source, encoding="utf-8")
        paths[name] = path
    return paths


def run_dry(
    instance: Instance,
    policy_name: str,
    work_dir: Path,
    *,
    frame_path: str = DEFAULT_FRAME_PATH,
) -> dict[str, Any]:
    paths = prepare_sources(instance, policy_name, work_dir, frame_path=frame_path)
    reference = reference_schedule(instance, get_policy(policy_name))
    report = validate_schedule(instance, reference)
    metrics = compute_metrics(instance, reference) if report.valid else None
    return {
        "mode": "dry-run",
        "status": "DRY_RUN",
        "frame_path": frame_path,
        "instance_id": instance.instance_id,
        "instance_hash": instance.content_hash(),
        "policy": policy_name,
        "simtalk_files": {name: str(path) for name, path in paths.items()},
        "reference_valid": report.valid,
        "reference_makespan": None if metrics is None else metrics.makespan,
        "reference_total_tardiness": None if metrics is None else metrics.total_tardiness,
        "reference_schedule": [
            {
                "op_id": item.op_id,
                "job_id": item.job_id,
                "machine_id": item.machine_id,
                "start": item.start,
                "end": item.end,
            }
            for item in reference
        ],
    }


def _default_end_time(instance: Instance) -> float:
    max_release = max(job.release_time for job in instance.jobs)
    total_duration = sum(op.duration for job in instance.jobs for op in job.operations)
    return max(200.0, max_release + total_duration + 50.0)


def run_execute(
    instance: Instance,
    policy_name: str,
    work_dir: Path,
    model_root: Path,
    model_name: str,
    *,
    frame_path: str | None = None,
) -> int:
    """Execute against a live Plant Simulation instance; returns an exit code."""
    from psbridge.com.win32 import Win32ComBackend
    from psbridge.core.ops import SafeOps

    config = BridgeConfig(work_dir=work_dir, model_root=model_root, backend="win32")
    dirs = config.ensure_work_dirs()
    original = (model_root / model_name).resolve()
    if not original.exists():
        print("model not found: " + str(original), file=sys.stderr)
        return 2

    before_hash = sha256_file(original)
    backend = Win32ComBackend(quit_on_disconnect=True)
    evidence: dict[str, Any] = {
        "mode": "execute",
        "status": "RUNNING",
        "instance_id": instance.instance_id,
        "instance_hash": instance.content_hash(),
        "policy": policy_name,
        "model": str(original),
        "model_hash_before": before_hash,
    }
    exit_code = 0
    try:
        ops = SafeOps(backend, config)
        ops.connect()
        fingerprint = ops.load_model(str(original))
        evidence["loaded_model"] = fingerprint.to_dict()

        clone = ops.clone_model_for_experiment(dirs["models"], tag="acceptance")
        evidence["clone"] = clone.to_dict()
        if not clone.original_unchanged:
            raise RuntimeError("cloning changed the original model file; aborting")

        active_frame = frame_path or backend.default_frame_path
        evidence["frame_path"] = active_frame
        prepare_sources(instance, policy_name, dirs["logs"], frame_path=active_frame)

        spec = build_model_spec(instance)
        backend.execute_simtalk(build_model_source(spec, frame_path=active_frame))
        backend.execute_simtalk(initialize_tables_source(instance, frame_path=active_frame))
        methods = method_sources(policy_name, frame_path=active_frame)
        for method_name, source in methods.items():
            backend.set_method_source(active_frame + "." + method_name, source)

        end_time = _default_end_time(instance)
        backend.reset()
        backend.start(end_time=end_time)
        finished = backend.wait_until_finished(timeout=config.operation_timeout_s)
        status = backend.status()
        evidence["sim_status"] = {
            "running": status.running,
            "finished": finished,
            "sim_time": status.sim_time,
            "end_time": status.end_time,
            "state": status.state,
            "timed_out": not finished,
        }
        if not finished:
            backend.stop()
            evidence["status"] = "FAILED"
            evidence["error"] = {"code": "OPERATION_TIMEOUT", "message": "simulation did not finish in time"}
            exit_code = 1
        else:
            raw_rows = str(backend.evaluate("to_str(" + active_frame + "." + RESULT_TABLE + ".yDim)")).strip()
            row_count = int(float(raw_rows or "0"))
            raw_drain = str(backend.evaluate("to_str(" + active_frame + ".Drain_Output.StatNumIn)")).strip()
            drain_in = int(float(raw_drain or "0"))
            rows = (
                backend.read_table_range(
                    active_frame + "." + RESULT_TABLE,
                    1,
                    1,
                    row_count,
                    len(RESULT_COLUMNS),
                )
                if row_count > 0
                else ()
            )
            evidence["result_rows"] = [list(row) for row in rows]
            evidence["drain_in"] = drain_in

            ps_tuples = [
                (str(row[0]), str(row[1]), str(row[2]), round(float(row[3]), 6), round(float(row[4]), 6))
                for row in rows
            ]
            reference = python_schedule(instance, policy_name)
            ref_tuples = [
                (item.op_id, item.job_id, item.machine_id, round(item.start, 6), round(item.end, 6))
                for item in reference
            ]
            report = validate_schedule(instance, reference)
            metrics = compute_metrics(instance, reference) if report.valid else None
            evidence["reference_valid"] = report.valid
            evidence["reference_makespan"] = None if metrics is None else metrics.makespan
            evidence["reference_total_tardiness"] = None if metrics is None else metrics.total_tardiness

            plant_makespan = max((row[4] for row in ps_tuples), default=0.0)
            evidence["plant_makespan"] = plant_makespan
            makespan_agrees = metrics is not None and abs(plant_makespan - metrics.makespan) <= 1e-6
            exact_match = ps_tuples == ref_tuples and drain_in == len(instance.jobs)
            evidence["makespan_agrees"] = makespan_agrees
            evidence["exact_schedule_match"] = exact_match

            # Verify read_results_source compiles cleanly on the live model.
            backend.execute_simtalk(read_results_source(frame_path=active_frame))

            if not (exact_match and makespan_agrees):
                evidence["status"] = "FAILED"
                evidence["error"] = {
                    "code": "SCHEDULE_MISMATCH",
                    "message": "Plant Simulation schedule differed from the reference schedule",
                    "expected": ref_tuples,
                    "actual": ps_tuples,
                }
                exit_code = 1

        after_hash = sha256_file(original)
        evidence["model_hash_after"] = after_hash
        evidence["original_untouched"] = before_hash == after_hash
        if before_hash != after_hash:
            evidence["status"] = "FAILED"
            evidence["error"] = {"code": "MODEL_MUTATION_DENIED", "message": "the original model file changed"}
            exit_code = 1
        elif exit_code == 0:
            evidence["status"] = "VERIFIED"
    except BridgeError as exc:
        evidence["error"] = exc.to_dict()
        evidence["status"] = "FAILED"
        print(json.dumps(evidence, indent=2, ensure_ascii=False), file=sys.stderr)
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - the harness must record every failure
        evidence["error"] = {"code": "UNEXPECTED", "message": str(exc), "type": type(exc).__name__}
        evidence["status"] = "FAILED"
        print(json.dumps(evidence, indent=2, ensure_ascii=False), file=sys.stderr)
        exit_code = 1
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        backend = None
        gc.collect()

    evidence_path = dirs["results"] / ("real_machine_" + instance.instance_id + "_" + policy_name + ".json")
    evidence_path.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    evidence["evidence_path"] = str(evidence_path)
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="psbridge real-machine acceptance harness")
    parser.add_argument(
        "--instance",
        required=True,
        help="path to an instance JSON file (see psbridge.demo.write_instance)",
    )
    parser.add_argument("--policy", default="fifo")
    parser.add_argument("--work-dir", default=".psbridge-work/acceptance")
    parser.add_argument("--model-root", default=None, help="directory containing the .spp model")
    parser.add_argument("--model-name", default="DeliveryConcept.spp")
    parser.add_argument("--frame-path", default=None, help="override target SimTalk frame path")
    parser.add_argument("--execute", action="store_true", help="actually connect to Plant Simulation")
    parser.add_argument("--confirm-real-machine", action="store_true", help="required with --execute")
    args = parser.parse_args(argv)

    instance = load_instance(Path(args.instance))
    work_dir = Path(args.work_dir)
    if not args.execute:
        dry_frame = args.frame_path or DEFAULT_FRAME_PATH
        print(json.dumps(run_dry(instance, args.policy, work_dir, frame_path=dry_frame), indent=2, ensure_ascii=False))
        return 0
    if not args.confirm_real_machine:
        parser.error("--execute requires --confirm-real-machine")
    if not args.model_root:
        parser.error("--execute requires --model-root")
    return run_execute(
        instance,
        args.policy,
        work_dir,
        Path(args.model_root),
        args.model_name,
        frame_path=args.frame_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
