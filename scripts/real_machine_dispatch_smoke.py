"""Real-machine smoke test for the generated dispatch code.

Runs the exact strings produced by psbridge.simtalk.dispatch against Plant
Simulation 13 and checks that the numbers reconcile. This is deliberately not a
unit test: the point is to prove the generated SimTalk compiles and behaves.

Usage:  python scripts/real_machine_dispatch_smoke.py [--keep]
"""
from __future__ import annotations

import argparse
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
from psbridge.simtalk.dispatch import (
    ScheduledJob,
    build_exit_control_program,
    build_schedule_source_statements,
)

_DEFAULT_TEMPLATES = (
    Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
    Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
)


def resolve_template() -> Path:
    candidates: list[Path] = []
    env_val = os.environ.get("PSBRIDGE_TEMPLATE")
    if env_val:
        candidates.append(Path(env_val))
    candidates.extend(_DEFAULT_TEMPLATES)
    for path in candidates:
        if path.exists():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError("Plant Simulation template .spp not found; set PSBRIDGE_TEMPLATE (checked: " + searched + ")")


JOBS = (ScheduledJob("JobA", 0, 6), ScheduledJob("JobB", 200, 6), ScheduledJob("JobC", 400, 6))
P1_TIME = 4
P2_TIME = 9
HORIZON = 1000


def _seconds(value: object) -> float:
    """SimTalk times come back as "h:mm:ss.ssss"; return seconds."""
    text = str(value).strip()
    if ":" not in text:
        return round(float(text), 4)
    hours, minutes, seconds = text.split(":")[:3]
    return round(float(hours) * 3600 + float(minutes) * 60 + float(seconds), 4)


def create_named(backend: Win32ComBackend, frame: str, simtalk_class: str, name: str) -> str:
    """Create an object and give it a known name.

    createObject takes no name, so the object first appears under a generated
    one. Address the node that was just appended and rename it; after that
    <frame>.<name> resolves (measured).
    """
    backend.execute_simtalk(simtalk_class + ".createObject(" + frame + ", 120, 240);")
    backend.execute_simtalk(frame + ".node(" + frame + ".NumNodes).Name := " + json.dumps(name) + ";")
    return frame + "." + name


def wait_until_idle(
    backend: Win32ComBackend,
    timeout: float = 120.0,
    *,
    startup_grace: float = 1.0,
) -> float:
    started = time.monotonic()
    deadline = started + timeout
    grace_deadline = started + min(startup_grace, timeout)
    saw_running = False
    while True:
        status = backend.status()
        if status.running:
            saw_running = True
        elif status.finished or saw_running or time.monotonic() >= grace_deadline:
            return time.monotonic() - started
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        time.sleep(min(0.05, remaining))
    raise RuntimeError("simulation did not finish within " + str(timeout) + " s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the cloned model")
    args = parser.parse_args(argv)

    template = resolve_template()
    workdir = Path(tempfile.mkdtemp(prefix="psbridge-dispatch-"))
    target = workdir / template.name
    shutil.copyfile(template, target)

    backend = Win32ComBackend(prefer_running_instance=False)
    report: dict = {"model": str(template)}
    try:
        backend.connect()
        backend.load_model(str(target))
        frame = backend.default_frame_path
        report["frame"] = frame

        method = backend.create_method_object("ExitRoute", frame)

        for job in JOBS:
            for statement in build_schedule_source_statements(job, frame):
                backend.execute_simtalk(statement)

        p1 = create_named(backend, frame, ".MaterialFlow.SingleProc", "Machine1")
        p2 = create_named(backend, frame, ".MaterialFlow.SingleProc", "Machine2")
        drain = create_named(backend, frame, ".MaterialFlow.Drain", "Sink")
        for job in JOBS:
            backend.connect_objects(frame + "." + job.name, p1)
            backend.connect_objects(frame + "." + job.name, p2)
        backend.connect_objects(p1, drain)
        backend.connect_objects(p2, drain)

        program = build_exit_control_program((p1, p2))
        report["program"] = program
        backend.execute_simtalk(method + ".Program := " + json.dumps(program) + ";")
        for job in JOBS:
            backend.execute_simtalk(frame + "." + job.name + ".ExitCtrl := " + method + ";")

        backend.execute_simtalk(p1 + ".ProcTime := " + str(P1_TIME) + ";")
        backend.execute_simtalk(p2 + ".ProcTime := " + str(P2_TIME) + ";")
        backend.execute_simtalk(frame + ".EventController.End := " + str(HORIZON) + ";")
        backend.execute_simtalk(frame + ".EventController.reset;")
        backend.execute_simtalk(frame + ".EventController.start;")
        report["seconds_to_finish"] = round(wait_until_idle(backend), 2)

        produced = 0
        for job in JOBS:
            produced += int(str(backend.get_value(frame + "." + job.name, "StatNumOut")))
        p1_in = int(str(backend.get_value(p1, "StatNumIn")))
        p2_in = int(str(backend.get_value(p2, "StatNumIn")))
        p1_work = _seconds(backend.get_value(p1, "StatWorkingTime"))
        p2_work = _seconds(backend.get_value(p2, "StatWorkingTime"))
        report.update(
            {
                "produced": produced,
                "expected": sum(job.units for job in JOBS),
                "p1_in": p1_in,
                "p2_in": p2_in,
                "routed_total": p1_in + p2_in,
                "drain_in": int(str(backend.get_value(drain, "StatNumIn"))),
                "p1_work_s": p1_work,
                "p2_work_s": p2_work,
                "p1_occupation": backend.get_value(p1, "StatRelativeOccupation"),
                "p2_occupation": backend.get_value(p2, "StatRelativeOccupation"),
            }
        )
        report["checks"] = {
            "produced_matches_schedule": produced == report["expected"],
            "everything_routed": report["routed_total"] == report["expected"],
            "everything_drained": report["drain_in"] == report["expected"],
            "p1_work_matches_units": p1_work == p1_in * P1_TIME,
            "p2_work_matches_units": p2_work == p2_in * P2_TIME,
        }
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    report["ok"] = bool(report.get("checks")) and all(report["checks"].values())
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())