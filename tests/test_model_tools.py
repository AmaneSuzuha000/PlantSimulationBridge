"""Tests for live-model MCP tools: schema shape and argument plumbing.

These tests pin the MCP surface so a tool cannot quietly change its contract.
"""
from __future__ import annotations

from typing import Any

import pytest

from psbridge import tools
from psbridge.com.backend import SimStatus

MODEL_TOOLS = (
    "model_status",
    "model_open",
    "model_list_objects",
    "model_object_info",
    "model_create_device",
    "model_delete_device",
    "model_connect_objects",
    "model_get_property",
    "model_set_property",
)


class _FakeInfo:
    def __init__(self, path: str, name: str, class_name: str) -> None:
        self.path = path
        self.name = name
        self.class_name = class_name


class _FakeFingerprint:
    path = "C:/tmp/model.spp"
    sha256 = "abc123"
    size = 4096


class FakeOps:
    """Records what the tools ask SafeOps to do."""

    def __init__(self) -> None:
        self.model_path = "C:/tmp/model.spp"
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def ensure_connected(self) -> None:
        self._record("ensure_connected")

    def load_model(self, path: str) -> Any:
        self._record("load_model", path)
        return _FakeFingerprint()

    def list_objects(self, frame_path: str) -> tuple[Any, ...]:
        self._record("list_objects", frame_path)
        return (_FakeInfo(frame_path + ".S1", "S1", ".MaterialFlow.SingleProc"),)

    def object_info(self, path: str) -> Any:
        self._record("object_info", path)
        return _FakeInfo(path, "S1", ".MaterialFlow.SingleProc")

    def create_device(self, class_name: str, name: str, **kwargs: Any) -> Any:
        self._record("create_device", class_name, name, **kwargs)
        return _FakeInfo(".Models.Model." + name, name, class_name)

    def delete_device(self, target: str, **kwargs: Any) -> None:
        self._record("delete_device", target, **kwargs)

    def connect_objects(self, source: str, target: str, **kwargs: Any) -> None:
        self._record("connect_objects", source, target, **kwargs)

    def get_property(self, path: str, attribute: str, **kwargs: Any) -> Any:
        self._record("get_property", path, attribute, **kwargs)
        return 5.0

    def set_property(self, path: str, attribute: str, value: Any, **kwargs: Any) -> Any:
        self._record("set_property", path, attribute, value, **kwargs)
        return value

    def simulation_status(self) -> SimStatus:
        self._record("simulation_status")
        return SimStatus(running=False, finished=True, sim_time=60.0, end_time=60.0, state="finished")

    def start_simulation(self, *, end_time: float | None = None,
                         statistics_start: float | None = 0.0) -> None:
        self._record("start_simulation", end_time=end_time,
                     statistics_start=statistics_start)

    def stop_simulation(self) -> None:
        self._record("stop_simulation")

    def reset_simulation(self) -> None:
        self._record("reset_simulation")

    def run_simulation(self, *, end_time: float | None = None,
                       timeout: float = 300.0,
                       statistics_start: float | None = 0.0) -> SimStatus:
        self._record("run_simulation", end_time=end_time, timeout=timeout,
                     statistics_start=statistics_start)
        return SimStatus(running=False, finished=True, sim_time=60.0, end_time=60.0, state="finished")


@pytest.fixture()
def ops(monkeypatch: pytest.MonkeyPatch) -> FakeOps:
    fake = FakeOps()
    monkeypatch.setitem(tools._SESSION, "ops", fake)
    return fake


