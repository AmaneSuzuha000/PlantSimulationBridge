"""Tests for psbridge.experiment.instance (strict I/O, validation, variants)."""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.instance import (
    MIN_DURATION,
    instance_from_json,
    load_instance,
    sample_variant,
    save_instance,
    summarize_instance,
    validate_engine_compatibility,
    validate_instance,
)
from psbridge.schema import Instance, Job, Machine, Operation


def tiny_instance() -> Instance:
    return Instance(
        instance_id="tiny_01",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                job_id="J1",
                release_time=0.0,
                due_date=6.0,
                operations=(
                    Operation("J1_O1", "J1", 0, ("M1",), 3.0),
                    Operation("J1_O2", "J1", 1, ("M2",), 2.0),
                ),
            ),
            Job(
                job_id="J2",
                release_time=1.0,
                due_date=6.0,
                weight=2.0,
                operations=(
                    Operation("J2_O1", "J2", 0, ("M1",), 2.0),
                    Operation("J2_O2", "J2", 1, ("M2",), 4.0),
                ),
            ),
        ),
        meta={"source": "unit-test", "nested": {"a": [1, 2]}},
    )


def test_save_load_roundtrip(tmp_path: Path) -> None:
    instance = tiny_instance()
    path = tmp_path / "nested" / "instance.json"
    written = save_instance(instance, path)
    assert written == path
    assert path.exists()
    restored = load_instance(path)
    assert restored == instance
    assert restored.content_hash() == instance.content_hash()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1"
    assert payload["instance_id"] == "tiny_01"


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "instance.json"
    save_instance(tiny_instance(), path)
    save_instance(tiny_instance(), path)
    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name != "instance.json"]
    assert leftovers == []
    assert json.loads(path.read_text(encoding="utf-8"))["instance_id"] == "tiny_01"


def test_save_overwrites_previous_content(tmp_path: Path) -> None:
    path = tmp_path / "instance.json"
    save_instance(tiny_instance(), path)
    other = Instance(
        instance_id="other",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 1.0),)),),
    )
    save_instance(other, path)
    assert load_instance(path).instance_id == "other"


def test_load_rejects_unknown_fields(tmp_path: Path) -> None:
    data = tiny_instance().to_dict()
    data["surprise"] = 1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        load_instance(path)
    assert "unknown" in excinfo.value.message.lower()


def test_load_rejects_non_finite_constants(tmp_path: Path) -> None:
    text = json.dumps(tiny_instance().to_dict()).replace('"duration": 3.0', '"duration": NaN')
    path = tmp_path / "nan.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_instance(path)
    with pytest.raises(ValidationError):
        instance_from_json('{"instance_id": "x", "machines": [], "jobs": [], "meta": {"v": Infinity}}')


def test_load_rejects_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_instance(path)
    with pytest.raises(ValidationError):
        instance_from_json("[]")


def test_load_rejects_missing_required_keys() -> None:
    with pytest.raises(ValidationError):
        instance_from_json('{"instance_id": "x", "machines": [], "jobs": []}')


