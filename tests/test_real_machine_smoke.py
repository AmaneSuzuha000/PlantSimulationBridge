"""Tests for the real-machine acceptance harness (dry-run and CLI contract)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "real_machine_smoke.py"


def load_harness():
    spec = importlib.util.spec_from_file_location("real_machine_smoke", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return load_harness()


def test_harness_exists(harness) -> None:
    assert SCRIPT_PATH.exists()
    assert callable(harness.run_execute)
    assert callable(harness.run_dry)


def test_dry_run_generates_sources_and_reference(harness, tmp_path: Path) -> None:
    from psbridge.demo import build_demo_instance

    instance = build_demo_instance()
    payload = harness.run_dry(instance, "fifo", tmp_path / "dry")
    assert payload["mode"] == "dry-run"
    assert payload["status"] == "DRY_RUN"
    assert payload["reference_valid"] is True
    assert payload["reference_makespan"] > 0
    assert set(payload["simtalk_files"]) == {
        "build_model.simtalk",
        "initialize_tables.simtalk",
        "dispatch_method.simtalk",
        "on_source_exit.simtalk",
        "on_station_enter.simtalk",
        "on_station_exit.simtalk",
        "read_results.simtalk",
    }
    for path in payload["simtalk_files"].values():
        assert Path(path).exists()
        assert Path(path).read_text(encoding="utf-8").strip()
    assert not (tmp_path / "dry" / "NOT_VERIFIED.txt").exists()
    assert payload["reference_schedule"]
    json.dumps(payload)


def test_dry_run_reference_matches_engine(harness, tmp_path: Path) -> None:
    from psbridge.demo import build_demo_instance
    from psbridge.experiment.validity import validate_schedule
    from psbridge.schema import ScheduledOperation

    instance = build_demo_instance()
    payload = harness.run_dry(instance, "spt", tmp_path / "dry2")
    engine_schedule = harness.python_schedule(instance, "spt")

    rebuilt = tuple(
        ScheduledOperation(
            op_id=row["op_id"],
            job_id=row["job_id"],
            machine_id=row["machine_id"],
            start=row["start"],
            end=row["end"],
        )
        for row in payload["reference_schedule"]
    )
    assert [item.op_id for item in rebuilt] == [item.op_id for item in engine_schedule]
    assert validate_schedule(instance, rebuilt).valid is True


def test_execute_requires_confirmation(harness, tmp_path: Path, capsys) -> None:
    from psbridge.demo import build_demo_instance, write_instance

    instance_path = write_instance(build_demo_instance(), tmp_path / "inst.json")
    with pytest.raises(SystemExit) as excinfo:
        harness.main(["--instance", str(instance_path), "--execute"])
    assert excinfo.value.code == 2
    assert "confirm-real-machine" in capsys.readouterr().err


def test_execute_requires_model_root(harness, tmp_path: Path, capsys) -> None:
    from psbridge.demo import build_demo_instance, write_instance

    instance_path = write_instance(build_demo_instance(), tmp_path / "inst2.json")
    with pytest.raises(SystemExit) as excinfo:
        harness.main(["--instance", str(instance_path), "--execute", "--confirm-real-machine"])
    assert excinfo.value.code == 2
    assert "model-root" in capsys.readouterr().err


def test_execute_reports_missing_model(harness, tmp_path: Path) -> None:
    from psbridge.demo import build_demo_instance, write_instance

    instance_path = write_instance(build_demo_instance(), tmp_path / "inst3.json")
    exit_code = harness.main(
        [
            "--instance",
            str(instance_path),
            "--execute",
            "--confirm-real-machine",
            "--model-root",
            str(tmp_path / "no-model-here"),
            "--work-dir",
            str(tmp_path / "work"),
        ]
    )
    assert exit_code == 2


def test_dry_run_cli_exit_code(harness, tmp_path: Path) -> None:
    from psbridge.demo import build_demo_instance, write_instance

    instance_path = write_instance(build_demo_instance(), tmp_path / "inst4.json")
    assert harness.main(["--instance", str(instance_path), "--policy", "edd", "--work-dir", str(tmp_path / "w")]) == 0
