"""Tests for SafeOps, the verified layer every tool wrapper must go through."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from psbridge.com.backend import ObjectInfo
from psbridge.com.executor import ComExecutor
from psbridge.com.fake import FakeBackend, FakeFault
from psbridge.config import BridgeConfig
from psbridge.core.ops import CloneResult, ModelFingerprint, SafeOps, hash_file
from psbridge.errors import (
    BackendError,
    ModelMutationDenied,
    ObjectExists,
    ObjectNotFound,
    PathNotAllowed,
    TypeNotAllowed,
    ValidationError,
    VerificationFailed,
)

FRAME = ".Models.Model"


@pytest.fixture()
def backend() -> FakeBackend:
    fake = FakeBackend()
    fake.connect()
    fake.new_model()
    return fake


@pytest.fixture()
def ops(backend: FakeBackend) -> SafeOps:
    return SafeOps(backend)


def _saved_model(tmp_path: Path, backend: FakeBackend) -> Path:
    target = tmp_path / "model.spp"
    backend.save_model(str(target))
    return target


# ---------------------------------------------------------------------------
# connection and listing
# ---------------------------------------------------------------------------
def test_listing_connects_first_and_never_returns_a_silent_empty_list() -> None:
    fake = FakeBackend()
    ops = SafeOps(fake)
    assert fake.is_connected() is False
    with pytest.raises(ObjectNotFound):
        ops.list_objects()
    assert fake.is_connected() is True
    assert fake.connect_count == 1

    ops.new_model()
    assert ops.list_objects() == ()
    with pytest.raises(ObjectNotFound):
        ops.list_objects(allow_empty=False)
    with pytest.raises(ObjectNotFound):
        ops.list_objects(".Models.Other")


def test_ensure_connected_is_idempotent(ops: SafeOps, backend: FakeBackend) -> None:
    ops.ensure_connected()
    ops.ensure_connected()
    assert backend.connect_count == 1


def test_object_info_reports_unknown_objects(ops: SafeOps) -> None:
    with pytest.raises(ObjectNotFound):
        ops.object_info(FRAME + ".Nope")


# ---------------------------------------------------------------------------
# create / delete
# ---------------------------------------------------------------------------
def test_create_device_creates_without_overwriting(ops: SafeOps, backend: FakeBackend) -> None:
    info = ops.create_device("Station", "S1", x=1.0, y=2.0)
    assert info.path == FRAME + ".S1"
    assert info.class_name == ".MaterialFlow.SingleProc"
    ops.set_property("S1", "procTime", 2.5)
    with pytest.raises(ObjectExists):
        ops.create_device("Buffer", "S1")
    assert backend.get_value(FRAME + ".S1", "procTime") == 2.5
    assert backend.delete_count == 0


def test_create_device_validates_class_and_coordinates(ops: SafeOps) -> None:
    with pytest.raises(TypeNotAllowed):
        ops.create_device("Evil", "S1")
    with pytest.raises(TypeNotAllowed):
        ops.create_device("Method", "M1")  # auxiliary classes are not devices
    with pytest.raises(ValidationError):
        ops.create_device("Station", "bad name")
    with pytest.raises(ValidationError):
        ops.create_device("Station", "S1", x=float("inf"))


def test_delete_device_requires_explicit_confirmation(ops: SafeOps, backend: FakeBackend) -> None:
    ops.create_device("Station", "S1")
    with pytest.raises(ValidationError):
        ops.delete_device("S1")
    with pytest.raises(ValidationError):
        ops.delete_device("S1", confirm="yes")  # type: ignore[arg-type]
    assert backend.object_exists(FRAME + ".S1") is True
    ops.delete_device("S1", confirm=True)
    assert backend.object_exists(FRAME + ".S1") is False
    with pytest.raises(ObjectNotFound):
        ops.delete_device("S1", confirm=True)


def test_connect_objects_checks_both_endpoints(ops: SafeOps) -> None:
    ops.create_device("Station", "S1")
    ops.create_device("Buffer", "B1")
    with pytest.raises(ObjectNotFound):
        ops.connect_objects("S1", "B2")
    ops.connect_objects("S1", "B1")
    info = ops.object_info(FRAME + ".S1")
    assert info.successors == (FRAME + ".B1",)


# ---------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------
def test_property_round_trip_and_applicability(ops: SafeOps) -> None:
    ops.create_device("Station", "S1")
    assert ops.get_property("S1", "procTime") == 0.0
    assert ops.set_property("S1", "procTime", 2) == 2.0
    assert ops.get_property("S1", "procTime") == 2.0
    with pytest.raises(TypeNotAllowed):
        ops.get_property("S1", "Capacity")
    with pytest.raises(TypeNotAllowed):
        ops.set_property("S1", "Capacity", 1)
    with pytest.raises(ValidationError):
        ops.set_property("S1", "procTime", -1.0)


def test_property_access_requires_a_reported_class(
    ops: SafeOps, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops.create_device("Station", "S1")
    monkeypatch.setattr(
        backend,
        "get_object_info",
        lambda path: ObjectInfo(path=path, name="S1", class_name=""),
    )
    with pytest.raises(BackendError):
        ops.get_property("S1", "procTime")
    with pytest.raises(BackendError):
        ops.set_property("S1", "procTime", 1.0)


def test_set_property_surfaces_a_silent_backend_failure(ops: SafeOps, backend: FakeBackend) -> None:
    ops.create_device("Station", "S1")
    backend.inject_fault(FakeFault("set_value", target="procTime", silent=True))
    with pytest.raises(VerificationFailed):
        ops.set_property("S1", "procTime", 9.0)
    assert backend.get_value(FRAME + ".S1", "procTime") == 0.0


# ---------------------------------------------------------------------------
# original-model protection
# ---------------------------------------------------------------------------
def test_load_model_tracks_a_fingerprint(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    ops.create_device("Station", "S1")
    target = _saved_model(tmp_path, backend)
    fingerprint = ops.load_model(str(target))
    assert isinstance(fingerprint, ModelFingerprint)
    assert fingerprint.exists is True
    assert fingerprint.sha256 == hash_file(target)
    assert ops.original_fingerprint == fingerprint
    assert ops.assert_original_untouched() == fingerprint


def test_assert_original_untouched_detects_a_modified_file(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(VerificationFailed):
        ops.assert_original_untouched()
    assert ops.verify_model_hash() is False
    target.unlink()
    with pytest.raises(VerificationFailed):
        ops.assert_original_untouched()


def test_mutations_of_the_tracked_original_are_denied(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    ops.create_device("Station", "S1")
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    with pytest.raises(ModelMutationDenied):
        ops.create_device("Station", "S2")
    with pytest.raises(ModelMutationDenied):
        ops.set_property("S1", "procTime", 5.0)
    with pytest.raises(ModelMutationDenied):
        ops.delete_device("S1", confirm=True)
    with pytest.raises(ModelMutationDenied):
        ops.create_device("Buffer", "S1", overwrite=True)


def test_protection_can_be_disabled_explicitly(tmp_path: Path, backend: FakeBackend) -> None:
    ops = SafeOps(backend, protect_original=False)
    ops.create_device("Station", "S1")
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    ops.create_device("Station", "S2")
    assert backend.object_exists(FRAME + ".S2") is True


# ---------------------------------------------------------------------------
# cloning for experiments
# ---------------------------------------------------------------------------
def test_clone_model_for_experiment_proves_the_original_is_untouched(
    tmp_path: Path, backend: FakeBackend, ops: SafeOps
) -> None:
    ops.create_device("Station", "S1")
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    before = ops.snapshot_model_hash()

    result = ops.clone_model_for_experiment(tmp_path / "work", tag="run1")
    assert isinstance(result, CloneResult)
    assert result.original_unchanged is True
    assert result.original_before.sha256 == result.original_after.sha256 == before.sha256
    assert result.clone.exists is True
    assert result.clone.sha256 == before.sha256
    assert result.identical is True
    assert backend.model_path == result.clone.path
    assert ops.clone_result is result
    assert ops.assert_original_untouched().sha256 == before.sha256

    # The clone is now the active model, so mutations are allowed there.
    ops.create_device("Station", "S2")
    assert backend.object_exists(FRAME + ".S2") is True
    assert ops.assert_original_untouched().sha256 == before.sha256


def test_clone_requires_a_tracked_original(tmp_path: Path, ops: SafeOps) -> None:
    with pytest.raises(BackendError):
        ops.clone_model_for_experiment(tmp_path / "work")


def test_clone_rejects_a_tag_that_is_not_an_identifier(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    with pytest.raises(ValidationError):
        ops.clone_model_for_experiment(tmp_path / "work", tag="bad tag")


def test_clone_result_serialises_to_plain_json(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    payload = ops.clone_model_for_experiment(tmp_path / "work").to_dict()
    assert payload["original_unchanged"] is True
    assert set(payload) == {"original_before", "original_after", "clone", "identical", "original_unchanged"}
    assert json.loads(json.dumps(payload))["clone"]["exists"] is True


# ---------------------------------------------------------------------------
# saving, hashing, config
# ---------------------------------------------------------------------------
def test_save_model_returns_a_fingerprint(tmp_path: Path, ops: SafeOps) -> None:
    ops.new_model()
    with pytest.raises(BackendError):
        ops.save_model()  # no path anywhere yet
    fingerprint = ops.save_model(str(tmp_path / "model.spp"))
    assert fingerprint.exists is True
    assert fingerprint.size == Path(fingerprint.path).stat().st_size


def test_snapshot_model_hash_reports_missing_files(tmp_path: Path, ops: SafeOps) -> None:
    fingerprint = ops.snapshot_model_hash(str(tmp_path / "missing.spp"))
    assert fingerprint.exists is False
    assert fingerprint.sha256 is None
    assert fingerprint.size is None
    with pytest.raises(ValidationError):
        ops.snapshot_model_hash()


def test_hash_file_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    target.write_bytes(b"abc" * 1000)
    assert hash_file(target) == hashlib.sha256(target.read_bytes()).hexdigest()


def test_model_root_is_enforced(tmp_path: Path, backend: FakeBackend) -> None:
    ops = SafeOps(backend, BridgeConfig(model_root=tmp_path))
    outside = tmp_path.parent / "outside.spp"
    with pytest.raises(PathNotAllowed):
        ops.load_model(str(outside))
    with pytest.raises(ValidationError):
        ops.load_model("")


def test_disconnect_never_saves_by_default(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    saves_before = backend.save_count
    ops.disconnect()
    assert backend.save_count == saves_before
    assert backend.is_connected() is False


def test_disconnect_can_save_when_explicitly_configured(tmp_path: Path, backend: FakeBackend) -> None:
    ops = SafeOps(backend, BridgeConfig(auto_save_on_disconnect=True))
    ops.create_device("Station", "S1")
    _saved_model(tmp_path, backend)
    saves_before = backend.save_count
    ops.disconnect()
    assert backend.save_count == saves_before + 1


def test_new_model_clears_tracking(tmp_path: Path, backend: FakeBackend, ops: SafeOps) -> None:
    target = _saved_model(tmp_path, backend)
    ops.load_model(str(target))
    ops.new_model()
    assert ops.original_fingerprint is None
    assert ops.clone_result is None
    with pytest.raises(BackendError):
        ops.clone_model_for_experiment(tmp_path / "work")


# ---------------------------------------------------------------------------
# executor integration
# ---------------------------------------------------------------------------
def test_ops_runs_every_call_through_the_executor(backend: FakeBackend) -> None:
    executor = ComExecutor(default_timeout=10.0)
    try:
        ops = SafeOps(backend, BridgeConfig(), executor=executor)
        ops.create_device("Station", "S1")
        stats = executor.stats()
        assert stats.calls >= 3  # exists + create + exists
        assert stats.worker_thread_id is not None
        assert stats.worker_thread_id != threading.get_ident()
        assert stats.current_operation is None
        assert stats.operation_depth == 0
    finally:
        executor.close()


def test_ops_with_executor_still_reports_domain_errors(backend: FakeBackend) -> None:
    executor = ComExecutor(default_timeout=10.0)
    try:
        ops = SafeOps(backend, BridgeConfig(), executor=executor)
        ops.create_device("Station", "S1")
        with pytest.raises(ObjectExists):
            ops.create_device("Station", "S1")
        with pytest.raises(ObjectNotFound):
            ops.delete_device("S9", confirm=True)
    finally:
        executor.close()


def test_ops_rejects_a_missing_backend() -> None:
    with pytest.raises(ValidationError):
        SafeOps(None)  # type: ignore[arg-type]


def test_start_and_run_reject_non_positive_end_time_and_negative_stats(
    tmp_path: Path, backend: FakeBackend, ops: SafeOps
) -> None:
    for bad_end in (0.0, -5.0):
        with pytest.raises(ValidationError):
            ops.start_simulation(end_time=bad_end)
        with pytest.raises(ValidationError):
            ops.run_simulation(end_time=bad_end)
    with pytest.raises(ValidationError):
        ops.start_simulation(statistics_start=-1.0)


def test_clone_rejects_same_path_with_different_relative_form(tmp_path: Path, backend: FakeBackend) -> None:
    target = _saved_model(tmp_path, backend)
    alias = str(tmp_path / "." / target.name)
    with pytest.raises(ValidationError):
        backend.clone_model(alias)
