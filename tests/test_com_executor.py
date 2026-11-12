"""Tests for the single-thread COM executor.

The Win32 backend is tested through pure snippet builders plus a canned COM
stand-in, so the suite runs without pywin32 and without Plant Simulation. The
one test that really dispatches COM is skipped unless both are present.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from psbridge.com import win32
from psbridge.com.executor import ComExecutor, ExecutorStats
from psbridge.errors import (
    BackendError,
    BackendUnavailable,
    ExecutorClosed,
    ObjectExists,
    ObjectNotFound,
    OperationTimeout,
    PathNotAllowed,
    TypeNotAllowed,
    ValidationError,
    VerificationFailed,
)


# ---------------------------------------------------------------------------
# executor: threading and results
# ---------------------------------------------------------------------------
def test_calls_run_on_one_dedicated_worker_thread() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        seen = {executor.call(threading.get_ident) for _ in range(5)}
        assert len(seen) == 1
        assert seen != {threading.get_ident()}
        assert executor.stats().calls == 5
        assert executor.thread_id in seen
    finally:
        executor.close()


def test_call_passes_arguments_and_returns_the_result() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        assert executor.call(lambda a, b, c=0: (a, b, c), 1, 2, c=3) == (1, 2, 3)
    finally:
        executor.close()


def test_worker_exception_propagates_with_its_original_type() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        with pytest.raises(ValueError, match="boom"):
            executor.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        stats = executor.stats()
        assert stats.failures == 1
        assert stats.last_error is not None and "ValueError" in stats.last_error
        assert executor.call(lambda: "still alive") == "still alive"
    finally:
        executor.close()


def test_timeout_error_raised_by_the_callable_is_not_converted() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        def raise_timeout() -> None:
            raise TimeoutError("inner")

        with pytest.raises(TimeoutError) as excinfo:
            executor.call(raise_timeout)
        assert not isinstance(excinfo.value, OperationTimeout)
        assert str(excinfo.value) == "inner"
    finally:
        executor.close()


def test_call_timeout_leaves_the_worker_usable() -> None:
    executor = ComExecutor(default_timeout=5.0)
    release = threading.Event()
    try:
        with pytest.raises(OperationTimeout):
            executor.call(release.wait, 5.0, timeout=0.05)
        release.set()
        assert executor.call(lambda: "recovered", timeout=5.0) == "recovered"
        assert executor.stats().timeouts >= 1
    finally:
        release.set()
        executor.close()


def test_stats_snapshot_is_consistent_and_serialisable() -> None:
    executor = ComExecutor(name="stats-test", default_timeout=5.0)
    try:
        executor.call(time.sleep, 0.02)
        stats = executor.stats()
        assert isinstance(stats, ExecutorStats)
        assert stats.name == "stats-test"
        assert stats.started is True
        assert stats.closed is False
        assert stats.calls == 1
        assert stats.last_duration_s is not None and stats.last_duration_s >= 0.02
        assert stats.total_busy_s >= stats.last_duration_s
        payload = stats.to_dict()
        assert payload["name"] == "stats-test"
        assert set(payload) == {
            "name",
            "started",
            "closed",
            "worker_thread_id",
            "calls",
            "failures",
            "timeouts",
            "rejected",
            "last_duration_s",
            "total_busy_s",
            "last_error",
            "current_operation",
            "operation_depth",
            "pending",
        }
    finally:
        executor.close()


def test_invalid_call_arguments_are_rejected() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        with pytest.raises(ValidationError):
            executor.call("not callable")  # type: ignore[arg-type]
        for bad in (0, -1, float("nan"), float("inf"), True, "1"):
            with pytest.raises(ValidationError):
                executor.call(lambda: None, timeout=bad)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            executor.operation("  ")
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# executor: exclusive operations
# ---------------------------------------------------------------------------
def test_operation_excludes_other_threads_until_it_exits() -> None:
    executor = ComExecutor(default_timeout=5.0)
    order: list[str] = []
    errors: list[BaseException] = []

    def other_thread_call() -> None:
        try:
            executor.call(lambda: order.append("other"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        with executor.operation("exclusive", timeout=5.0) as scope:
            executor.call(lambda: order.append("inside-1"))
            executor.call(lambda: order.append("inside-2"))
            thread = threading.Thread(target=other_thread_call)
            thread.start()
            time.sleep(0.1)
            # The other thread must still be blocked on the exclusivity gate.
            assert order == ["inside-1", "inside-2"]
            assert scope.calls == 2
            assert scope.depth == 1
            order.append("after-start")
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert errors == []
        assert order == ["inside-1", "inside-2", "after-start", "other"]
    finally:
        executor.close()


def test_operation_is_reentrant_for_its_owner() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        with executor.operation("outer", timeout=5.0):
            with executor.operation("inner", timeout=5.0) as inner:
                assert inner.depth == 2
                assert executor.call(lambda: 1) == 1
                assert executor.stats().operation_depth == 2
                assert executor.stats().current_operation == "inner"
            assert executor.stats().operation_depth == 1
            assert executor.stats().current_operation == "outer"
        assert executor.stats().operation_depth == 0
        assert executor.stats().current_operation is None
    finally:
        executor.close()


def test_operation_waits_for_another_threads_operation_or_times_out() -> None:
    executor = ComExecutor(default_timeout=5.0)
    holder_ready = threading.Event()
    release_holder = threading.Event()
    failures: list[BaseException] = []

    def hold() -> None:
        try:
            with executor.operation("holder", timeout=5.0):
                holder_ready.set()
                release_holder.wait(5.0)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holder_ready.wait(5.0)
        with pytest.raises(OperationTimeout), executor.operation("intruder", timeout=0.1):
            pytest.fail("a second thread must not enter a running operation")
        release_holder.set()
        holder.join(timeout=5.0)
        assert failures == []
        assert executor.stats().timeouts >= 1
    finally:
        release_holder.set()
        executor.close()


def test_nested_call_budget_is_capped_by_the_operation_deadline() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        started = time.monotonic()
        with pytest.raises(OperationTimeout), executor.operation("short", timeout=0.1):
            executor.call(time.sleep, 2.0, timeout=5.0)
        assert time.monotonic() - started < 1.5
    finally:
        executor.close()


def test_operation_scopes_must_be_exited_in_reverse_order() -> None:
    executor = ComExecutor(default_timeout=5.0)
    try:
        outer = executor.operation("outer", timeout=5.0)
        inner = executor.operation("inner", timeout=5.0)
        outer.__enter__()
        inner.__enter__()
        with pytest.raises(ValidationError):
            outer.__exit__(None, None, None)  # out of order: inner is still open
        inner.__exit__(None, None, None)
        outer.__exit__(None, None, None)
        assert executor.stats().operation_depth == 0
        with pytest.raises(ValidationError):
            outer.__enter__()
            outer.__enter__()  # already active
    finally:
        executor.close()


# ---------------------------------------------------------------------------
# executor: lifecycle
# ---------------------------------------------------------------------------
def test_initializer_and_finalizer_run_on_the_worker_thread() -> None:
    init_threads: list[int] = []
    fin_threads: list[int] = []
    executor = ComExecutor(
        default_timeout=5.0,
        initializer=lambda: init_threads.append(threading.get_ident()),
        finalizer=lambda: fin_threads.append(threading.get_ident()),
    )
    worker = executor.call(threading.get_ident)
    executor.close()
    assert init_threads == [worker]
    assert fin_threads == [worker]
    assert worker != threading.get_ident()
    assert executor.finalizer_error is None


def test_close_is_idempotent_and_rejects_later_calls() -> None:
    executor = ComExecutor(default_timeout=5.0)
    executor.call(lambda: 1)
    executor.close()
    executor.close()
    assert executor.is_closed is True
    with pytest.raises(ExecutorClosed):
        executor.call(lambda: 1)
    with pytest.raises(ExecutorClosed), executor.operation("late"):
        pass
    with pytest.raises(ExecutorClosed):
        executor.start()
    assert executor.stats().rejected >= 2


def test_initializer_failure_is_reported_as_backend_unavailable() -> None:
    fin_threads: list[int] = []

    def broken_initializer() -> None:
        raise RuntimeError("COM is not available")

    executor = ComExecutor(default_timeout=5.0, initializer=broken_initializer, finalizer=lambda: fin_threads.append(1))
    try:
        with pytest.raises(BackendUnavailable) as excinfo:
            executor.call(lambda: 1)
        assert "COM is not available" in repr(excinfo.value.details)
        # A second attempt reports the same failure instead of hanging.
        with pytest.raises(BackendUnavailable):
            executor.call(lambda: 1)
        assert fin_threads == [1]
    finally:
        executor.close()


def test_executor_works_as_a_context_manager() -> None:
    with ComExecutor(default_timeout=5.0) as executor:
        assert executor.call(lambda: 2) == 2
        assert executor.is_started is True
    assert executor.is_closed is True


# ---------------------------------------------------------------------------
# Win32 backend: import safety and pure snippet builders
# ---------------------------------------------------------------------------
def test_win32_module_does_not_import_pywin32_eagerly() -> None:
    # The module must be importable on a machine without pywin32: no COM import
    # may appear at module scope (it lives inside _import_pywin32()).
    source = Path(win32.__file__).read_text(encoding="utf-8")
    top_level = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert top_level, "the module should have imports"
    assert not any("pythoncom" in line or "win32com" in line for line in top_level)
    assert callable(win32.pythoncom_initializer)
    assert callable(win32.pythoncom_finalizer)


def test_missing_pywin32_becomes_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    backend = win32.Win32ComBackend()
    with pytest.raises(BackendUnavailable):
        backend.connect()
    assert backend.is_connected() is False


def test_create_snippet_never_deletes_an_existing_object() -> None:
    snippet = win32.build_create_statement("Station", "S1", x=1.5, y=2)
    assert "deleteObject" not in snippet
    # Measured: the CLASS is the receiver and the frame is argument 1. The
    # reverse form (frame.createObject(class, x, y)) fails on a real machine
    # with "argument 1 is not a frame, and not a folder".
    assert ".MaterialFlow.SingleProc.createObject(.Models.Model, 1.5, 2)" in snippet
    assert ".createObject(" in snippet
    # Measured: Coordinate3D is not writable on this build, and emitting it
    # made every create_object fail. Coordinates come from createObject.
    assert "Coordinate3D" not in snippet
    assert snippet.index("var psbridgeTarget") < snippet.index("psbridgeTarget :=")


def test_create_snippet_with_overwrite_is_explicitly_destructive() -> None:
    snippet = win32.build_create_statement("Station", "S1", overwrite=True)
    assert "deleteObject" in snippet


def test_create_snippet_rejects_unknown_classes_and_bad_names() -> None:
    with pytest.raises(TypeNotAllowed):
        win32.build_create_statement("Evil", "S1")
    with pytest.raises(ValidationError):
        win32.build_create_statement("Station", "bad name")


def test_table_snippets_use_one_based_column_row_indices() -> None:
    # SimTalk DataTables index as [column, row] and are 1-based.
    assert win32.build_table_cell_path(".Models.Model.T1", 1, 1) == ".Models.Model.T1[1,1]"
    assert win32.build_table_cell_path(".Models.Model.T1", 3, 4) == ".Models.Model.T1[4,3]"
    snippet = win32.build_table_write(".Models.Model.T1", 2, 3, 4.5)
    # Default TableFile cells are string-typed, so scalars go in via to_str.
    assert "psbridgeTable[3,2] := to_str(4.5);" in snippet
    with pytest.raises(ValidationError):
        win32.build_table_cell_path(".Models.Model.T1", 0, 1)


def test_table_write_escapes_strings_with_backslashes() -> None:
    snippet = win32.build_table_write(".Models.Model.T1", 1, 1, 'a"b\\c')
    assert ':= "a\\"b\\\\c";' in snippet


def test_table_resize_uses_column_row_and_cut_helpers() -> None:
    snippet = win32.build_table_resize(".Models.Model.T1", 3, 4)
    assert "cutRow(psbridgeTable.yDim);" in snippet
    assert "cutColumn(psbridgeTable.xDim);" in snippet
    assert 'psbridgeTable[4,3] := "";' in snippet
    assert "setDimensions" not in snippet


def test_controller_start_sets_the_end_period_in_seconds() -> None:
    # Measured: the attribute is End (a relative period, not an absolute stop
    # time) and it takes a plain number of seconds. There is no EndTime, and a
    # time literal like 0:01:30.5 is a syntax error in SimTalk source.
    snippet = win32.build_controller_start(".Models.Model.EventController", 90.5)
    assert "End := 90.5;" in snippet
    assert "EndTime" not in snippet
    assert ":" not in snippet.split("End :=")[1].split(";")[0]
    plain = win32.build_controller_start(".Models.Model.EventController")
    assert "End :=" not in plain
    assert ".start();" in plain


def test_exists_query_is_a_bare_expression() -> None:
    # Measured: GetValue evaluates an expression directly, and there is no
    # result attribute to write to on this build.
    exists = win32.build_exists_query(".Models.Model.S1")
    assert exists == 'existsObject(".Models.Model.S1")'
    assert "PsbridgeResult" not in exists


def test_connect_snippet_rejects_self_connection() -> None:
    with pytest.raises(ValidationError):
        win32.build_connect_statement(".Models.Model.S1", ".Models.Model.S1")


def test_method_source_write_uses_the_program_attribute() -> None:
    # Measured: the body lives in .Program. The documented .Source name is
    # not what the product accepts, so this asserts the measured spelling.
    with pytest.raises(ValidationError):
        win32.build_method_source_write(".Models.Model.M1", "a\x00b")
    snippet = win32.build_method_source_write(".Models.Model.M1", 'print "hi";')
    assert 'psbridgeMethod.Program := "print \\"hi\\";";' in snippet
    assert "Source" not in snippet


def test_multiline_method_bodies_take_the_file_route() -> None:
    # Measured: a SimTalk string literal has no newline escape, so assigning a
    # multi-line body to .Program is rejected as a syntax error. A body with a
    # param declaration always has a newline, which is why every parameterised
    # method failed to write. load(FileName) reads a text file instead and
    # round-tripped a five-line body unchanged on the real machine.
    assert win32.method_source_needs_file("print 1;") is False
    assert win32.method_source_needs_file("param a : integer\nprint a;") is True
    assert win32.method_source_needs_file("a;\r\nb;") is True
    snippet = win32.build_method_source_load(".Models.Model.M1", "C:\\tmp\\body.smt")
    assert ".load(" in snippet
    assert "Program :=" not in snippet


def test_method_handle_is_used_directly_not_through_str_to_obj() -> None:
    # A node handle is an expression, not a path; str_to_obj would reject it.
    handle = ".Models.Model.Frame.node(3)"
    snippet = win32.build_method_source_write(handle, "is do end;")
    assert "psbridgeMethod := .Models.Model.Frame.node(3);" in snippet
    assert "str_to_obj" not in snippet
    assert win32.build_method_source_read(handle) == "to_str(.Models.Model.Frame.node(3).Program)"
    by_path = win32.build_method_source_write(".Models.Model.M1", "is do end;")
    assert 'str_to_obj(".Models.Model.M1")' in by_path
    assert win32.build_method_source_read(".Models.Model.M1") == 'to_str(str_to_obj(".Models.Model.M1").Program)'


# ---------------------------------------------------------------------------
# Win32 backend: protocol logic against a canned COM stand-in
# ---------------------------------------------------------------------------
class _CannedApp:
    """Stand-in for the COM automation object (unit-test seam for parsing)."""

    def __init__(self, *, canned: list[Any] | None = None, values: dict[str, Any] | None = None) -> None:
        self.canned = list(canned or [])
        self.values = dict(values or {})
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.simtalk: list[str] = []
        self.saved: list[str] = []
        self.loaded: list[str] = []
        self.visible = False
        self.quit_called = False

    # -- helpers ---------------------------------------------------------
    def _record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.append((name, args))

    # -- COM surface -----------------------------------------------------
    def ExecuteSimTalk(self, source: str) -> None:
        self._record("ExecuteSimTalk", (source,))
        self.simtalk.append(source)

    def IsSimulationRunning(self) -> Any:
        self._record("IsSimulationRunning", ())
        if self.canned:
            return self.canned.pop(0)
        return False

    def StartSimulation(self, controller_path: str) -> None:
        self._record("StartSimulation", (controller_path,))

    def StopSimulation(self) -> None:
        self._record("StopSimulation", ())

    def GetValue(self, path: str) -> Any:
        self._record("GetValue", (path,))
        if path in self.values:
            return self.values[path]
        if self.canned:
            return self.canned.pop(0)
        raise RuntimeError("unknown value path: " + path)

    def SetNoMessageBox(self, flag: bool) -> None:
        self._record("SetNoMessageBox", (flag,))

    def SetValue(self, path: str, value: Any) -> None:
        self._record("SetValue", (path, value))
        self.values[path] = value

    def LoadModel(self, path: str) -> None:
        self._record("LoadModel", (path,))
        self.loaded.append(path)

    def SaveModel(self, path: str) -> None:
        self._record("SaveModel", (path,))
        self.saved.append(path)
        Path(path).write_text("fake model", encoding="utf-8")

    def NewModel(self) -> None:
        self._record("NewModel", ())

    def CloseModel(self) -> None:
        self._record("CloseModel", ())

    def SetVisible(self, flag: bool) -> None:
        self._record("SetVisible", (flag,))
        self.visible = bool(flag)

    def Quit(self) -> None:
        self._record("Quit", ())
        self.quit_called = True


def _attached(app: _CannedApp, **kwargs: Any) -> win32.Win32ComBackend:
    """Attach a backend to the stand-in without touching pywin32."""
    backend = win32.Win32ComBackend(**kwargs)
    backend._app = app
    backend._connected = True
    return backend


def test_object_exists_reads_the_expression_result() -> None:
    # Measured: GetValue returns the value of the expression it is given, and
    # existsObject answers true/false (it is NOT reliable for class paths).
    app = _CannedApp(canned=["true", "false"])
    backend = _attached(app)
    assert backend.object_exists(".Models.Model.S1") is True
    assert backend.object_exists(".Models.Model.S2") is False
    # Nothing was executed: the answer comes from one GetValue.
    assert app.simtalk == []
    app.canned.append("maybe")
    with pytest.raises(BackendError):
        backend.object_exists(".Models.Model.S3")


def _list_app(rows: list[tuple[str, str]]) -> _CannedApp:
    """A stand-in that answers the per-node listing expressions."""
    frame = ".Models.Model"
    values: dict[str, Any] = {"to_str(" + frame + ".NumNodes);": str(len(rows))}
    for index, (name, class_path) in enumerate(rows, start=1):
        node = frame + ".node(" + str(index) + ")"
        values["to_str(" + node + ".Name);"] = name
        values["to_str(" + node + ".Class);"] = class_path
    return _CannedApp(values=values)


def test_list_objects_reads_each_node_directly() -> None:
    app = _list_app([("S1", ".MaterialFlow.SingleProc"), ("S2", ".MaterialFlow.Buffer")])
    backend = _attached(app)
    objects = backend.list_objects()
    assert [obj.name for obj in objects] == ["S1", "S2"]
    assert [obj.class_name for obj in objects] == [".MaterialFlow.SingleProc", ".MaterialFlow.Buffer"]
    assert objects[0].path == ".Models.Model.S1"
    # Measured: listing needs no scratch object -- the TableFile version was
    # rejected by the product, so nothing may be created in the user's frame.
    assert app.simtalk == []


def test_list_objects_rejects_an_unparsable_count() -> None:
    frame = ".Models.Model"
    malformed = _attached(_CannedApp(values={"to_str(" + frame + ".NumNodes);": "not-a-number"}))
    with pytest.raises(BackendError):
        malformed.list_objects()


def test_empty_frame_lists_as_empty() -> None:
    # Measured: an empty frame really reports 0 children, so an empty listing
    # is the truth rather than something to hide behind a flag.
    backend = _attached(_list_app([]))
    assert backend.list_objects() == ()


def test_save_and_clone_track_the_model_path(tmp_path: Path) -> None:
    app = _CannedApp()
    backend = _attached(app)
    first = str(tmp_path / "model.spp")
    backend.save_model(first)
    assert app.saved == [first]
    assert backend.model_path == first

    clone = str(tmp_path / "clone.spp")
    backend.clone_model(clone)
    assert app.saved == [first, clone]
    assert app.loaded == [clone]
    assert backend.model_path == clone


def test_clone_requires_a_saved_model_and_a_different_target(tmp_path: Path) -> None:
    backend = _attached(_CannedApp())
    with pytest.raises(BackendError):
        backend.clone_model(str(tmp_path / "clone.spp"))
    target = str(tmp_path / "model.spp")
    backend.save_model(target)
    with pytest.raises(ValidationError):
        backend.clone_model(target)


def test_disconnect_never_saves_the_model(tmp_path: Path) -> None:
    app = _CannedApp()
    backend = _attached(app, quit_on_disconnect=True)
    backend.save_model(str(tmp_path / "model.spp"))
    backend.disconnect()
    assert app.saved == [str(tmp_path / "model.spp")]
    assert app.quit_called is True
    assert backend.is_connected() is False
    with pytest.raises(BackendUnavailable):
        backend.object_exists(".Models.Model.S1")


def test_set_value_verifies_the_read_back() -> None:
    app = _CannedApp(values={".Models.Model.S1.Capacity": 5})
    backend = _attached(app)
    assert backend.set_value(".Models.Model.S1", "Capacity", 5) == 5

    class _LyingApp(_CannedApp):
        def SetValue(self, path: str, value: Any) -> None:
            self._record("SetValue", (path, value))

    lying = _attached(_LyingApp(values={".Models.Model.S1.Capacity": 7}))
    with pytest.raises(VerificationFailed):
        lying.set_value(".Models.Model.S1", "Capacity", 5)


def test_set_value_writes_time_attributes_as_seconds() -> None:
    # Measured: SetValue cannot write a time attribute at all -- every string
    # spelling is rejected as "not a time". SimTalk with a numeric literal in
    # SECONDS works, and a time literal like 4:00 is a syntax error.
    app = _CannedApp(values={".Models.Model.S1.procTime": 1.5})
    backend = _attached(app)
    assert backend.set_value(".Models.Model.S1", "procTime", 1.5) == 1.5
    assert any("procTime := 1.5;" in source for source in app.simtalk)
    assert not any("0:00:01.500" in source for source in app.simtalk)
    assert not any(name == "SetValue" for name, _ in app.calls)


def test_create_object_checks_existence_before_creating() -> None:
    app = _CannedApp(canned=["false", "true"])
    backend = _attached(app)
    info = backend.create_object("Station", "S1", x=1, y=2)
    assert info.path == ".Models.Model.S1"
    assert info.class_name == ".MaterialFlow.SingleProc"
    # Existence is the only proof of success: creation reports nothing, and
    # NumChildren would stay 0 even with the object present.
    assert any(".MaterialFlow.SingleProc.createObject(" in source for source in app.simtalk)

    duplicate = _attached(_CannedApp(canned=["true"]))
    with pytest.raises(ObjectExists):
        duplicate.create_object("Station", "S1")
    # Only the existence probe ran: nothing was created and nothing deleted.
    assert duplicate._app.simtalk == []


def test_delete_object_verifies_the_deletion() -> None:
    backend = _attached(_CannedApp(canned=["true", "false"]))
    backend.delete_object(".Models.Model.S1")

    lingering = _attached(_CannedApp(canned=["true", "true"]))
    with pytest.raises(BackendError):
        lingering.delete_object(".Models.Model.S1")

    missing = _attached(_CannedApp(canned=["false"]))
    with pytest.raises(ObjectNotFound):
        missing.delete_object(".Models.Model.S2")


def test_status_parses_controller_attributes() -> None:
    # Measured names: simTime / isRunning / End. There is no EndTime, and End is
    # a RELATIVE period, so "sim_time >= end_time" still means "ran to the end".
    finished_app = _CannedApp(
        values={
            ".Models.Model.EventController.simTime": 10.0,
            ".Models.Model.EventController.isRunning": False,
            ".Models.Model.EventController.End": 10.0,
        }
    )
    status = _attached(finished_app).status()
    assert status.running is False
    assert status.finished is True
    assert status.state == "finished"
    assert status.sim_time == 10.0

    running_app = _CannedApp(
        values={
            ".Models.Model.EventController.simTime": 3.0,
            ".Models.Model.EventController.isRunning": True,
            ".Models.Model.EventController.End": 10.0,
        }
    )
    running = _attached(running_app).status()
    assert running.running is True and running.finished is False and running.state == "running"

    bad_app = _CannedApp(values={".Models.Model.EventController.simTime": "early"})
    with pytest.raises(BackendError):
        _attached(bad_app).status()


def test_connect_objects_uses_the_documented_connector_form() -> None:
    # Measured: the receiver is the Connector CLASS. Success is proven by
    # reading NumPred back, because the SimTalk call returns nothing.
    app = _CannedApp(canned=["1"])
    backend = _attached(app)
    backend.connect_objects(".Models.Model.S1", ".Models.Model.S2")
    joined = "\n".join(app.simtalk)
    assert ".MaterialFlow.Connector.connect(" in joined
    assert "str_to_obj(\".Models.Model.S1\")" in joined
    assert any(name == "GetValue" and "NumPred" in str(args[0]) for name, args in app.calls)

    # A link that did not take is an error, not a silent success.
    failed = _attached(_CannedApp(canned=["0"]))
    with pytest.raises(BackendError):
        failed.connect_objects(".Models.Model.S1", ".Models.Model.S2")


def test_connect_statement_rejects_self_connection() -> None:
    backend = _attached(_CannedApp())
    with pytest.raises(ValidationError):
        backend.connect_objects(".Models.Model.S1", ".Models.Model.S1")


def test_wait_until_finished_polls_is_running() -> None:
    """Measured: native IsSimulationRunning() agrees with the SimTalk
    <controller>.isRunning attribute in every state checked, and needs no
    object path, so polling uses it directly instead of building GetValue
    expressions."""
    app = _CannedApp(canned=[True, True, False])
    backend = _attached(app)
    assert backend.wait_until_finished(timeout=5.0, poll=0.0) is True
    assert sum(1 for name, _ in app.calls if name == "IsSimulationRunning") == 3
    assert not [name for name, _ in app.calls if name == "GetValue"]

    stuck = _CannedApp(canned=[True, True, True])
    assert _attached(stuck).wait_until_finished(timeout=0.0, poll=0.0) is False


def test_event_log_is_not_silently_empty() -> None:
    backend = _attached(_CannedApp())
    with pytest.raises(BackendUnavailable):
        backend.event_log()
    with pytest.raises(ValidationError):
        backend.event_log(-1)


def test_read_table_cell_and_range_budget() -> None:
    app = _CannedApp(values={".Models.Model.T1[3,2]": 42})
    backend = _attached(app, max_table_cells=3)
    assert backend.read_table_cell(".Models.Model.T1", 2, 3) == 42
    with pytest.raises(ValidationError):
        backend.read_table_range(".Models.Model.T1", 1, 1, 2, 2)
    with pytest.raises(ValidationError):
        backend.read_table_range(".Models.Model.T1", 2, 2, 1, 1)


def test_model_root_is_enforced_before_any_com_call(tmp_path: Path) -> None:
    backend = _attached(_CannedApp(), model_root=tmp_path)
    outside = str(tmp_path.parent / "elsewhere.spp")
    with pytest.raises(PathNotAllowed):
        backend.load_model(outside)
    with pytest.raises(BackendError):
        backend.load_model(str(tmp_path / "missing.spp"))


def test_load_model_requires_an_existing_file(tmp_path: Path) -> None:
    backend = _attached(_CannedApp())
    with pytest.raises(BackendError):
        backend.load_model(str(tmp_path / "nope.spp"))
    with pytest.raises(ValidationError):
        backend.load_model("")
    with pytest.raises(ValidationError):
        backend.load_model("bad\x00path.spp")


def test_disconnect_quits_only_an_instance_the_backend_started() -> None:
    # Default (quit_on_disconnect=None): quit only what connect() started.
    started = _attached(_CannedApp())
    started._owns_instance = True
    started.disconnect()
    assert started._app is None
    assert started._last_error is None

    attached_only = _attached(_CannedApp())
    app = attached_only._app
    attached_only.disconnect()
    assert app is not None
    assert app.quit_called is False  # the user's own session is never closed

    never = _attached(_CannedApp(), quit_on_disconnect=False)
    never._owns_instance = True
    never.disconnect()
    assert never._app is None


def test_quit_on_disconnect_rejects_non_boolean() -> None:
    with pytest.raises(ValidationError):
        win32.Win32ComBackend(quit_on_disconnect="yes")  # type: ignore[arg-type]


class _FakePythoncom:
    """Records CoInitialize/CoUninitialize calls without touching COM."""

    def __init__(self) -> None:
        self.initialised = 0
        self.uninitialised = 0

    def CoInitialize(self) -> None:
        self.initialised += 1

    def CoUninitialize(self) -> None:
        self.uninitialised += 1


class _FakeClient:
    """Stand-in for win32com.client."""

    def __init__(self, app: Any, *, running: Any = None) -> None:
        self.app = app
        self.running = running

    def GetActiveObject(self, prog_id: str) -> Any:
        if self.running is None:
            raise RuntimeError("no running object")
        return self.running

    def Dispatch(self, prog_id: str) -> Any:
        return self.app


def test_connect_and_disconnect_manage_com_initialisation(monkeypatch: pytest.MonkeyPatch) -> None:
    pythoncom = _FakePythoncom()
    app = _CannedApp()
    monkeypatch.setattr(win32, "_import_pywin32", lambda: (pythoncom, _FakeClient(app)))
    backend = win32.Win32ComBackend()
    backend.connect()
    try:
        assert backend.is_connected() is True
        assert backend.owns_instance is True
        assert pythoncom.initialised == 1
    finally:
        backend.disconnect()
    assert pythoncom.uninitialised == 1
    assert app.quit_called is True  # the instance this backend started is closed


def test_connect_quits_the_instance_it_started_when_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenApp(_CannedApp):
        def SetVisible(self, flag: bool) -> None:
            raise RuntimeError("cannot show the window")

    pythoncom = _FakePythoncom()
    app = _BrokenApp()
    monkeypatch.setattr(win32, "_import_pywin32", lambda: (pythoncom, _FakeClient(app)))
    backend = win32.Win32ComBackend(visible=True)
    with pytest.raises(BackendUnavailable):
        backend.connect()
    assert backend.is_connected() is False
    assert backend.owns_instance is False
    assert app.quit_called is True  # no half-connected hidden instance is left behind
    assert pythoncom.uninitialised == 1


def test_acquire_app_prefers_a_running_instance() -> None:
    class _Client:
        def __init__(self, running: Any) -> None:
            self.running = running
            self.dispatched = 0

        def GetActiveObject(self, prog_id: str) -> Any:
            if self.running is None:
                raise RuntimeError("no running object")
            return self.running

        def Dispatch(self, prog_id: str) -> Any:
            self.dispatched += 1
            return "started-app"

    backend = win32.Win32ComBackend()
    assert backend._acquire_app(_Client("running-app")) == ("running-app", True)
    assert backend._acquire_app(_Client(None)) == ("started-app", False)

    no_preference = win32.Win32ComBackend(prefer_running_instance=False)
    client = _Client("running-app")
    assert no_preference._acquire_app(client) == ("started-app", False)
    assert client.dispatched == 1


@pytest.mark.skipif(
    os.environ.get("PSBRIDGE_REAL_COM") != "1",
    reason="set PSBRIDGE_REAL_COM=1 to start a real Plant Simulation instance",
)
def test_connect_against_a_real_installation() -> None:
    """Opt-in: starts and quits a hidden instance, never touching a user session."""
    pytest.importorskip("pythoncom")
    backend = win32.Win32ComBackend(prefer_running_instance=False, quit_on_disconnect=True)
    backend.connect()
    try:
        assert backend.is_connected() is True
        assert backend.owns_instance is True
        assert backend.attached_to_running is False
        assert backend.capabilities.kind == "win32"
    finally:
        backend.disconnect()
    assert backend.is_connected() is False
    assert backend.owns_instance is False


def _discover_demo_spp() -> Path | None:
    env_path = os.environ.get("PSBRIDGE_TEMPLATE")
    candidates = [Path(env_path)] if env_path else []
    candidates.extend(
        [
            Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
            Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


@pytest.mark.skipif(
    os.environ.get("PSBRIDGE_REAL_COM") != "1",
    reason="set PSBRIDGE_REAL_COM=1 to run live Win32 table and SimTalk escaping checks",
)
def test_live_win32_table_resize_escaping_and_state_hash(tmp_path: Path) -> None:
    """Opt-in: exercises TableFile [col, row], resize shrink/grow, escaping, and state_hash."""
    pytest.importorskip("pythoncom")
    demo = _discover_demo_spp()
    if demo is None:
        pytest.skip("no demo .spp model discovered for live COM table/escaping check")
    work_model = tmp_path / "win32_live_probe.spp"
    backend = win32.Win32ComBackend(prefer_running_instance=False, quit_on_disconnect=True)
    backend.connect()
    try:
        backend.load_model(str(demo))
        backend.clone_model(str(work_model))
        frame = backend.default_frame_path
        tbl_name = "PsbridgeLiveTbl"
        tbl_path = frame + "." + tbl_name
        backend.execute_simtalk(
            f'var t : object;\n'
            f't := .InformationFlow.&TableFile.createObject({frame}, 100, 100);\n'
            f't.Name := "{tbl_name}";\n'
        )
        backend.set_table_size(tbl_path, 3, 4)
        backend.write_table_cell(tbl_path, 3, 4, 'a"b\\c')
        backend.write_table_cell(tbl_path, 2, 1, True)
        backend.write_table_cell(tbl_path, 1, 2, 4.5)
        assert backend.read_table_cell(tbl_path, 3, 4) == 'a"b\\c'
        grid = backend.read_table_range(tbl_path, 1, 1, 3, 4)
        assert grid[2][3] == 'a"b\\c'
        assert grid[1][0] == "true"
        assert grid[0][1] == "4.5"

        # Shrink sparse table and grow again to verify sparse cutRow/cutColumn behavior
        backend.set_table_size(tbl_path, 2, 2)
        assert int(float(str(backend.evaluate(f"to_str({tbl_path}.yDim)")))) == 2
        assert int(float(str(backend.evaluate(f"to_str({tbl_path}.xDim)")))) == 2

        # Method creation + backslash/quote/multiline round-trip
        meth = backend.create_method_object("PsbridgeLiveMeth", frame)
        body = 'param msg : string\n:string is\ndo\n  return "a\\"b\\\\c";\nend;'
        backend.set_method_source(meth, body)
        assert backend.get_method_source(meth) == body
        assert len(backend.state_hash()) == 64
    finally:
        backend.disconnect()
