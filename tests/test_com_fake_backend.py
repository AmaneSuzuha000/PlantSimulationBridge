"""Tests for the in-memory fake backend (the reference SimBackend implementation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from psbridge.com.fake import (
    FAKE_MODEL_FORMAT,
    FAKE_MODEL_VERSION,
    FakeBackend,
    FakeFault,
)
from psbridge.errors import (
    BackendError,
    BackendUnavailable,
    ModelMutationDenied,
    ObjectExists,
    ObjectNotFound,
    SimTalkError,
    SimulationError,
    TypeNotAllowed,
    ValidationError,
    VerificationFailed,
)

FRAME = ".Models.Model"


@pytest.fixture()
def backend() -> FakeBackend:
    """Connected fake backend with an empty model."""
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    return fake


def _write_model(path: Path, objects: list[dict[str, Any]] | None = None) -> Path:
    payload = {
        "format": FAKE_MODEL_FORMAT,
        "version": FAKE_MODEL_VERSION,
        "objects": objects or [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def test_calls_before_connect_raise_backend_unavailable() -> None:
    fake = FakeBackend()
    with pytest.raises(BackendUnavailable):
        fake.new_model()
    with pytest.raises(BackendUnavailable):
        fake.list_objects()
    with pytest.raises(BackendUnavailable):
        fake.execute_simtalk("var x : integer;")


def test_connect_and_disconnect_are_idempotent_and_never_save() -> None:
    fake = FakeBackend()
    fake.connect()
    fake.connect()
    assert fake.connect_count == 1
    fake.disconnect()
    fake.disconnect()
    assert fake.disconnect_count == 1
    assert fake.save_count == 0
    assert fake.is_connected() is False


def test_new_model_clears_objects_and_path() -> None:
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    fake.create_object("Station", "S1")
    fake.new_model()
    assert fake.list_objects() == ()
    assert fake.model_path is None


def test_save_requires_a_model_and_an_existing_directory(tmp_path: Path) -> None:
    fake = FakeBackend()
    fake.connect()
    with pytest.raises(ObjectNotFound):
        fake.save_model(str(tmp_path / "model.spp"))
    fake.new_model()
    with pytest.raises(BackendError):
        fake.save_model(str(tmp_path / "missing" / "model.spp"))


def test_save_and_load_round_trip_preserves_content(tmp_path: Path) -> None:
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    fake.create_object("Station", "S1", x=1.0, y=2.0)
    fake.create_object("Buffer", "B1")
    fake.connect_objects(FRAME + ".S1", FRAME + ".B1")
    fake.set_value(FRAME + ".S1", "procTime", 2.5)
    before = fake.model_content_hash()
    target = tmp_path / "model.spp"
    fake.save_model(str(target))
    assert fake.model_path == str(target)
    assert fake.save_count == 1
    assert target.is_file()
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["format"] == FAKE_MODEL_FORMAT
    assert document["version"] == FAKE_MODEL_VERSION

    other = FakeBackend()
    other.connect()
    other.load_model(str(target))
    assert other.load_count == 1
    assert other.model_content_hash() == before
    assert other.get_value(FRAME + ".S1", "procTime") == 2.5
    # The saved document is canonicalised (objects sorted by path), so the
    # reloaded frame lists B1 before S1; the set of objects is what matters.
    assert sorted(obj.name for obj in other.list_objects()) == ["B1", "S1"]


def test_load_model_rejects_missing_and_malformed_files(tmp_path: Path) -> None:
    fake = FakeBackend()
    fake.connect()
    with pytest.raises(BackendError):
        fake.load_model(str(tmp_path / "nope.spp"))

    bad_json = tmp_path / "bad.spp"
    bad_json.write_text("not json", encoding="utf-8")
    with pytest.raises(BackendError):
        fake.load_model(str(bad_json))

    wrong_format = tmp_path / "wrong.spp"
    wrong_format.write_text(json.dumps({"format": "other", "version": 1}), encoding="utf-8")
    with pytest.raises(BackendError):
        fake.load_model(str(wrong_format))

    wrong_version = tmp_path / "v2.spp"
    wrong_version.write_text(json.dumps({"format": FAKE_MODEL_FORMAT, "version": 99}), encoding="utf-8")
    with pytest.raises(BackendError):
        fake.load_model(str(wrong_version))

    with pytest.raises(ValidationError):
        fake.load_model("")


def test_close_model_clears_the_model(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.close_model()
    assert backend.model_path is None
    with pytest.raises(ObjectNotFound):
        backend.list_objects()


def test_clone_model_requires_a_saved_model_and_preserves_content(tmp_path: Path) -> None:
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    with pytest.raises(BackendError):
        fake.clone_model(str(tmp_path / "clone.spp"))
    fake.create_object("Station", "S1")
    fake.save_model(str(tmp_path / "model.spp"))
    content = fake.model_content_hash()
    fake.clone_model(str(tmp_path / "clone.spp"))
    assert fake.clone_count == 1
    assert fake.model_path == str(tmp_path / "clone.spp")
    assert fake.model_content_hash() == content
    with pytest.raises(ValidationError):
        fake.clone_model(str(tmp_path / "clone.spp"))


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------
def test_listing_requires_a_loaded_model_and_a_known_frame() -> None:
    fake = FakeBackend()
    fake.connect()
    with pytest.raises(ObjectNotFound):
        fake.list_objects()
    fake.new_model()
    with pytest.raises(ObjectNotFound):
        fake.list_objects(".Models.Other")


def test_create_object_never_replaces_an_existing_object(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1", x=1.0, y=2.0)
    backend.set_value(FRAME + ".S1", "procTime", 3.0)
    with pytest.raises(ObjectExists) as excinfo:
        backend.create_object("Buffer", "S1")
    assert excinfo.value.details["class_name"] == ".MaterialFlow.SingleProc"
    info = backend.get_object_info(FRAME + ".S1")
    assert info.class_name == ".MaterialFlow.SingleProc"
    assert backend.get_value(FRAME + ".S1", "procTime") == 3.0
    assert backend.delete_count == 0


def test_overwrite_reconfigures_in_place_without_deleting(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    info = backend.create_object("Buffer", "S1", overwrite=True)
    assert info.class_name == ".MaterialFlow.Buffer"
    assert backend.delete_count == 0
    assert backend.create_count == 1
    kinds = [event.kind for event in backend.event_log()]
    assert "object_reconfigured" in kinds


def test_create_object_validates_class_name_and_coordinates(backend: FakeBackend) -> None:
    with pytest.raises(TypeNotAllowed):
        backend.create_object("Evil", "S1")
    with pytest.raises(ValidationError):
        backend.create_object("Station", "bad name")
    with pytest.raises(ValidationError):
        backend.create_object("Station", "S1", x=float("nan"))


def test_delete_object_protects_the_root_frame_and_cleans_connectors(backend: FakeBackend) -> None:
    with pytest.raises(ModelMutationDenied):
        backend.delete_object(FRAME)
    with pytest.raises(ObjectNotFound):
        backend.delete_object(FRAME + ".Nope")
    backend.create_object("Station", "S1")
    backend.create_object("Buffer", "B1")
    backend.connect_objects(FRAME + ".S1", FRAME + ".B1")
    backend.delete_object(FRAME + ".S1")
    assert backend.delete_count == 1
    info = backend.get_object_info(FRAME + ".B1")
    assert info.predecessors == ()
    assert backend.object_exists(FRAME + ".S1") is False


def test_connect_objects_validates_endpoints(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.create_object("Buffer", "B1")
    with pytest.raises(ValidationError):
        backend.connect_objects(FRAME + ".S1", FRAME + ".S1")
    with pytest.raises(ObjectNotFound):
        backend.connect_objects(FRAME + ".S1", FRAME + ".Nope")
    backend.connect_objects(FRAME + ".S1", FRAME + ".B1")
    with pytest.raises(BackendError):
        backend.connect_objects(FRAME + ".S1", FRAME + ".B1")
    source = backend.get_object_info(FRAME + ".S1")
    assert source.successors == (FRAME + ".B1",)


def test_get_object_info_reports_the_root_frame(backend: FakeBackend) -> None:
    info = backend.get_object_info(FRAME)
    assert info.class_name == ".Models.Frame"
    assert info.name == "Model"


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------
def test_property_access_validates_names_and_types(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    with pytest.raises(ObjectNotFound):
        backend.get_value(FRAME + ".Nope", "procTime")
    with pytest.raises(TypeNotAllowed):
        backend.get_value(FRAME + ".S1", "Capacity")
    with pytest.raises(ValidationError):
        backend.get_value(FRAME + ".S1", "bad-name")
    with pytest.raises(ValidationError):
        backend.set_value(FRAME + ".S1", "procTime", -1.0)
    with pytest.raises(ValidationError):
        backend.set_value(FRAME + ".S1", "procTime", "later")


def test_set_value_normalises_and_verifies(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    assert backend.set_value(FRAME + ".S1", "procTime", 2) == 2.0
    assert backend.get_value(FRAME + ".S1", "procTime") == 2.0
    backend.create_object("ParallelProc", "P1")
    assert backend.set_value(FRAME + ".P1", "XDim", 4) == 4


def test_silent_fault_is_caught_by_the_read_back(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.set_value(FRAME + ".S1", "procTime", 1.0)
    backend.inject_fault(FakeFault("set_value", target="procTime", silent=True))
    with pytest.raises(VerificationFailed) as excinfo:
        backend.set_value(FRAME + ".S1", "procTime", 9.0)
    assert excinfo.value.details["silent_fault"] is True
    assert backend.get_value(FRAME + ".S1", "procTime") == 1.0
    events = [event for event in backend.event_log() if event.kind == "value_set"]
    assert events[-1].payload["applied"] is False


def test_verify_false_returns_the_stored_value_without_raising(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.inject_fault(FakeFault("set_value", target="procTime", silent=True))
    assert backend.set_value(FRAME + ".S1", "procTime", 9.0, verify=False) == 0.0


def test_loud_fault_raises_simtalk_error_and_changes_nothing(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.inject_fault(FakeFault("set_value", target="procTime"))
    with pytest.raises(SimTalkError) as excinfo:
        backend.set_value(FRAME + ".S1", "procTime", 9.0)
    assert excinfo.value.details["injected"] is True
    backend.clear_faults()
    assert backend.get_value(FRAME + ".S1", "procTime") == 0.0


# ---------------------------------------------------------------------------
# methods and SimTalk
# ---------------------------------------------------------------------------
def test_method_source_round_trip(backend: FakeBackend) -> None:
    backend.create_object("Method", "M1")
    assert backend.get_method_source(FRAME + ".M1") == ""
    backend.set_method_source(FRAME + ".M1", "var i : integer;\ni := 1;\n")
    assert backend.get_method_source(FRAME + ".M1") == "var i : integer;\ni := 1;\n"
    with pytest.raises(ValidationError):
        backend.set_method_source(FRAME + ".M1", "bad\x00source")


def test_method_calls_require_a_method_object(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    with pytest.raises(TypeNotAllowed):
        backend.get_method_source(FRAME + ".S1")
    with pytest.raises(TypeNotAllowed):
        backend.set_method_source(FRAME + ".S1", "var x : integer;")
    with pytest.raises(TypeNotAllowed):
        backend.execute_method(FRAME + ".S1")


def test_execute_method_counts_executions(backend: FakeBackend) -> None:
    backend.create_object("Method", "M1")
    result = backend.execute_method(FRAME + ".M1")
    assert result["executions"] == 1
    assert backend.execute_method(FRAME + ".M1")["executions"] == 2


def test_execute_simtalk_is_logged_and_validated(backend: FakeBackend) -> None:
    result = backend.execute_simtalk("var x : integer;\nx := 1;\n")
    assert result["statements"] == 2
    assert backend.simtalk_log == ("var x : integer;\nx := 1;\n",)
    with pytest.raises(ValidationError):
        backend.execute_simtalk("")
    with pytest.raises(ValidationError):
        backend.execute_simtalk("bad\x00source")
    backend.inject_fault(FakeFault("execute_simtalk"))
    with pytest.raises(SimTalkError):
        backend.execute_simtalk("var x : integer;")


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
def test_table_cells_are_normalised_and_bounds_checked(backend: FakeBackend) -> None:
    backend.create_object("DataTable", "T1")
    assert backend.read_table_cell(FRAME + ".T1", 1, 1) is None
    backend.write_table_cell(FRAME + ".T1", 1, 1, 4)
    assert backend.read_table_cell(FRAME + ".T1", 1, 1) == 4.0
    backend.write_table_cell(FRAME + ".T1", 1, 1, "text")
    assert backend.read_table_cell(FRAME + ".T1", 1, 1) == "text"
    with pytest.raises(ValidationError):
        backend.read_table_cell(FRAME + ".T1", 2, 1)
    with pytest.raises(ValidationError):
        backend.write_table_cell(FRAME + ".T1", 1, 1, ["nested"])


def test_table_resize_preserves_existing_cells(backend: FakeBackend) -> None:
    backend.create_object("DataTable", "T1")
    backend.write_table_cell(FRAME + ".T1", 1, 1, "keep")
    backend.set_table_size(FRAME + ".T1", 3, 2)
    assert backend.read_table_cell(FRAME + ".T1", 1, 1) == "keep"
    assert backend.read_table_cell(FRAME + ".T1", 3, 2) is None
    with pytest.raises(ValidationError):
        backend.set_table_size(FRAME + ".T1", 0, 1)


def test_table_range_reads_and_budget(backend: FakeBackend) -> None:
    backend.create_object("DataTable", "T1")
    backend.set_table_size(FRAME + ".T1", 2, 2)
    backend.write_table_cell(FRAME + ".T1", 2, 2, 7)
    assert backend.read_table_range(FRAME + ".T1", 1, 1, 2, 2) == ((None, None), (None, 7.0))
    with pytest.raises(ValidationError):
        backend.read_table_range(FRAME + ".T1", 2, 2, 1, 1)
    with pytest.raises(ValidationError):
        backend.read_table_range(FRAME + ".T1", 1, 1, 6, 6)


def test_table_calls_require_a_table_object(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    with pytest.raises(TypeNotAllowed):
        backend.read_table_cell(FRAME + ".S1", 1, 1)


def test_table_cell_budget_is_enforced_on_ranges(tmp_path: Path) -> None:
    # A model written by a gateway with a larger budget can hold a bigger table;
    # the reader must still refuse a range above its own budget.
    writer = FakeBackend()
    writer.connect()
    writer.new_model()
    writer.create_object("DataTable", "T1")
    writer.set_table_size(FRAME + ".T1", 3, 3)
    writer.save_model(str(tmp_path / "big.spp"))

    reader = FakeBackend(max_table_cells=3)
    reader.connect()
    reader.load_model(str(tmp_path / "big.spp"))
    assert reader.read_table_range(FRAME + ".T1", 1, 1, 1, 3) == ((None, None, None),)
    with pytest.raises(ValidationError):
        reader.read_table_range(FRAME + ".T1", 1, 1, 2, 2)
    with pytest.raises(ValidationError):
        reader.set_table_size(FRAME + ".T1", 2, 2)


# ---------------------------------------------------------------------------
# simulation control
# ---------------------------------------------------------------------------
def test_simulation_state_machine(backend: FakeBackend) -> None:
    with pytest.raises(SimulationError):
        backend.stop()
    backend.start(end_time=10.0)
    with pytest.raises(SimulationError):
        backend.start()
    assert backend.status().running is True
    assert backend.status().state == "running"
    backend.stop()
    assert backend.status().state == "stopped"


def test_advance_finishes_exactly_at_end_time(backend: FakeBackend) -> None:
    backend.start(end_time=10.0)
    assert backend.advance(4.0) == 4.0
    assert backend.advance(100.0) == 10.0
    status = backend.status()
    assert status.finished is True
    assert status.running is False
    assert status.state == "finished"
    assert status.end_time == 10.0
    assert [event.kind for event in backend.event_log()][-1] == "sim_finished"


def test_advance_requires_a_running_simulation(backend: FakeBackend) -> None:
    with pytest.raises(SimulationError):
        backend.advance(1.0)
    backend.start()
    with pytest.raises(ValidationError):
        backend.advance(0)


def test_start_rejects_an_end_time_in_the_past(backend: FakeBackend) -> None:
    backend.start(end_time=10.0)
    backend.advance(10.0)
    with pytest.raises(ValidationError):
        backend.start(end_time=5.0)


def test_reset_restores_the_initial_simulation_state(backend: FakeBackend) -> None:
    backend.start(end_time=10.0)
    backend.advance(5.0)
    backend.reset()
    status = backend.status()
    assert status.sim_time == 0.0
    assert status.running is False
    assert status.state == "reset"


def test_status_before_a_model_is_no_model() -> None:
    fake = FakeBackend()
    fake.connect()
    assert fake.status().state == "no_model"


# ---------------------------------------------------------------------------
# events and hashes
# ---------------------------------------------------------------------------
def test_event_log_is_ordered_and_filterable(backend: FakeBackend) -> None:
    backend.create_object("Station", "S1")
    backend.create_object("Station", "S2")
    events = backend.event_log()
    assert [event.seq for event in events] == sorted(event.seq for event in events)
    assert [event.kind for event in events][-2:] == ["object_created", "object_created"]
    tail = backend.event_log(since_seq=events[-2].seq)
    assert [event.kind for event in tail] == ["object_created"]
    with pytest.raises(ValidationError):
        backend.event_log(-1)
    with pytest.raises(ValidationError):
        backend.event_log(True)  # type: ignore[arg-type]


def test_state_hash_tracks_structure_while_content_hash_does_not(tmp_path: Path) -> None:
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    initial = fake.state_hash()
    fake.create_object("Station", "S1")
    changed = fake.state_hash()
    assert changed != initial
    fake.save_model(str(tmp_path / "a.spp"))
    content = fake.model_content_hash()
    saved_state = fake.state_hash()
    fake.save_model(str(tmp_path / "b.spp"))
    assert fake.model_content_hash() == content
    assert fake.state_hash() != saved_state  # model_path is part of the state
    assert fake.capabilities.kind == "fake"