def test_save_rejects_non_instance(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        save_instance({"instance_id": "x"}, tmp_path / "x.json")  # type: ignore[arg-type]


def test_validate_instance_reports_every_gate() -> None:
    report = validate_instance(tiny_instance())
    assert report.valid
    names = [check.name for check in report.checks]
    assert names[:4] == ["instance_type", "instance_id", "machines_present", "jobs_present"]
    for expected in (
        "unique_machine_ids",
        "unique_job_ids",
        "unique_operation_ids",
        "duration_positive_finite",
        "operation_candidates",
        "release_times_valid",
        "due_dates_valid",
        "weights_positive",
        "meta_json_serialisable",
    ):
        assert expected in names
    assert all(check.passed for check in report.checks)


def test_validate_instance_flags_a_broken_object() -> None:
    # Build a valid instance, then violate an invariant behind the dataclass
    # validation (object.__setattr__ on a frozen instance) to prove the gate
    # actually inspects the object instead of trusting construction.
    instance = tiny_instance()
    bad_job = instance.jobs[0]
    object.__setattr__(bad_job.operations[0], "duration", -1.0)
    report = validate_instance(instance)
    assert not report.valid
    failed = {check.name for check in report.failures()}
    assert "duration_positive_finite" in failed


def test_validate_instance_flags_candidate_reference_to_unknown_machine() -> None:
    instance = tiny_instance()
    object.__setattr__(instance.jobs[0].operations[0], "candidates", ("M9",))
    report = validate_instance(instance)
    assert not report.valid
    failed = {check.name for check in report.failures()}
    assert "operation_candidates" in failed


def test_validate_instance_rejects_non_instance() -> None:
    report = validate_instance("not an instance")  # type: ignore[arg-type]
    assert not report.valid
    assert report.checks[0].name == "instance_type"


def test_engine_compatibility_gates() -> None:
    assert validate_engine_compatibility(tiny_instance()).valid
    flexible = Instance(
        instance_id="flex",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1", "M2"), 1.0),)),),
    )
    report = validate_engine_compatibility(flexible)
    assert not report.valid
    failed = {check.name for check in report.failures()}
    assert failed == {"single_candidate_routing"}


def test_sample_variant_zero_jitter_is_identity() -> None:
    instance = tiny_instance()
    assert sample_variant(instance, 0, 0.0) is instance
    assert sample_variant(instance, 99, 0.0) == instance


def test_sample_variant_is_deterministic() -> None:
    instance = tiny_instance()
    first = sample_variant(instance, 7, 0.2)
    second = sample_variant(instance, 7, 0.2)
    assert first.content_hash() == second.content_hash()
    assert [op.duration for op in first.operations()] == [op.duration for op in second.operations()]


def test_sample_variant_is_seed_sensitive() -> None:
    instance = tiny_instance()
    assert sample_variant(instance, 1, 0.2).content_hash() != sample_variant(instance, 2, 0.2).content_hash()


def test_sample_variant_respects_jitter_bounds_and_stays_positive() -> None:
    instance = tiny_instance()
    jitter = 0.5
    for seed in range(20):
        variant = sample_variant(instance, seed, jitter)
        for base, drawn in zip(instance.operations(), variant.operations()):
            assert base.op_id == drawn.op_id
            assert drawn.duration > 0.0
            assert base.duration * (1.0 - jitter) - 1e-12 <= drawn.duration <= base.duration * (1.0 + jitter) + 1e-12
        assert validate_instance(variant).valid


def test_sample_variant_does_not_touch_global_random_state() -> None:
    instance = tiny_instance()
    random.seed(12345)
    expected = random.random()
    random.seed(12345)
    sample_variant(instance, 3, 0.3)
    assert random.random() == expected


def test_sample_variant_matches_a_private_rng_reimplementation() -> None:
    instance = tiny_instance()
    seed, jitter = 11, 0.1
    rng = random.Random(seed)
    expected = {
        operation.op_id: operation.duration * rng.uniform(1.0 - jitter, 1.0 + jitter)
        for operation in instance.operations()
    }
    variant = sample_variant(instance, seed, jitter)
    for op_id, value in expected.items():
        assert math.isclose(variant.operation(op_id).duration, value, rel_tol=1e-12, abs_tol=1e-12)


def test_sample_variant_rejects_bad_arguments() -> None:
    instance = tiny_instance()
    with pytest.raises(ValidationError):
        sample_variant(instance, -1, 0.1)
    with pytest.raises(ValidationError):
        sample_variant(instance, 0, 1.0)
    with pytest.raises(ValidationError):
        sample_variant(instance, 0, -0.1)
    with pytest.raises(ValidationError):
        sample_variant(instance, True, 0.1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        sample_variant("nope", 0, 0.1)  # type: ignore[arg-type]


def test_sample_variant_never_underflows_to_zero() -> None:
    # A subnormal base duration stays positive for every seed/jitter pair: the
    # clamp only exists for a possible underflow to 0.0.
    assert MIN_DURATION > 0.0
    instance = Instance(
        instance_id="subnormal",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 5e-324),)),),
    )
    for seed in range(25):
        duration = sample_variant(instance, seed, 0.9).operation("J1_O1").duration
        assert duration > 0.0
        assert math.isfinite(duration)


def test_summarize_instance() -> None:
    summary = summarize_instance(tiny_instance())
    assert summary["instance_id"] == "tiny_01"
    assert summary["job_count"] == 2
    assert summary["machine_count"] == 2
    assert summary["operation_count"] == 4
    assert summary["flexible_operation_count"] == 0
    assert summary["total_processing_time"] == 11.0
    assert summary["min_duration"] == 2.0
    assert summary["max_duration"] == 4.0
    assert summary["mean_duration"] == pytest.approx(2.75)
    assert summary["min_release_time"] == 0.0
    assert summary["max_release_time"] == 1.0
    assert summary["jobs_with_due_date"] == 2
    assert summary["min_due_date"] == 6.0
    assert summary["total_weight"] == 3.0
    assert summary["meta_keys"] == ["nested", "source"]
    assert summary["content_hash"] == tiny_instance().content_hash()
    json.dumps(summary)


def test_summary_reports_due_date_absence() -> None:
    instance = Instance(
        instance_id="no_due",
        machines=(Machine("M1"),),
        jobs=(Job("J1", (Operation("J1_O1", "J1", 0, ("M1",), 1.0),)),),
    )
    summary = summarize_instance(instance)
    assert summary["jobs_with_due_date"] == 0
    assert summary["min_due_date"] is None
    assert summary["max_due_date"] is None
