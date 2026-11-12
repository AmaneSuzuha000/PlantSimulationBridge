"""Exercise every MCP tool against live Siemens Plant Simulation and the offline engine.

Calls each registered tool through ``psbridge.tools.call_tool``, checks every
response, and exits non-zero if any tool call fails.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psbridge import tools
from psbridge.demo import build_demo_instance, write_instance

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
    parser = argparse.ArgumentParser(description="End-to-end smoke test of all psbridge MCP tools")
    parser.add_argument("--template", default=None, help="path to base .spp model (or set PSBRIDGE_TEMPLATE)")
    parser.add_argument("--out", default=None, help="optional path to write full JSON report")
    args = parser.parse_args(argv)

    template = resolve_template(args.template)
    wd = Path(tempfile.mkdtemp(prefix="psbridge-all-"))
    model = wd / template.name
    shutil.copyfile(template, model)
    root = str(wd)
    tools.close_session()
    calls: list[dict] = []

    experiment_tools = {
        "bridge_info",
        "list_runs",
        "experiment_summary",
        "validate_instance",
        "compare_policies",
        "run_experiment",
        "reproduce_run",
        "generate_simtalk",
        "schedule_vs_reference",
    }

    def call(tool: str, **kwargs):
        if tool not in experiment_tools:
            kwargs.setdefault("model_root", root)
            kwargs.setdefault("work_dir", str(wd / "tool_work"))
        elif tool in {"list_runs", "experiment_summary", "compare_policies", "run_experiment", "reproduce_run"}:
            kwargs.setdefault("work_dir", str(wd / "exp_work"))
        try:
            result = tools.call_tool(tool, kwargs)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": {"code": "RAISED", "message": str(exc)[:160]}}
        calls.append(
            {
                "tool": tool,
                "ok": bool(result.get("ok")),
                "error": result.get("error"),
                "keys": sorted(k for k in result if k != "ok"),
            }
        )
        return result

    try:
        call("bridge_info")
        call("list_runs")
        call("experiment_summary")
        instance = write_instance(build_demo_instance(), wd / "demo_instance.json")
        call("validate_instance", instance=str(instance))
        call("compare_policies", instance=str(instance), policies=["fifo", "spt", "edd"])
        call("run_experiment", instance=str(instance), policy="fifo")
        call("reproduce_run", instance=str(instance), policy="fifo")
        call("generate_simtalk", instance=str(instance), policy="fifo")
        call("schedule_vs_reference", instance=str(instance), policy="fifo")

        call("model_status")
        call("model_open", path=str(model))
        call("model_list_objects")
        src1 = call("model_create_device", class_name="Source", name="Src1", x=80, y=100)
        m1 = call("model_create_device", class_name="SingleProc", name="M1", x=240, y=100)
        d1 = call("model_create_device", class_name="Drain", name="D1", x=400, y=100)
        src1_path = src1.get("path", "Src1")
        m1_path = m1.get("path", "M1")
        d1_path = d1.get("path", "D1")
        call("model_object_info", path=m1_path)
        call("model_connect_objects", source=src1_path, target=m1_path)
        call("model_connect_objects", source=m1_path, target=d1_path)
        call("model_set_property", path=m1_path, attribute="procTime", value=2.0)
        call("model_get_property", path=m1_path, attribute="procTime")
        call("mu_create_attribute", name="BatchNo", kind="integer")
        src = call(
            "method_create",
            name="InitBatch",
            source=f': boolean\nis\ndo\n  @.BatchNo := 1;\n  @.move(str_to_obj("{m1_path}"));\n  return true;\nend;',
        )
        call("method_get_source", path=src.get("path", ""))
        call(
            "method_set_source",
            path=src.get("path", ""),
            source=f': boolean\nis\ndo\n  @.BatchNo := 2;\n  @.move(str_to_obj("{m1_path}"));\n  return true;\nend;',
        )
        call("object_set_control", path=src1_path, control="ExitCtrl", method=src.get("path", ""))
        call("simulation_set_source", path=src1_path, units=5, interval=10.0)
        call("simulation_reset")
        call("simulation_run", end_time=60.0, timeout=120.0)
        call("simulation_status")
        call("simulation_start")
        call("simulation_stop")
        call("model_read_counters", objects=["Src1", "M1", "D1"])
        call("model_delete_device", target=d1_path, confirm=True)
    finally:
        tools.close_session()
        gc.collect()
        shutil.rmtree(wd, ignore_errors=True)

    failed = [c for c in calls if not c["ok"]]
    report = {
        "tools_called": len(calls),
        "passed": len(calls) - len(failed),
        "failed": len(failed),
        "failures": [{"tool": c["tool"], "error": c["error"]} for c in failed],
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"report": report, "calls": calls}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
