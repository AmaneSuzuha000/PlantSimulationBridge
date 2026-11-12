"""Closed-loop demo: FIFO/SPT/EDD -> dispatch engine -> metrics -> independent cross-check.

This is the smallest meaningful research loop defined in the audit plan:

  1. build one instance (or load it from JSON),
  2. run every dispatch policy on it, with several seeds and duration jitter,
  3. validate the schedule and compute standard metrics,
  4. compare the engine schedule against the independent reference oracle,
  5. persist run records and print a comparison table.

It runs without Plant Simulation. The Plant Simulation integration reuses the
same experiment layer; only the engine changes (SimTalk decision engine).

Usage:
    python -m psbridge.demo
    python -m psbridge.demo --work-dir .psbridge-work/demo --seeds 0 1 2 --jitter 0.05
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path

from psbridge.config import BridgeConfig
from psbridge.experiment.contracts import ExperimentSpec, RunRecord
from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.runner import ExperimentRunner
from psbridge.experiment.store import ResultStore
from psbridge.scheduling.policies import available_policies, get_policy
from psbridge.scheduling.reference import reference_schedule
from psbridge.schema import Instance, Job, Machine, Operation


def build_demo_instance() -> Instance:
    """A small 5-job x 3-operation job shop on 4 machines with due dates."""
    machines = (Machine("M1"), Machine("M2"), Machine("M3"), Machine("M4"))
    routes = (
        ("M1", "M2", "M3"),
        ("M2", "M3", "M4"),
        ("M1", "M3", "M4"),
        ("M2", "M1", "M4"),
        ("M3", "M1", "M2"),
    )
    durations = (
        (4.0, 3.0, 5.0),
        (6.0, 2.0, 3.0),
        (3.0, 4.0, 2.0),
        (5.0, 6.0, 4.0),
        (2.0, 3.0, 6.0),
    )
    release_times = (0.0, 0.0, 2.0, 4.0, 6.0)
    due_dates = (18.0, 20.0, 22.0, 30.0, 34.0)
    jobs = []
    for index, (route, route_times, release, due) in enumerate(zip(routes, durations, release_times, due_dates)):
        job_id = "J" + str(index + 1)
        operations = tuple(
            Operation(
                op_id=job_id + "_O" + str(position + 1),
                job_id=job_id,
                index=position,
                candidates=(machine_id,),
                duration=duration,
            )
            for position, (machine_id, duration) in enumerate(zip(route, route_times))
        )
        jobs.append(Job(job_id=job_id, operations=operations, release_time=release, due_date=due))
    return Instance(instance_id="demo_5x3", jobs=tuple(jobs), machines=machines, meta={"source": "psbridge.demo"})


def run_comparison(
    work_dir: Path,
    *,
    seeds: Sequence[int] = (0, 1, 2),
    jitter: float = 0.05,
    policies: Sequence[str] | None = None,
) -> tuple[Instance, list[RunRecord]]:
    """Run all requested policies on the demo instance and persist the records."""
    instance = build_demo_instance()
    selected = tuple(policies) if policies is not None else ("fifo", "spt", "edd")
    config = BridgeConfig(work_dir=work_dir)
    dirs = config.ensure_work_dirs()
    store = ResultStore(dirs["results"])
    runner = ExperimentRunner(FakeDispatchEngine(), config, store)
    records: list[RunRecord] = []
    for policy_name in selected:
        spec = ExperimentSpec(
            experiment_id="demo_" + policy_name,
            instance=instance,
            policy=policy_name,
            seeds=tuple(seeds),
            duration_jitter=jitter,
        )
        policy = get_policy(policy_name)
        produced = runner.run_spec(spec, [policy], replications=1, reference=reference_schedule)
        records.extend(produced)
    return instance, records


def summarize(records: Iterable[RunRecord]) -> list[dict]:
    """Aggregate valid runs per policy; invalid/failed counts are never hidden."""
    grouped: dict[str, list[RunRecord]] = {}
    for record in records:
        grouped.setdefault(record.policy, []).append(record)
    summary = []
    for policy_name in sorted(grouped):
        runs = grouped[policy_name]
        valid = [run for run in runs if run.status == "valid" and run.metrics is not None]
        summary.append(
            {
                "policy": policy_name,
                "runs": len(runs),
                "valid": len(valid),
                "invalid": sum(1 for run in runs if run.status == "invalid"),
                "failed": sum(1 for run in runs if run.status == "failed"),
                "makespan_mean": statistics.fmean(run.metrics.makespan for run in valid) if valid else None,
                "makespan_min": min(run.metrics.makespan for run in valid) if valid else None,
                "makespan_max": max(run.metrics.makespan for run in valid) if valid else None,
                "tardiness_mean": statistics.fmean(run.metrics.total_tardiness for run in valid) if valid else None,
                "tardy_jobs_mean": statistics.fmean(run.metrics.tardy_jobs for run in valid) if valid else None,
                "wip_mean": statistics.fmean(run.metrics.avg_wip for run in valid) if valid else None,
            }
        )
    return summary


def write_instance(instance: Instance, path: Path) -> Path:
    """Persist an instance as strict JSON (usable by the acceptance harness)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(instance.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def print_table(summary: Sequence[dict]) -> None:
    print("policy   runs  valid  invalid  failed   makespan(mean/min/max)   tardiness(mean)  tardy_jobs  wip")
    for row in summary:
        if row["valid"]:
            makespan = "{:.2f}/{:.2f}/{:.2f}".format(row["makespan_mean"], row["makespan_min"], row["makespan_max"])
            tardiness = "{:.2f}".format(row["tardiness_mean"])
            tardy = "{:.2f}".format(row["tardy_jobs_mean"])
            wip = "{:.2f}".format(row["wip_mean"])
        else:
            makespan = tardiness = tardy = wip = "-"
        print(
            "{:<8} {:>5} {:>6} {:>8} {:>7}   {:<23} {:>16} {:>11} {:>4}".format(
                row["policy"], row["runs"], row["valid"], row["invalid"], row["failed"], makespan, tardiness, tardy, wip
            )
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="psbridge closed-loop demo (no Plant Simulation required)")
    parser.add_argument("--work-dir", default=".psbridge-work/demo")
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    parser.add_argument("--jitter", type=float, default=0.05)
    parser.add_argument("--policies", nargs="+", default=["fifo", "spt", "edd"])
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(argv)

    available = set(available_policies())
    unknown = [name for name in args.policies if name not in available]
    if unknown:
        parser.error("unknown policies: " + ", ".join(unknown) + "; available: " + ", ".join(sorted(available)))

    instance, records = run_comparison(
        Path(args.work_dir), seeds=tuple(args.seeds), jitter=args.jitter, policies=tuple(args.policies)
    )
    summary = summarize(records)
    if args.json:
        print(json.dumps({"instance": instance.to_dict(), "summary": summary}, indent=2, ensure_ascii=False))
        return 0

    print("instance: " + instance.instance_id + " | jobs=" + str(len(instance.jobs)) + " | machines=" + str(len(instance.machines)) + " | operations=" + str(instance.total_operations))
    print("seeds: " + ", ".join(str(seed) for seed in args.seeds) + " | duration jitter: " + str(args.jitter))
    print()
    print_table(summary)
    invalid = sum(row["invalid"] + row["failed"] for row in summary)
    print()
    if invalid:
        print("WARNING: " + str(invalid) + " run(s) were not valid; inspect the stored records before using them as evidence.")
        return 1
    print("all runs valid; records stored under " + str(Path(args.work_dir).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