def test_every_model_tool_is_declared_and_closed() -> None:
    by_name = {spec["name"]: spec for spec in tools.TOOLS}
    for name in MODEL_TOOLS:
        spec = by_name[name]
        assert spec["description"]
        schema = spec["input_schema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert callable(spec["handler"])


def test_model_status_reports_the_tracked_model(ops: FakeOps) -> None:
    result = tools.call_tool("model_status", {})
    assert result["ok"] is True
    assert result["model_path"] == "C:/tmp/model.spp"
    assert result["backend"] == "win32"


def test_model_open_fingerprints_the_file(ops: FakeOps) -> None:
    result = tools.call_tool("model_open", {"path": "C:/tmp/model.spp"})
    assert result["ok"] is True
    assert result["sha256"] == "abc123"
    assert result["size"] == 4096


def test_model_list_objects_returns_paths_and_real_class(ops: FakeOps) -> None:
    result = tools.call_tool("model_list_objects", {"frame_path": ".Models.Model.Frame"})
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["objects"][0]["class_name"] == ".MaterialFlow.SingleProc"
    assert result["objects"][0]["path"] == ".Models.Model.Frame.S1"


def test_model_create_device_defaults_coordinates(ops: FakeOps) -> None:
    result = tools.call_tool("model_create_device", {"class_name": "SingleProc", "name": "P1"})
    assert result["ok"] is True
    name, args, kwargs = ops.calls[-1]
    assert (name, args) == ("create_device", ("SingleProc", "P1"))
    assert kwargs["x"] == 0.0 and kwargs["y"] == 0.0


def test_model_delete_device_needs_confirm(ops: FakeOps) -> None:
    refused = tools.call_tool("model_delete_device", {"target": "P1"})
    assert refused["ok"] is False
    assert refused["error"]["code"] == "VALIDATION_ERROR"

    done = tools.call_tool("model_delete_device", {"target": "P1", "confirm": True})
    assert done["ok"] is True
    assert ops.calls[-1][2]["confirm"] is True


def test_model_set_property_forwards_the_value(ops: FakeOps) -> None:
    result = tools.call_tool(
        "model_set_property", {"path": ".Models.Model.P1", "attribute": "procTime", "value": 5.0}
    )
    assert result["ok"] is True
    assert ops.calls[-1][1] == (".Models.Model.P1", "procTime", 5.0)


def test_model_tools_reject_undeclared_arguments(ops: FakeOps) -> None:
    result = tools.call_tool("model_get_property", {"path": "P1", "attribute": "procTime", "zap": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"

SIMULATION_TOOLS = (
    "simulation_status",
    "simulation_start",
    "simulation_stop",
    "simulation_reset",
    "simulation_run",
)


def test_every_simulation_tool_is_declared() -> None:
    by_name = {spec["name"]: spec for spec in tools.TOOLS}
    for name in SIMULATION_TOOLS:
        schema = by_name[name]["input_schema"]
        assert schema["additionalProperties"] is False
        assert "model_root" in schema["properties"]


def test_simulation_status_reports_the_controller(ops: FakeOps) -> None:
    result = tools.call_tool("simulation_status", {})
    assert result["ok"] is True
    assert result["state"] == "finished"
    assert result["sim_time"] == 60.0


def test_simulation_start_says_it_returns_immediately(ops: FakeOps) -> None:
    result = tools.call_tool("simulation_start", {"end_time": 30.0})
    assert result["ok"] is True
    # Measured: start is asynchronous, so the tool must not imply a finished run.
    assert result["returns_immediately"] is True
    assert ops.calls[-1][2]["end_time"] == 30.0
    # Measured: StartStat defaults to one hour and clears every counter
    # there, so the tool asks for cumulative statistics unless told otherwise.
    assert ops.calls[-1][2]["statistics_start"] == 0.0


def test_simulation_run_forwards_the_timeout(ops: FakeOps) -> None:
    result = tools.call_tool("simulation_run", {"end_time": 30.0, "timeout": 12.0})
    assert result["ok"] is True
    assert ops.calls[-1][2] == {"end_time": 30.0, "timeout": 12.0,
                                "statistics_start": 0.0}


def test_statistics_start_can_be_left_to_the_model(ops: FakeOps) -> None:
    """null means "do not touch StartStat", for someone who set it himself."""
    assert tools.call_tool("simulation_run", {"statistics_start": None})["ok"] is True
    assert ops.calls[-1][2]["statistics_start"] is None
    assert tools.call_tool("simulation_run", {"statistics_start": 3600.0})["ok"] is True
    assert ops.calls[-1][2]["statistics_start"] == 3600.0


def test_simulation_reset_and_stop(ops: FakeOps) -> None:
    assert tools.call_tool("simulation_reset", {})["ok"] is True
    assert tools.call_tool("simulation_stop", {})["ok"] is True
