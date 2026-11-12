"""Tests for the MCP-ready tool layer (pure handlers, no transport)."""

from __future__ import annotations

import json
from pathlib import Path

from psbridge import tools
from psbridge.demo import build_demo_instance, write_instance
from psbridge.schema import Instance, Job, Machine, Operation


def tiny_instance() -> Instance:
    return Instance(
        instance_id="tool_tiny",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                job_id="J1",
                due_date=10.0,
                operations=(
                    Operation("J1_O1", "J1", 0, ("M1",), 3.0),
                    Operation("J1_O2", "J1", 1, ("M2",), 2.0),
                ),
            ),
            Job(
                job_id="J2",
                release_time=1.0,
                due_date=5.0,
                operations=(
                    Operation("J2_O1", "J2", 0, ("M1",), 2.0),
                    Operation("J2_O2", "J2", 1, ("M2",), 4.0),
                ),
            ),
        ),
    )


def test_registry_integrity() -> None:
    names = tools.tool_names()
    assert len(names) == len(set(names))
    assert len(names) >= 9
    for spec in tools.TOOLS:
        assert spec["description"]
        assert spec["input_schema"]["type"] == "object"
        assert callable(spec["handler"])
        assert spec["input_schema"].get("additionalProperties") is False


def test_bridge_info() -> None:
    result = tools.call_tool("bridge_info")
    assert result["ok"] is True
    assert result["plant_simulation_verified"] is True
    assert set(result["policies"]) == {"fifo", "spt", "edd"}
    assert "verified" in result["note"].lower()
    assert "NOT_VERIFIED" not in result["note"]


def test_validate_instance_tool() -> None:
    result = tools.call_tool("validate_instance", {"instance": tiny_instance().to_dict()})
    assert result["ok"] is True
    assert result["valid"] is True
    assert result["engine_compatibility"]["compatible"] is True
    assert result["total_operations"] == 4
    assert result["summary"]["operation_count"] == 4

    broken = tiny_instance().to_dict()
    broken["jobs"][0]["operations"][0]["candidates"] = ["M9"]
    result = tools.call_tool("validate_instance", {"instance": broken})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_validate_instance_reports_flexible_routing_incompatibility() -> None:
    flex = Instance(
        instance_id="flex",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(Job(job_id="J1", operations=(Operation("J1_O1", "J1", 0, ("M1", "M2"), 2.0),)),),
    )
    result = tools.call_tool("validate_instance", {"instance": flex.to_dict()})
    assert result["ok"] is True
    assert result["engine_compatibility"]["compatible"] is False
    assert any(check["name"] == "single_candidate_routing" for check in result["engine_compatibility"]["checks"])


def test_compare_policies_persists_and_summarizes(tmp_path: Path) -> None:
    work_dir = str(tmp_path / "work")
    result = tools.call_tool(
        "compare_policies",
        {"instance": tiny_instance().to_dict(), "policies": ["fifo", "spt"], "seeds": [0, 1], "jitter": 0.1, "work_dir": work_dir},
    )
    assert result["ok"] is True
    assert result["run_count"] == 4
    assert result["valid_runs"] == 4
    assert result["summary"]["run_count"] == 4
    assert all(run["status"] == "valid" for run in result["runs"])
    assert Path(work_dir, "results", "runs.jsonl").exists()

    listed = tools.call_tool("list_runs", {"work_dir": work_dir})
    assert listed["ok"] is True
    assert listed["run_count"] == 4

    summary = tools.call_tool("experiment_summary", {"work_dir": work_dir})
    assert summary["ok"] is True
    assert summary["summary"]["run_count"] == 4


def test_compare_policies_rejects_unknown_policy_and_arguments() -> None:
    result = tools.call_tool("compare_policies", {"instance": tiny_instance().to_dict(), "policies": ["nope"]})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
    result = tools.call_tool("compare_policies", {"instance": tiny_instance().to_dict(), "surprise": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_run_experiment_and_reference_agreement(tmp_path: Path) -> None:
    result = tools.call_tool(
        "run_experiment",
        {"instance": tiny_instance().to_dict(), "policy": "edd", "seeds": [0, 1], "work_dir": str(tmp_path / "runs")},
    )
    assert result["ok"] is True
    assert result["policy"] == "edd"
    assert result["run_count"] == 2
    assert all(run["validity_failures"] == [] for run in result["runs"])

    compared = tools.call_tool("schedule_vs_reference", {"instance": tiny_instance().to_dict(), "policy": "edd"})
    assert compared["ok"] is True
    assert compared["agree"] is True
    assert compared["engine_valid"] is True
    assert compared["reference_valid"] is True
    assert compared["engine_metrics"] == compared["reference_metrics"]


def test_reproduce_run_is_identical(tmp_path: Path) -> None:
    result = tools.call_tool(
        "reproduce_run",
        {
            "instance": tiny_instance().to_dict(),
            "policy": "spt",
            "seed": 3,
            "jitter": 0.1,
            "replications": 3,
            "work_dir": str(tmp_path / "repro"),
        },
    )
    assert result["ok"] is True
    assert result["identical"] is True
    assert result["statuses"] == ["valid", "valid", "valid"]
    assert len(result["signatures"]) == 3


def test_reproduce_run_requires_two_replications() -> None:
    result = tools.call_tool(
        "reproduce_run",
        {"instance": tiny_instance().to_dict(), "policy": "fifo", "replications": 1},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"


def test_generate_simtalk_returns_all_verified_templates() -> None:
    result = tools.call_tool(
        "generate_simtalk",
        {"instance": tiny_instance().to_dict(), "policy": "spt", "frame_path": ".Models.Custom.Frame"},
    )
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["frame_path"] == ".Models.Custom.Frame"
    assert set(result["sources"]) == {
        "build_model.simtalk",
        "initialize_tables.simtalk",
        "dispatch_method.simtalk",
        "on_source_exit.simtalk",
        "on_station_enter.simtalk",
        "on_station_exit.simtalk",
        "read_results.simtalk",
    }
    assert "NOT_VERIFIED" not in result["sources"]["dispatch_method.simtalk"]
    assert ".Models.Custom.Frame" in result["sources"]["build_model.simtalk"]

    bad = tools.call_tool(
        "generate_simtalk",
        {"instance": tiny_instance().to_dict(), "policy": "spt", "frame_path": "bad path"},
    )
    assert bad["ok"] is False
    assert bad["error"]["code"] == "VALIDATION_ERROR"


def test_instance_can_be_a_json_file(tmp_path: Path) -> None:
    path = write_instance(tiny_instance(), tmp_path / "tiny.json")
    result = tools.call_tool("validate_instance", {"instance": str(path)})
    assert result["ok"] is True
    assert result["instance_id"] == "tool_tiny"


def test_unknown_tool_returns_structured_error() -> None:
    result = tools.call_tool("does_not_exist")
    assert result["ok"] is False
    assert result["error"]["code"] == "UNKNOWN_TOOL"
    assert "available" in result["error"]["details"]


def test_demo_instance_tool_roundtrip(tmp_path: Path) -> None:
    result = tools.call_tool(
        "compare_policies",
        {"instance": build_demo_instance().to_dict(), "policies": ["fifo"], "seeds": [0], "work_dir": str(tmp_path / "demo")},
    )
    assert result["ok"] is True
    assert result["run_count"] == 1
    json.dumps(result)
