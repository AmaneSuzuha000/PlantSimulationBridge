"""Run the job-shop generator's own output on the real product.

Executes exactly what ``build_job_shop_statements`` returns (without injecting
any extra hand-written routing SimTalk) and reconciles all counters and
processing work times against independent Python arithmetic for both:

1. Uniform-duration crossover shop (M1=3 s, M2=5 s; 40 parts -> 120 s / 200 s).
2. Heterogeneous-duration crossover shop (Job A: M1=3 s, M2=5 s; Job B: M2=2 s, M1=7 s;
   40 parts -> M1 work 200 s, M2 work 140 s).
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psbridge.com.win32 import Win32ComBackend
from psbridge.simtalk.jobshop import (
    Job,
    Operation,
    build_job_shop_statements,
)

UNITS = 20


def resolve_template() -> Path:
    env = os.environ.get("PSBRIDGE_TEMPLATE")
    if env:
        return Path(env)
    candidates = (
        Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
        Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def as_seconds(value) -> float:
    """StatWorkingTime comes back as h:mm:ss.ssss."""
    parts = str(value).split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    return float(value)


def run_case(template: Path, name: str, jobs: list[Job], *, expected_m1_work: float, expected_m2_work: float) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="psbridge-jobshop-"))
    target = workdir / template.name
    shutil.copyfile(template, target)

    backend = Win32ComBackend(model_root=workdir, quit_on_disconnect=True, prefer_running_instance=False)
    report: dict = {"case": name}
    try:
        backend.connect()
        backend.load_model(str(target))
        root_frame = backend.default_frame_path
        frame = root_frame + ".Frame" if backend.object_exists(root_frame + ".Frame") else root_frame
        controller = root_frame + ".EventController"
        statements = build_job_shop_statements(jobs, frame)
        report["statements_executed"] = len(statements)
        for statement in statements:
            backend.execute_simtalk(statement)

        backend.execute_simtalk(controller + ".End := 2000;")
        backend.execute_simtalk(controller + ".reset;")
        backend.execute_simtalk(controller + ".start;")
        now = time.monotonic()
        deadline = now + 120.0
        grace_deadline = now + 1.0
        saw_running = False
        while True:
            status = backend.status()
            if status.running:
                saw_running = True
            elif status.finished or saw_running or time.monotonic() >= grace_deadline:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise RuntimeError("simulation did not finish within 120.0 s")
            time.sleep(min(0.05, remaining))

        report["srcA_out"] = backend.get_value(frame + ".Src_A", "StatNumOut")
        report["srcB_out"] = backend.get_value(frame + ".Src_B", "StatNumOut")
        report["m1_in"] = backend.get_value(frame + ".M_M1", "StatNumIn")
        report["m1_out"] = backend.get_value(frame + ".M_M1", "StatNumOut")
        report["m2_in"] = backend.get_value(frame + ".M_M2", "StatNumIn")
        report["m2_out"] = backend.get_value(frame + ".M_M2", "StatNumOut")
        report["bufA2_in"] = backend.get_value(frame + ".Buf_A_2", "StatNumIn")
        report["bufA2_out"] = backend.get_value(frame + ".Buf_A_2", "StatNumOut")
        report["bufB2_in"] = backend.get_value(frame + ".Buf_B_2", "StatNumIn")
        report["bufB2_out"] = backend.get_value(frame + ".Buf_B_2", "StatNumOut")
        report["drain_in"] = backend.get_value(frame + ".Drain1", "StatNumIn")
        report["m1_work_s"] = as_seconds(backend.get_value(frame + ".M_M1", "StatWorkingTime"))
        report["m2_work_s"] = as_seconds(backend.get_value(frame + ".M_M2", "StatWorkingTime"))
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        shutil.rmtree(workdir, ignore_errors=True)

    total = UNITS * 2
    checks = {
        "both_sources_released": report.get("srcA_out") == UNITS and report.get("srcB_out") == UNITS,
        "each_machine_saw_every_part": report.get("m1_in") == total and report.get("m2_in") == total,
        "nothing_parked_on_a_machine": report.get("m1_out") == total and report.get("m2_out") == total,
        "crossovers_passed_through_buffers":
            report.get("bufA2_in") == UNITS and report.get("bufA2_out") == UNITS
            and report.get("bufB2_in") == UNITS and report.get("bufB2_out") == UNITS,
        "everything_drained": report.get("drain_in") == total,
        "m1_work_matches_its_operations": abs(report.get("m1_work_s", 0.0) - expected_m1_work) < 1e-6,
        "m2_work_matches_its_operations": abs(report.get("m2_work_s", 0.0) - expected_m2_work) < 1e-6,
    }
    report["expected_drain"] = total
    report["expected_m1_work_s"] = expected_m1_work
    report["expected_m2_work_s"] = expected_m2_work
    report["checks"] = checks
    report["ok"] = all(checks.values())
    return report


def main() -> int:
    template = resolve_template()
    uniform_jobs = [
        Job("A", UNITS, 0, 5, (Operation("M1", 3.0), Operation("M2", 5.0))),
        Job("B", UNITS, 0, 5, (Operation("M2", 5.0), Operation("M1", 3.0))),
    ]
    heterogeneous_jobs = [
        Job("A", UNITS, 0, 5, (Operation("M1", 3.0), Operation("M2", 5.0))),
        Job("B", UNITS, 0, 5, (Operation("M2", 2.0), Operation("M1", 7.0))),
    ]
    reports = [
        run_case(template, "uniform_crossover", uniform_jobs, expected_m1_work=120.0, expected_m2_work=200.0),
        run_case(template, "heterogeneous_crossover", heterogeneous_jobs, expected_m1_work=200.0, expected_m2_work=140.0),
    ]
    overall = {"ok": all(r["ok"] for r in reports), "cases": reports}
    print(json.dumps(overall, ensure_ascii=False, indent=1))
    return 0 if overall["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
