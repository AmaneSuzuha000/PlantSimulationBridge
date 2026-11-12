"""Regression and hardening tests for the offline bridge layers.

All tests here run without a real Plant Simulation installation (FakeBackend
or in-memory COM mocks only). Live COM / SimTalk semantics are verified under
``PSBRIDGE_REAL_COM=1``.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import math
import re
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from psbridge import tools
from psbridge.apiquery import ApiIndex, Member
from psbridge.com.backend import SimBackend, SimStatus
from psbridge.com.executor import ComExecutor
from psbridge.com.fake import FakeBackend
from psbridge.com.win32 import Win32ComBackend
from psbridge.config import BridgeConfig
from psbridge.core import validation
from psbridge.core.ops import SafeOps
from psbridge.errors import (
    BackendError,
    BackendUnavailable,
    ConfigError,
    ExecutorClosed,
    ModelMutationDenied,
    PathNotAllowed,
    ValidationError,
)
from psbridge.experiment.contracts import ExperimentSpec
from psbridge.experiment.fake_engine import FakeDispatchEngine
from psbridge.experiment.metrics import compute_metrics
from psbridge.experiment.runner import ExperimentRunner
from psbridge.experiment.store import ResultStore
from psbridge.mcp import server as mcp_server
from psbridge.scheduling.policies import get_policy
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation
from psbridge.schema import validate_identifier as schema_validate_identifier
from psbridge.simtalk import dispatch, jobshop, templates


# ---------------------------------------------------------------------------
# 1. Schema and validation hardening
# ---------------------------------------------------------------------------
def test_validators_reject_trailing_newline_injection() -> None:
    with pytest.raises(ValidationError):
        schema_validate_identifier("M1\n", field_name="machine_id")
    with pytest.raises(ValidationError):
        validation.validate_identifier("M1\n", field_name="name")
    with pytest.raises(ValidationError):
        validation.validate_object_path(".Models.Model.M1\n", field_name="path")
    with pytest.raises(ValidationError):
        validation.validate_object_handle(".Models.Model.node(1)\n", field_name="path")
    with pytest.raises(ValidationError):
        templates.validate_simtalk_path(".Models.Model\n", field_name="frame_path")


def test_instance_from_dict_strict_shape_checks() -> None:
    base = {
        "instance_id": "bad_shape",
        "machines": [{"machine_id": "M1"}],
        "jobs": [
            {
                "job_id": "J1",
                "operations": [
                    {
                        "op_id": "O1",
                        "index": 0,
                        "candidates": "M1",
                        "duration": 1.0,
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValidationError, match="candidates must be an array"):
        Instance.from_dict(base)

    unknown_top = {
        "instance_id": "ok",
        "machines": [{"machine_id": "M1"}],
        "jobs": [
            {
                "job_id": "J1",
                "operations": [{"op_id": "O1", "index": 0, "candidates": ["M1"], "duration": 1.0}],
            }
        ],
        "extra_field": 123,
    }
    with pytest.raises(ValidationError, match="unknown instance fields"):
        Instance.from_dict(unknown_top)


def test_bridge_config_rejects_booleans() -> None:
    with pytest.raises(ConfigError):
        BridgeConfig(com_timeout_s=True)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        BridgeConfig(operation_timeout_s=False)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        BridgeConfig(max_table_read_cells=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 2. ComExecutor concurrency and lifecycle hardening
# ---------------------------------------------------------------------------
def test_com_executor_concurrent_start_waits_for_initializer() -> None:
    gate = threading.Event()
    initialized = False

    def slow_init() -> None:
        nonlocal initialized
        assert gate.wait(timeout=2.0)
        initialized = True

    executor = ComExecutor(name="concurrent-start", default_timeout=2.0, initializer=slow_init)
    results: list[bool] = []
    errors: list[BaseException] = []

    def caller() -> None:
        try:
            executor.start()
            results.append(initialized)
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(timeout=2.0)
    try:
        assert not errors
        assert results == [True, True, True]
    finally:
        executor.close()


def test_com_executor_close_rejects_further_calls_and_handles_init_failure() -> None:
    def broken_init() -> None:
        raise RuntimeError("boom")

    failing = ComExecutor(name="bad-init", default_timeout=2.0, initializer=broken_init)
    with pytest.raises(BackendUnavailable):
        failing.start()
    failing.close()

    executor = ComExecutor(name="close-test", default_timeout=2.0)
    with executor.operation("outer", timeout=1.0):
        assert executor.call(lambda: 42) == 42
        executor.close()
    assert executor.is_closed is True
    with pytest.raises(ExecutorClosed):
        executor.call(lambda: 1)


# ---------------------------------------------------------------------------
# 3. SafeOps executor routing, concurrency, and original-model protection
# ---------------------------------------------------------------------------
def _make_fake_model_file(path: Path) -> Path:
    backend = FakeBackend()
    backend.connect()
    backend.new_model()
    backend.create_object("Source", "Src1", x=0.0, y=0.0)
    backend.create_object("SingleProc", "M1", x=10.0, y=0.0)
    backend.save_model(str(path))
    backend.disconnect()
    return path


def test_safe_ops_routes_model_path_and_frame_queries_through_executor(tmp_path: Path) -> None:
    model_file = _make_fake_model_file(tmp_path / "model.psfake.json")
    executor = ComExecutor(name="ops-routing", default_timeout=2.0)
    worker_ids: list[int | None] = []
    try:
        executor.start()
        worker_tid = executor.stats().worker_thread_id
        assert worker_tid is not None

        class _ThreadCheckingFakeBackend(FakeBackend):
            @property
            def model_path(self) -> str | None:
                worker_ids.append(threading.get_ident())
                return super().model_path

            @property
            def default_frame_path(self) -> str:
                worker_ids.append(threading.get_ident())
                return super().default_frame_path

        backend = _ThreadCheckingFakeBackend()
        ops = SafeOps(backend, BridgeConfig(work_dir=tmp_path / "work", model_root=tmp_path), executor=executor)
        ops.load_model(str(model_file))
        _ = ops.model_path
        objects = ops.list_objects()
        assert len(objects) == 2
        assert worker_ids
        assert all(tid == worker_tid for tid in worker_ids)
    finally:
        executor.close()


def test_safe_ops_serializes_concurrent_ensure_connected(tmp_path: Path) -> None:
    calls = 0

    class _CountingConnectFakeBackend(FakeBackend):
        def connect(self) -> None:
            nonlocal calls
            time.sleep(0.03)
            calls += 1
            super().connect()

    ops = SafeOps(_CountingConnectFakeBackend(), BridgeConfig(work_dir=tmp_path))
    threads = [threading.Thread(target=ops.ensure_connected) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)
    assert calls == 1


def test_safe_ops_protect_original_blocks_in_place_mutations_until_cloned(tmp_path: Path) -> None:
    model_file = _make_fake_model_file(tmp_path / "orig.psfake.json")
    backend = FakeBackend()
    config = BridgeConfig(work_dir=tmp_path / "work", model_root=tmp_path, auto_save_on_disconnect=True)
    ops = SafeOps(backend, config, protect_original=True)
    with pytest.raises(PathNotAllowed):
        ops.load_model(str(tmp_path.parent / "outside.psfake.json"))
    ops.load_model(str(model_file))

    with pytest.raises(ModelMutationDenied):
        ops.configure_source(".Models.Model.Src1", units=5, interval=1.0)
    with pytest.raises(ModelMutationDenied):
        ops.create_method("Disp", ":boolean is do return true; end;")
    with pytest.raises(ModelMutationDenied):
        ops.set_method_source(".Models.Model.node(1)", "return true;")
    with pytest.raises(ModelMutationDenied):
        ops.set_object_control(".Models.Model.M1", "ExitCtrl", ".Models.Model.node(1)")
    with pytest.raises(ModelMutationDenied):
        ops.create_mu_attribute("JobId", "integer")
    with pytest.raises(ModelMutationDenied):
        ops.save_model(str(model_file))
    with pytest.raises(ModelMutationDenied):
        ops.disconnect()

    clone = ops.clone_model_for_experiment(tmp_path / "work", tag="exp1")
    assert clone.identical is True

    ops.configure_source(".Models.Model.Src1", units=5, interval=1.0, start=0.0)
    method_path = ops.create_method("Disp", ":boolean is do return true; end;")
    ops.set_object_control(".Models.Model.M1", "ExitCtrl", method_path)
    ops.create_mu_attribute("JobId", "integer")
    ops.save_model()
    assert ops.assert_original_untouched().sha256 == clone.original_before.sha256


def test_safe_ops_configure_source_validates_bounds(tmp_path: Path) -> None:
    ops = SafeOps(FakeBackend(), BridgeConfig(work_dir=tmp_path), protect_original=False)
    ops.new_model()
    ops.create_device("Source", "Src1")
    with pytest.raises(ValidationError):
        ops.configure_source(".Models.Model.Src1", units=True, interval=1.0)
    with pytest.raises(ValidationError):
        ops.configure_source(".Models.Model.Src1", units=0, interval=1.0)
    with pytest.raises(ValidationError):
        ops.configure_source(".Models.Model.Src1", units=1, interval=0.0)
    with pytest.raises(ValidationError):
        ops.configure_source(".Models.Model.Src1", units=1, interval=1.0, start=-0.5)


# ---------------------------------------------------------------------------
# 4. FakeBackend protocol & persistence parity
# ---------------------------------------------------------------------------
def test_fake_backend_implements_protocol_and_roundtrips_controls_and_mu_attrs(tmp_path: Path) -> None:
    backend = FakeBackend()
    assert isinstance(backend, SimBackend)
    backend.connect()
    backend.new_model()
    backend.create_object("SingleProc", "M1")
    method_path = backend.create_method_object("Dispatch", ".Models.Model")
    source = "param m : object\n:boolean is\ndo\n  return true;\nend;"
    backend.set_method_source(method_path, source)
    backend.set_object_control(".Models.Model.M1", "ExitCtrl", method_path)
    backend.create_mu_attribute("JobId", "integer")

    save_path = tmp_path / "roundtrip.psfake.json"
    backend.save_model(str(save_path))
    backend.disconnect()

    reloaded = FakeBackend()
    reloaded.connect()
    reloaded.load_model(str(save_path))
    assert reloaded.get_method_source(method_path) == source
    assert reloaded.get_object_info(".Models.Model.M1").class_name == ".MaterialFlow.SingleProc"
    reloaded.set_object_control(".Models.Model.M1", "EntranceCtrl", method_path)


# ---------------------------------------------------------------------------
# 5. Win32ComBackend mocked hardening
# ---------------------------------------------------------------------------
class _FakePythonCom:
    def CoInitialize(self) -> None:
        pass

    def CoUninitialize(self) -> None:
        pass


class _MockComApp:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.get_calls: list[str] = []
        self.values: dict[str, Any] = {
            ".Models.NumNodes": 1,
            'to_str(.Models.node(1).Name)': "FactoryAlpha",
            'existsObject(".Models.FactoryAlpha")': True,
            'to_str(.Models.FactoryAlpha.NumNodes)': "1",
            'to_str(.Models.FactoryAlpha.node(1).Name)': "M1",
            'to_str(.Models.FactoryAlpha.node(1).Class)': ".MaterialFlow.SingleProc",
            ".Models.FactoryAlpha.EventController.simTime": 12.5,
            ".Models.FactoryAlpha.EventController.isRunning": False,
            ".Models.FactoryAlpha.EventController.End": 60.0,
        }
        self.method_sources: dict[str, str] = {}
        self.loaded_temp_files: list[str] = []
        self.running_sequence: list[bool] = []
        self.bad_num_nodes = False

    def SetNoMessageBox(self, flag: bool) -> None:
        pass

    def SetVisible(self, flag: bool) -> None:
        pass

    def LoadModel(self, path: str) -> None:
        pass

    def Quit(self) -> None:
        pass

    def IsSimulationRunning(self) -> Any:
        if self.running_sequence:
            return self.running_sequence.pop(0)
        return self.values[".Models.FactoryAlpha.EventController.isRunning"]

    def GetValue(self, expr: str) -> Any:
        self.get_calls.append(expr)
        key = expr.rstrip().rstrip(";")
        if key == "to_str(.Models.FactoryAlpha.NumNodes)" and self.bad_num_nodes:
            return "not-an-int"
        if key == "to_str(.Models.FactoryAlpha.node(2).Program)":
            return self.method_sources[".Models.FactoryAlpha.node(2)"]
        return self.values[key]

    def SetValue(self, path: str, value: Any) -> None:
        self.values[path] = value

    def ExecuteSimTalk(self, source: str) -> str:
        self.queries.append(source)
        for line in source.splitlines():
            stripped = line.strip()
            if ".load(" in stripped and stripped.endswith(");"):
                start = stripped.index('.load("') + len('.load("')
                quoted = stripped[start:-3]
                file_path = quoted.replace('\\"', '"').replace("\\\\", "\\")
                self.loaded_temp_files.append(file_path)
                content = Path(file_path).read_text(encoding="utf-8")
                self.method_sources[".Models.FactoryAlpha.node(2)"] = content
        return ""


def _mock_win32_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Win32ComBackend, _MockComApp]:
    from psbridge.com import win32 as win

    app = _MockComApp()

    class _Client:
        @staticmethod
        def GetActiveObject(_prog_id: str) -> Any:
            return app

        @staticmethod
        def Dispatch(_prog_id: str) -> Any:
            return app

    monkeypatch.setattr(win, "_import_pywin32", lambda: (_FakePythonCom(), _Client()))
    backend = Win32ComBackend(model_root=tmp_path)
    backend.connect()
    return backend, app


def test_win32_backend_validation_temp_file_and_discovered_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_file = tmp_path / "renamed.spp"
    model_file.write_bytes(b"placeholder")
    backend, app = _mock_win32_backend(monkeypatch, tmp_path)
    try:
        assert isinstance(backend, SimBackend)
        backend.load_model(str(model_file))

        with pytest.raises(ValidationError):
            backend.configure_source(".Models.FactoryAlpha.Src1", units=True, interval=1.0, start=0.0)
        with pytest.raises(ValidationError):
            backend.configure_source(".Models.FactoryAlpha.Src1", units=1, interval=0.0, start=0.0)
        with pytest.raises(ValidationError):
            backend.set_statistics_start(-1.0)
        with pytest.raises(ValidationError):
            backend.set_statistics_start(True)

        app.bad_num_nodes = True
        with pytest.raises(BackendError, match="usable node count"):
            backend.create_method_object("M", ".Models.FactoryAlpha")
        app.bad_num_nodes = False

        multiline = "param tag : string\n:boolean is\ndo\n  -- UTF-8 comment: alpha beta gamma cafe\n  return true;\nend;"
        backend.set_method_source(".Models.FactoryAlpha.node(2)", multiline)
        assert app.loaded_temp_files
        assert not Path(app.loaded_temp_files[-1]).exists()
        assert backend.get_method_source(".Models.FactoryAlpha.node(2)") == multiline

        digest = backend.state_hash()
        assert len(digest) == 64
        assert hashlib.sha256(b"").hexdigest() != digest
    finally:
        backend.disconnect()


# ---------------------------------------------------------------------------
# 6. Tools & MCP Server hardening
# ---------------------------------------------------------------------------
def test_tools_validate_arguments_before_opening_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def _must_not_connect(_args: Any) -> Any:
        raise AssertionError("_session should not be called when validation fails")

    monkeypatch.setattr(tools, "_session", _must_not_connect)

    bad_calls = [
        ("model_delete_device", {"target": "M1", "confirm": False}),
        ("model_create_device", {"class_name": "SingleProc", "name": "M1", "x": float("nan")}),
        ("simulation_start", {"statistics_start": -1.0}),
        ("simulation_run", {"timeout": 0.0}),
        ("simulation_set_source", {"path": ".Models.Model.S1", "units": True, "interval": 1.0}),
        ("model_list_objects", {"frame_path": ".Models.Model\n"}),
        ("model_status", {"work_dir": "bad\x00dir"}),
    ]
    for name, args in bad_calls:
        res = tools.call_tool(name, args)
        assert res["ok"] is False
        assert res["error"]["code"] == "VALIDATION_ERROR"


def test_tools_live_session_wiring_model_root_check_and_close_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from psbridge.com import win32 as win

    app = _MockComApp()

    class _Client:
        @staticmethod
        def GetActiveObject(_prog_id: str) -> Any:
            return app

        @staticmethod
        def Dispatch(_prog_id: str) -> Any:
            return app

    monkeypatch.setattr(win, "_import_pywin32", lambda: (_FakePythonCom(), _Client()))
    tools.close_session()
    try:
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        status = tools.call_tool("model_status", {"model_root": str(root_a), "work_dir": str(tmp_path / "w")})
        assert status["ok"] is True
        assert status["connected"] is True
        assert tools._SESSION["executor"] is not None

        mismatch = tools.call_tool("model_status", {"model_root": str(root_b)})
        assert mismatch["ok"] is False
        assert mismatch["error"]["code"] == "VALIDATION_ERROR"
    finally:
        tools.close_session()
    assert tools._SESSION["ops"] is None
    assert tools._SESSION["executor"] is None


def test_mcp_server_rejects_falsy_non_object_params_and_invalid_ids() -> None:
    for invalid_id in (True, False, 1.5, [1], {"a": 1}):
        resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": invalid_id, "method": "ping"})
        assert resp is not None
        assert resp["id"] is None
        assert resp["error"]["code"] == mcp_server.INVALID_REQUEST

    for bad_params in ([], "", 0, False):
        resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": bad_params})
        assert resp is not None
        assert resp["id"] == 9
        assert resp["error"]["code"] == mcp_server.INVALID_PARAMS


def test_win32_status_reads_is_running_before_sim_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_file = tmp_path / "m.spp"
    model_file.write_bytes(b"placeholder")
    backend, app = _mock_win32_backend(monkeypatch, tmp_path)
    try:
        backend.load_model(str(model_file))
        app.get_calls.clear()
        status = backend.status()
        assert status.running is False
        assert status.sim_time == 12.5
        ctrl = ".Models.FactoryAlpha.EventController"
        assert app.get_calls.index(ctrl + ".isRunning") < app.get_calls.index(ctrl + ".simTime")
    finally:
        backend.disconnect()


def test_mcp_server_serve_closes_session_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(mcp_server, "close_session", lambda: closed.append(True))

    stdin = io.StringIO('{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n')
    stdout = io.StringIO()
    assert mcp_server.serve(stdin, stdout, io.StringIO()) == 0
    assert closed == [True]


def test_win32_wait_until_finished_waits_past_startup_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend, app = _mock_win32_backend(monkeypatch, tmp_path)
    try:
        app.running_sequence = [False, False, True, True, False]
        assert backend.wait_until_finished(timeout=2.0, poll=0.005, startup_grace=0.5) is True
        assert app.running_sequence == []

        t0 = time.monotonic()
        assert backend.wait_until_finished(timeout=2.0, poll=0.005, startup_grace=0.03) is True
        assert time.monotonic() - t0 < 0.5
    finally:
        backend.disconnect()


def test_safe_ops_run_simulation_startup_grace_and_timeout(tmp_path: Path) -> None:
    class _SequenceStatusFakeBackend(FakeBackend):
        def __init__(self, statuses: Sequence[SimStatus] = ()) -> None:
            super().__init__()
            self._queued_statuses = list(statuses)

        def status(self) -> SimStatus:
            self._require_connected()
            if self._queued_statuses:
                return self._queued_statuses.pop(0)
            return super().status()

    seq_backend = _SequenceStatusFakeBackend(
        [
            SimStatus(running=False, finished=False, sim_time=0.0, end_time=40.0, state="stopped"),
            SimStatus(running=False, finished=False, sim_time=0.0, end_time=40.0, state="stopped"),
            SimStatus(running=True, finished=False, sim_time=10.0, end_time=40.0, state="running"),
            SimStatus(running=False, finished=True, sim_time=40.0, end_time=40.0, state="finished"),
        ]
    )
    ops = SafeOps(seq_backend, BridgeConfig(work_dir=tmp_path), protect_original=False)
    ops.new_model()
    final = ops.run_simulation(end_time=40.0, timeout=2.0, poll=0.005, startup_grace=0.5)
    assert final.finished is True
    assert final.sim_time == 40.0
    assert seq_backend._queued_statuses == []

    fast_backend = _SequenceStatusFakeBackend(
        [SimStatus(running=False, finished=True, sim_time=40.0, end_time=40.0, state="finished")]
    )
    fast_ops = SafeOps(fast_backend, BridgeConfig(work_dir=tmp_path), protect_original=False)
    fast_ops.new_model()
    t0 = time.monotonic()
    fast_status = fast_ops.run_simulation(end_time=40.0, timeout=2.0, poll=0.005, startup_grace=0.8)
    assert fast_status.finished is True
    assert time.monotonic() - t0 < 0.4

    stopped_backend = _SequenceStatusFakeBackend(
        [SimStatus(running=False, finished=False, sim_time=0.0, end_time=40.0, state="stopped")] * 20
    )
    stopped_ops = SafeOps(stopped_backend, BridgeConfig(work_dir=tmp_path), protect_original=False)
    stopped_ops.new_model()
    t1 = time.monotonic()
    stopped_status = stopped_ops.run_simulation(end_time=40.0, timeout=2.0, poll=0.005, startup_grace=0.03)
    assert stopped_status.state == "stopped"
    assert time.monotonic() - t1 < 0.5

    stuck_ops = SafeOps(FakeBackend(), BridgeConfig(work_dir=tmp_path), protect_original=False)
    stuck_ops.new_model()
    with pytest.raises(BackendError, match="timeout"):
        stuck_ops.run_simulation(end_time=40.0, timeout=0.03, poll=0.005, startup_grace=0.5)

    for bad_grace in (-0.1, float("nan"), True):
        with pytest.raises(ValidationError):
            stuck_ops.run_simulation(end_time=40.0, startup_grace=bad_grace)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. SimTalk dispatch & jobshop scaling and injection hardening
# ---------------------------------------------------------------------------
def _eval_argmin_simtalk(body: str, wip: Sequence[int]) -> int:
    """Mini interpreter for the generated nested-if SimTalk argmin body."""
    cond_pattern = re.compile(
        r"\(\.Models\.F\.M(\d+)\.statNumIn - \.Models\.F\.M\1\.statNumOut\) "
        r"(<=|<) "
        r"\(\.Models\.F\.M(\d+)\.statNumIn - \.Models\.F\.M\3\.statNumOut\)"
    )
    move_pattern = re.compile(r"@\.move\(\.Models\.F\.M(\d+)\);")

    if len(wip) == 2:
        match = cond_pattern.search(body)
        assert match is not None
        left, op, right = int(match.group(1)), match.group(2), int(match.group(3))
        if op == "<=":
            return left if wip[left] <= wip[right] else right
        return left if wip[left] < wip[right] else right

    tokens = body.split(" return true; end;")
    for segment in tokens[:-1]:
        conds = cond_pattern.findall(segment)
        moved = move_pattern.search(segment)
        assert moved is not None
        if all((wip[int(a)] < wip[int(b)] if op == "<" else wip[int(a)] <= wip[int(b)]) for a, op, b in conds):
            return int(moved.group(1))
    last_move = move_pattern.search(tokens[-1])
    assert last_move is not None
    return int(last_move.group(1))


def test_dispatch_argmin_exact_2_exhaustive_3_4_and_25_scaling() -> None:
    two = dispatch.build_queue_rule([".Models.F.M0", ".Models.F.M1"])
    assert two == (
        "if (.Models.F.M0.statNumIn - .Models.F.M0.statNumOut) <= "
        "(.Models.F.M1.statNumIn - .Models.F.M1.statNumOut) "
        "then @.move(.Models.F.M0); else @.move(.Models.F.M1); end;"
    )

    with pytest.raises(ValidationError, match="distinct"):
        dispatch.build_queue_rule([".Models.F.M0", ".Models.F.M0"])

    for n in (3, 4):
        candidates = [f".Models.F.M{i}" for i in range(n)]
        body = dispatch.build_queue_rule(candidates)
        total = 4**n
        for mask in range(total):
            wip = [(mask >> (2 * i)) & 3 for i in range(n)]
            expected = min(range(n), key=wip.__getitem__)
            assert _eval_argmin_simtalk(body, wip) == expected

    candidates_25 = [f".Models.F.M{i}" for i in range(25)]
    t0 = time.perf_counter()
    prog = dispatch.build_exit_control_program(candidates_25)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.25
    assert len(prog) < 100_000


def test_jobshop_rejects_duplicate_jobs_drain_collisions_and_bad_numbers() -> None:
    j1 = jobshop.Job("J1", 2, 0.0, 1.0, (jobshop.Operation("M1", 3.0), jobshop.Operation("M2", 4.0)))
    j1_dup = jobshop.Job("J1", 1, 0.0, 1.0, (jobshop.Operation("M1", 3.0),))
    with pytest.raises(ValidationError, match="duplicate job name"):
        jobshop.build_job_shop_statements([j1, j1_dup], ".Models.F")

    for colliding_drain in ("M_M1", "Src_J1", "Buf_J1_2"):
        with pytest.raises(ValidationError, match="collides"):
            jobshop.build_job_shop_statements([j1], ".Models.F", drain=colliding_drain)

    with pytest.raises(ValidationError):
        jobshop.Operation("M1", float("nan"))
    with pytest.raises(ValidationError):
        jobshop.Operation("M1", -1.0)
    with pytest.raises(ValidationError):
        jobshop.Job("J1", True, 0.0, 1.0, (jobshop.Operation("M1", 1.0),))


# ---------------------------------------------------------------------------
# 8. Metrics, Runner, Store & ApiIndex hardening
# ---------------------------------------------------------------------------
def _naive_midpoint_avg_wip(
    instance: Instance,
    schedule: Sequence[ScheduledOperation],
    observation: float,
) -> float:
    ends = {item.op_id: item.end for item in schedule}
    completions = {job.job_id: max(ends[op.op_id] for op in job.operations) for job in instance.jobs}
    pts = {0.0, observation}
    for job in instance.jobs:
        for val in (float(job.release_time), completions[job.job_id]):
            if 0.0 < val < observation:
                pts.add(val)
    boundaries = sorted(pts)
    total = 0.0
    for a, b in itertools.pairwise(boundaries):
        mid = 0.5 * (a + b)
        wip = sum(1 for job in instance.jobs if float(job.release_time) <= mid < completions[job.job_id])
        total += wip * (b - a)
    return total / observation


def test_metrics_exact_avg_wip_matches_reference_integral() -> None:
    inst = Instance(
        instance_id="wip_ref",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 3.0),), release_time=0.0),
            Job("J2", (Operation("J2_O1", "J2", 0, ("M2",), 5.0),), release_time=2.5),
            Job("J3", (Operation("J3_O1", "J3", 0, ("M1",), 4.0),), release_time=6.0),
        ),
    )
    sched = (
        ScheduledOperation("J1_O1", "J1", "M1", 0.0, 3.0),
        ScheduledOperation("J2_O1", "J2", "M2", 2.5, 7.5),
        ScheduledOperation("J3_O1", "J3", "M1", 6.0, 10.0),
    )
    for horizon in (None, 5.0, 15.0):
        obs = 10.0 if horizon is None else horizon
        metrics = compute_metrics(inst, sched, horizon=horizon)
        assert math.isclose(metrics.avg_wip, _naive_midpoint_avg_wip(inst, sched, obs), rel_tol=0.0, abs_tol=1e-12)


def test_runner_handles_large_instance_and_caches_seed_variant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    machines = tuple(Machine(f"M{i}") for i in range(10))
    jobs = tuple(
        Job(
            job_id=f"J{j}",
            operations=tuple(
                Operation(f"J{j}_O{k}", f"J{j}", k, (f"M{(j + k) % 10}",), 1.0)
                for k in range(10)
            ),
        )
        for j in range(260)
    )
    large_inst = Instance(instance_id="large_2600", machines=machines, jobs=jobs)
    assert large_inst.total_operations == 2600

    config = BridgeConfig(work_dir=tmp_path)
    runner = ExperimentRunner(FakeDispatchEngine(), config, ResultStore(tmp_path / "results"))
    spec_large = ExperimentSpec("large_exp", large_inst, "fifo", seeds=(0,))
    large_records = runner.run_spec(spec_large, [get_policy("fifo")], replications=1)
    assert len(large_records) == 1
    assert large_records[0].status == "valid"

    small_inst = Instance(
        instance_id="cache_test",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 2.0),)),),
    )
    import psbridge.experiment.runner as runner_mod

    orig_sample = runner_mod.sample_variant
    sample_calls: list[int] = []

    def _counting_sample(inst: Instance, seed: int, jitter: float) -> Instance:
        sample_calls.append(seed)
        return orig_sample(inst, seed, jitter)

    monkeypatch.setattr(runner_mod, "sample_variant", _counting_sample)
    spec_small = ExperimentSpec("cache_exp", small_inst, "fifo", seeds=(0, 1), duration_jitter=0.1)
    records = runner.run_spec(spec_small, [get_policy("fifo"), get_policy("spt")], replications=2)
    assert len(records) == 8
    assert sample_calls == [0, 1]


def test_store_unicode_separators_tail_handling_and_summary_path_security(tmp_path: Path) -> None:
    store = ResultStore(tmp_path / "store")
    store.ensure_dirs()

    store.runs_path.write_text('{"run_id": "u", "note": "a\u2028b\u2029c"}\n', encoding="utf-8")
    rows = store.read_run_dicts(strict=True)
    assert len(rows) == 1
    assert rows[0]["note"] == "a\u2028b\u2029c"

    # Incomplete trailing write (no trailing newline) tolerated when strict=False
    store.runs_path.write_text('{"run_id": "ok"}\n{"run_id": "cut', encoding="utf-8")
    assert len(store.read_run_dicts(strict=False)) == 1
    assert store.malformed_lines == []

    # Corrupt newline-terminated trailing line IS reported as malformed
    store.runs_path.write_text('{"run_id": "ok"}\n{"run_id": "bad"\n', encoding="utf-8")
    assert len(store.read_run_dicts(strict=False)) == 1
    assert store.malformed_lines == [2]
    with pytest.raises(ValidationError):
        store.read_run_dicts(strict=True)

    for bad_id in ("", "   ", "..", "../escape", "a/../../b", ".hidden", "bad\x00id"):
        with pytest.raises(ValidationError):
            store.summary_path(bad_id)


def test_apiquery_search_limit_edge_cases() -> None:
    idx = ApiIndex([Member(name="Alpha", owner="SingleProc", kind="attribute")])
    assert idx.search("alpha", limit=0) == []
    assert idx.search("alpha", limit=-3) == []
    with pytest.raises(TypeError):
        idx.search("alpha", limit=True)
