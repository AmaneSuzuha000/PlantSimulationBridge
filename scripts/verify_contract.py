"""Verify the declared class allow-list, attribute specs, and SimTalk conventions against a live Plant Simulation instance.

Exit code 0 means every check passed.
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psbridge.com.win32 import Win32ComBackend
from psbridge.core import validation

_DEFAULT_TEMPLATES = (
    Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
    Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
)


def resolve_template(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_val = os.environ.get("PSBRIDGE_TEMPLATE")
    if env_val:
        candidates.append(Path(env_val))
    candidates.extend(_DEFAULT_TEMPLATES)
    for path in candidates:
        if path.exists():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError("Plant Simulation template .spp not found; set PSBRIDGE_TEMPLATE (checked: " + searched + ")")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify psbridge class/attribute/SimTalk contract on Plant Simulation")
    parser.add_argument("--template", default=None, help="path to base .spp model (or set PSBRIDGE_TEMPLATE)")
    parser.add_argument("--out", default=None, help="optional path to write detailed JSON report")
    args = parser.parse_args(argv)

    template = resolve_template(args.template)
    work = Path(tempfile.mkdtemp(prefix="psbridge-verify-"))
    model = work / template.name
    shutil.copyfile(template, model)
    checks: dict[str, bool] = {}
    detail: dict[str, str] = {}
    backend = Win32ComBackend(model_root=work, quit_on_disconnect=True)
    try:
        backend.connect()
        backend.load_model(str(model))
        frame = backend.default_frame_path

        # -- every declared class must be creatable -------------------------
        for index, class_path in enumerate(sorted(validation.DEVICE_CLASS_ALLOWLIST.values()), start=1):
            name = "V" + str(index)
            key = "class:" + class_path
            try:
                backend.create_object(class_path, name, frame_path=frame, x=20 + index * 40, y=20)
                checks[key] = True
            except Exception as exc:  # noqa: BLE001
                checks[key] = False
                detail[key] = str(exc).splitlines()[-1][:100]

        # -- unsupported classes must fail cleanly --------------------------
        for missing in (
            ".MaterialFlow.Station",
            ".MaterialFlow.Conveyor",
            ".MaterialFlow.ParallelStation",
            ".MaterialFlow.AssemblyStation",
            ".Models.Method",
            ".Models.DataTable",
            ".Resources.Worker",
        ):
            key = "absent:" + missing
            try:
                backend.create_object(missing, "NOPE", frame_path=frame, x=1, y=1)
                checks[key] = False
                detail[key] = "unexpectedly creatable"
            except Exception:  # noqa: BLE001
                checks[key] = True

        # -- every declared attribute must write on its declared class ------
        created = {}
        for index, class_path in enumerate(sorted(validation.CLASS_ATTRIBUTE_SPECS), start=1):
            specs = validation.CLASS_ATTRIBUTE_SPECS[class_path]
            if not specs:
                continue
            name = "W" + str(index)
            try:
                backend.create_object(class_path, name, frame_path=frame, x=20 + index * 40, y=400)
            except Exception as exc:  # noqa: BLE001
                checks["attr-create:" + class_path] = False
                detail["attr-create:" + class_path] = str(exc).splitlines()[-1][:100]
                continue
            created[class_path] = frame + "." + name
            for attribute, spec in sorted(specs.items()):
                key = f"attr:{class_path}.{attribute}"
                try:
                    backend.set_value(created[class_path], attribute, _sample(spec), verify=True)
                    checks[key] = True
                except Exception as exc:  # noqa: BLE001
                    checks[key] = False
                    detail[key] = str(exc).splitlines()[-1][:100]

        # -- the class path must come back as a full path -------------------
        if created:
            first = created[min(created)]
            key = "spelling:to_str_class"
            try:
                value = str(backend.evaluate("to_str(" + first + ".Class)")).strip()
                checks[key] = value.startswith(".")
                detail[key] = value
            except Exception as exc:  # noqa: BLE001
                checks[key] = False
                detail[key] = str(exc).splitlines()[-1][:100]

        # -- a time attribute takes seconds ---------------------------------
        proc = [p for c, p in created.items() if c == ".MaterialFlow.SingleProc"]
        if proc:
            key = "spelling:time_in_seconds"
            try:
                backend.execute_simtalk(proc[0] + ".procTime := 7;")
                checks[key] = str(backend.evaluate("to_str(" + proc[0] + ".procTime)")).startswith("7")
            except Exception as exc:  # noqa: BLE001
                checks[key] = False
                detail[key] = str(exc).splitlines()[-1][:100]

        # -- the discovered controller responds to isRunning ----------------
        key = "spelling:controller_path"
        try:
            running = str(backend.evaluate("to_str(" + backend._controller() + ".isRunning)")).strip().lower()
            checks[key] = running in {"true", "false", "wahr", "falsch"}
            detail[key] = running
        except Exception as exc:  # noqa: BLE001
            checks[key] = False
            detail[key] = str(exc).splitlines()[-1][:100]
    finally:
        with contextlib.suppress(Exception):
            backend.disconnect()
        backend = None
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)

    failed = {k: detail.get(k, "") for k, ok in checks.items() if not ok}
    report = {"ok": not failed, "checks": len(checks), "failed": len(failed), "failures": failed}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"report": report, "detail": detail}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if not failed else 1


def _sample(spec):
    if spec.kind == "time":
        return 5.0
    if spec.kind == "int":
        return max(int(spec.default), 3)
    if spec.kind == "float":
        return 2.5
    if spec.kind == "string":
        return spec.choices[0] if spec.choices else "probe"
    return spec.default


if __name__ == "__main__":
    raise SystemExit(main())
