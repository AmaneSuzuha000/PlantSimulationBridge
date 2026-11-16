"""Tests for the verified SimTalk source and control method generators.

Offline tests verify structure, determinism, SimTalk 1.0 convention
compliance, and strict identifier/path validation. Optional live tests
(enabled via ``PSBRIDGE_REAL_COM=1``) verify full execution parity against
Siemens Plant Simulation 13.
"""

from __future__ import annotations

import contextlib
import gc
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from psbridge import errors
from psbridge.errors import ValidationError
from psbridge.schema import Instance, Job, Machine, Operation
from psbridge.simtalk import templates as T
from psbridge.simtalk.model_spec import (
    JOB_TABLE,
    PARALLEL_STATION_CLASS,
    READY_TABLE,
    RESULT_TABLE,
    ROUTE_TABLE,
    build_model_spec,
)


def op(op_id: str, job_id: str, index: int, machine: str, duration: float) -> Operation:
    return Operation(op_id=op_id, job_id=job_id, index=index, candidates=(machine,), duration=duration)


def make_instance(
    instance_id: str = "tpl_demo",
    *,
    job_id: str = "J1",
    machine_id: str = "M1",
    due=None,
    capacity: int = 1,
) -> Instance:
    other_machine = "M2"
    return Instance(
        instance_id=instance_id,
        jobs=(
            Job(
                job_id,
                (op(job_id + "_O1", job_id, 0, machine_id, 3.0), op(job_id + "_O2", job_id, 1, other_machine, 2.0)),
                release_time=0.0,
                due_date=due,
            ),
            Job(
                "J2",
                (op("J2_O1", "J2", 0, machine_id, 2.0), op("J2_O2", "J2", 1, other_machine, 4.0)),
                release_time=1.0,
                due_date=None,
            ),
        ),
        machines=(Machine(machine_id, capacity=capacity), Machine(other_machine)),
    )


def all_artifacts(instance: Instance | None = None, policy: str = "fifo") -> dict[str, str]:
    instance = instance or make_instance()
    artifacts = {
        "build_model_source": T.build_model_source(build_model_spec(instance)),
        "initialize_tables_source": T.initialize_tables_source(instance),
        "dispatch_method_source": T.dispatch_method_source(policy),
        "read_results_source": T.read_results_source(),
    }
    artifacts.update(T.control_method_sources())
    return artifacts


# -- conventions & hygiene ---------------------------------------------------
def test_conventions_are_documented_and_numbered() -> None:
    ids = [item.split(":")[0] for item in T.SIMTALK_CONVENTIONS]
    assert ids == ["C" + str(index) for index in range(1, len(ids) + 1)]
    assert len(set(ids)) == len(ids)
    assert T.METHOD_NAMES == ("Dispatch", "OnSourceExit", "OnStationEnter", "OnStationExit")


def test_no_not_verified_markers_remain() -> None:
    for name, text in all_artifacts().items():
        assert "NOT_VERIFIED" not in text, name
        assert text.endswith("\n"), name
        assert "\r" not in text, name


# -- determinism -------------------------------------------------------------
def test_generation_is_deterministic() -> None:
    assert all_artifacts() == all_artifacts()


def test_generation_does_not_depend_on_instance_identity_alone() -> None:
    base = T.initialize_tables_source(make_instance("one"))
    other = T.initialize_tables_source(make_instance("two"))
    assert base != other
    assert "instance_id: one" in base
    assert "instance_id: two" in other


def test_policy_choice_changes_only_the_dispatch_method() -> None:
    fifo = all_artifacts(policy="fifo")
    spt = all_artifacts(policy="spt")
    assert fifo["build_model_source"] == spt["build_model_source"]
    assert fifo["initialize_tables_source"] == spt["initialize_tables_source"]
    assert fifo["read_results_source"] == spt["read_results_source"]
    assert fifo["OnSourceExit"] == spt["OnSourceExit"]
    assert fifo["OnStationEnter"] == spt["OnStationEnter"]
    assert fifo["OnStationExit"] == spt["OnStationExit"]
    assert fifo["dispatch_method_source"] != spt["dispatch_method_source"]


# -- path validation ---------------------------------------------------------
@pytest.mark.parametrize("path", [".Models.Model", "Model", ".Models.Model.Sub", "a.b.c_d"])
def test_valid_frame_paths_accepted(path: str) -> None:
    assert T.validate_simtalk_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        " ",
        "Models..Model",
        ".Models.Model.",
        "1Models",
        "Models.Model ",
        "Models;DROP",
        'Models."Model"',
        "Models.Model[1]",
        "Models\n.Model",
        'Models.Model\nprint "x"',
        "." + ".".join(["Segment"] * 40),
    ],
)
def test_invalid_frame_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        T.validate_simtalk_path(path)


def test_non_string_path_rejected() -> None:
    with pytest.raises(ValidationError):
        T.validate_simtalk_path(None)  # type: ignore[arg-type]


def test_hostile_frame_path_cannot_inject_simtalk() -> None:
    with pytest.raises(ValidationError):
        T.build_model_source(build_model_spec(make_instance()), frame_path='.Models.Model\nprint "pwned"')
    with pytest.raises(ValidationError):
        T.dispatch_method_source("fifo", frame_path='.Models"; deleteModel')


# -- policy validation -------------------------------------------------------
@pytest.mark.parametrize("name", ["fifo", "spt", "edd"])
def test_every_registered_policy_has_a_method(name: str) -> None:
    text = T.dispatch_method_source(name)
    assert "policy '" + name + "'" in text


@pytest.mark.parametrize("name", ["FIFO", "", "random", "fifo ", "fifo\n", "fcfs"])
def test_unknown_policy_rejected(name: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        T.dispatch_method_source(name)
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_non_string_policy_rejected() -> None:
    with pytest.raises(ValidationError):
        T.dispatch_method_source(None)  # type: ignore[arg-type]


def test_validate_policy_name_matches_registry() -> None:
    from psbridge.scheduling.policies import available_policies

    for name in available_policies():
        assert T.validate_policy_name(name) == name


# -- hostile identifiers -----------------------------------------------------
@pytest.mark.parametrize(
    "job_id",
    [
        'J1"; delete this; --',
        "J 1",
        "J-1",
        "1J",
        "",
        "J1\n",
        "J" * 200,
    ],
)
def test_hostile_job_ids_are_rejected_before_generation(job_id: str) -> None:
    with pytest.raises(errors.BridgeError):
        make_instance(job_id=job_id)


def test_generated_source_contains_no_raw_quotes_from_data() -> None:
    text = T.initialize_tables_source(make_instance())
    for line in text.splitlines():
        if ":=" not in line or line.lstrip().startswith("--"):
            continue
        value = line.split(":=", 1)[1].strip()
        if value.startswith('"'):
            assert value.count('"') == 2, line
            assert value.endswith(('";', '"')), line


# -- model source ------------------------------------------------------------
def test_model_source_creates_every_object_once() -> None:
    instance = make_instance()
    spec = build_model_spec(instance)
    text = T.build_model_source(spec)
    assert ".MUs.Entity.derive();" in text
    for src in spec.sources:
        assert ".MaterialFlow.Source.createObject(" in text
        assert 'obj.Name := "' + src.name + '";' in text
        assert 'obj.Name := "DT_' + src.job_id + '";' in text
    for buf in spec.buffers:
        assert 'obj.Name := "' + buf.name + '";' in text
    assert ".MaterialFlow.Drain.createObject(" in text
    assert 'obj.Name := "Drain_Output";' in text
    assert ".MaterialFlow.SingleProc.createObject(" in text
    assert 'obj.Name := "Stn_M1";' in text
    assert 'obj.Name := "Stn_M2";' in text
    for method_name in T.METHOD_NAMES:
        assert 'obj.Name := "' + method_name + '";' in text


def test_model_source_supports_parallel_stations() -> None:
    instance = make_instance(capacity=3)
    text = T.build_model_source(build_model_spec(instance))
    assert PARALLEL_STATION_CLASS + ".createObject(" in text
    assert "obj.XDim := 3;" in text
    assert "obj.YDim := 1;" in text


def test_model_source_never_deletes_objects() -> None:
    text = T.build_model_source(build_model_spec(make_instance()))
    statements = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    code = "\n".join(statements).lower()
    for forbidden in (".delete", "removeobject", ".destroy", "reset"):
        assert forbidden not in code


def test_model_source_sets_unbounded_buffer_capacity() -> None:
    text = T.build_model_source(build_model_spec(make_instance()))
    assert "obj.Capacity := -1;" in text


def test_model_source_rejects_wrong_spec_type() -> None:
    with pytest.raises(ValidationError):
        T.build_model_source(make_instance())  # type: ignore[arg-type]


# -- table initialisation ----------------------------------------------------
def test_tables_source_formats_tables_and_delivery_tables() -> None:
    instance = make_instance()
    text = T.initialize_tables_source(instance)
    for tbl_name in (JOB_TABLE, ROUTE_TABLE, READY_TABLE, RESULT_TABLE):
        assert 'str_to_obj(".Models.Model.' + tbl_name + '")' in text
    assert "tbl.ColumnIndex := true;" in text
    assert "tbl.setDataType(" in text
    assert "tbl.cutRow(tbl.yDim);" in text
    assert "setDimensions" not in text
    assert 'str_to_obj(".Models.Model.DT_J1")' in text
    assert 'src.TimeOfGeneration := "Delivery Table";' in text
    assert "src.Path := tbl;" in text


def test_tables_source_writes_every_column_header() -> None:
    text = T.initialize_tables_source(make_instance())
    for column in ("OpID", "JobID", "Step", "MachineID", "Duration", "ReadySince", "JobRelease", "DueDate", "HasDueDate"):
        assert ':= "' + column + '"' in text


def test_missing_due_date_uses_flag_and_sentinel() -> None:
    text = T.initialize_tables_source(make_instance(due=None))
    assert "false" in text
    literal = T._num(T.NO_DUE_SENTINEL)
    assert literal in text
    assert "." in literal
    assert "e+" not in text.lower()


def test_due_date_round_trips() -> None:
    text = T.initialize_tables_source(make_instance(due=12.5))
    assert "12.5" in text


def test_numeric_formatting_handles_extreme_ranges_and_tiny_positives() -> None:
    assert T._num(0.0) == "0"
    assert T._num(-0.0) == "0"
    assert T._num(2_000_000_000.0) == "2000000000.0"
    assert T._num(1.0e12) == "1000000000000.0"
    assert T._num(1.0e16) == "1e+16"
    assert T._num(1.0e-12) == "1e-12"
    assert T._num(1.0e-12) != "0"


def test_tables_source_rejects_wrong_type() -> None:
    with pytest.raises(ValidationError):
        T.initialize_tables_source(build_model_spec(make_instance()))  # type: ignore[arg-type]


# -- dispatch & control methods ----------------------------------------------
@pytest.mark.parametrize(
    ("policy", "primary"),
    [("fifo", "ReadySince"), ("spt", "Duration"), ("edd", "HasDueDate")],
)
def test_dispatch_primary_key_matches_policy(policy: str, primary: str) -> None:
    text = T.dispatch_method_source(policy)
    assert "primary key: " + primary in text


def test_dispatch_uses_simtalk1_controls_and_cuts_row() -> None:
    text = T.dispatch_method_source("fifo")
    assert text.lstrip().startswith("--")
    assert "\nis\n" in text
    assert "\ndo\n" in text
    assert "root.EventController.SimTime" in text
    assert "tblReady.cutRow(bestRow);" in text
    assert "stn.ProcTime := bestDuration;" in text
    assert "chosenMu.move(stn);" in text
    assert "tblRes[" in text
    assert "nowTime + bestDuration;" in text
    assert "setDimensions" not in text


def test_dispatch_methods_differ_between_policies() -> None:
    texts = {name: T.dispatch_method_source(name) for name in ("fifo", "spt", "edd")}
    assert len(set(texts.values())) == 3


def test_control_methods_are_simtalk1_and_avoid_reserved_weight() -> None:
    methods = T.method_sources("fifo")
    assert set(methods) == set(T.METHOD_NAMES)
    for name, src in methods.items():
        assert "\nis\n" in src, name
        assert "\nend;\n" in src, name
        assert "var " not in src, name
    assert "jobWeight : real;" in methods["OnSourceExit"]
    assert "wait 0;" in methods["OnSourceExit"]
    assert "executeNewCallChain()" in methods["OnSourceExit"]
    assert "wait 0;" in methods["OnStationExit"]
    assert "executeNewCallChain()" in methods["OnStationExit"]


# -- results reader ----------------------------------------------------------
def test_results_reader_prints_a_fixed_format() -> None:
    text = T.read_results_source()
    assert "RESULT " in text
    assert "SUMMARY " in text
    assert "Table_Results" in text


def test_results_reader_handles_the_empty_table() -> None:
    text = T.read_results_source()
    assert 'print "SUMMARY 0 0"' in text


def test_results_reader_rejects_bad_frame_path() -> None:
    with pytest.raises(ValidationError):
        T.read_results_source(frame_path="bad path")


# -- cross-artifact consistency ---------------------------------------------
def test_all_artifacts_agree_on_the_frame_path() -> None:
    instance = make_instance()
    frame = ".Models.Custom.Frame"
    artifacts = [
        T.build_model_source(build_model_spec(instance), frame_path=frame),
        T.initialize_tables_source(instance, frame_path=frame),
        T.read_results_source(frame_path=frame),
        *T.method_sources("fifo", frame_path=frame).values(),
    ]
    for text in artifacts:
        assert frame in text
        for other in (".Models.Plant", ".Models.Other"):
            assert other not in text


# -- optional live Plant Simulation 13 parity tests --------------------------
def _find_template() -> Path | None:
    candidates: list[Path] = []
    env_val = os.environ.get("PSBRIDGE_TEMPLATE")
    if env_val:
        candidates.append(Path(env_val))
    candidates.extend(
        [
            Path(r"C:\Program Files\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
            Path(r"E:\Software\Siemens\Tecnomatix Plant Simulation 13\Examples\Demo\DeliveryConcept.spp"),
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    return None


@pytest.mark.real_com
def test_live_templates_match_reference_and_parallel_capacity() -> None:
    if os.environ.get("PSBRIDGE_REAL_COM") != "1":
        pytest.skip("set PSBRIDGE_REAL_COM=1 to run live Plant Simulation template verification")
    template = _find_template()
    if template is None:
        pytest.skip("Plant Simulation template .spp not found")

    from psbridge.com.win32 import Win32ComBackend
    from psbridge.demo import build_demo_instance
    from psbridge.scheduling.policies import get_policy
    from psbridge.scheduling.reference import reference_schedule

    demo = build_demo_instance()
    none_due = Instance(
        instance_id="none_due",
        machines=(Machine("M1"), Machine("M2")),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 4.0), Operation("J1_O2", "J1", 1, ("M2",), 3.0)),
                release_time=0.0,
                due_date=None,
            ),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 5.0), Operation("J2_O2", "J2", 1, ("M2",), 2.0)),
                release_time=0.0,
                due_date=10.0,
            ),
        ),
    )
    par_cap2 = Instance(
        instance_id="par_cap2",
        machines=(Machine("M1", capacity=2), Machine("M2", capacity=1)),
        jobs=(
            Job(
                "J1",
                (Operation("J1_O1", "J1", 0, ("M1",), 10.0), Operation("J1_O2", "J1", 1, ("M2",), 2.0)),
                release_time=0.0,
            ),
            Job(
                "J2",
                (Operation("J2_O1", "J2", 0, ("M1",), 3.0), Operation("J2_O2", "J2", 1, ("M2",), 2.0)),
                release_time=0.0,
            ),
        ),
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="psb-live-tpl-"))
    backend = None
    try:
        base_spp = tmp_dir / "base.spp"
        shutil.copy2(template, base_spp)
        backend = Win32ComBackend(model_root=tmp_dir, quit_on_disconnect=True)
        backend.connect()

        def run_case(label: str, inst: Instance, policy: str) -> tuple[list[tuple[str, str, str, float, float]], int]:
            if backend.model_path is not None:
                backend.close_model()
            backend.load_model(str(base_spp))
            backend.clone_model(str(tmp_dir / f"{label}.spp"))
            frame = backend.default_frame_path
            spec = build_model_spec(inst)
            backend.execute_simtalk(T.build_model_source(spec, frame_path=frame))
            backend.execute_simtalk(T.initialize_tables_source(inst, frame_path=frame))
            for name, src in T.method_sources(policy, frame_path=frame).items():
                backend.set_method_source(f"{frame}.{name}", src)
            backend.reset()
            backend.start(end_time=200.0)
            assert backend.wait_until_finished(timeout=15.0) is True
            nrows = int(float(str(backend.evaluate(f"to_str({frame}.{RESULT_TABLE}.yDim)"))))
            rows = [
                (
                    str(backend.read_table_cell(f"{frame}.{RESULT_TABLE}", r, 1)),
                    str(backend.read_table_cell(f"{frame}.{RESULT_TABLE}", r, 2)),
                    str(backend.read_table_cell(f"{frame}.{RESULT_TABLE}", r, 3)),
                    round(float(backend.read_table_cell(f"{frame}.{RESULT_TABLE}", r, 4)), 6),
                    round(float(backend.read_table_cell(f"{frame}.{RESULT_TABLE}", r, 5)), 6),
                )
                for r in range(1, nrows + 1)
            ]
            drain_in = int(float(str(backend.evaluate(f"to_str({frame}.Drain_Output.StatNumIn)"))))
            backend.execute_simtalk(T.read_results_source(frame_path=frame))
            return rows, drain_in

        for policy in ("fifo", "spt", "edd"):
            ps_rows, drain_in = run_case(f"demo_{policy}", demo, policy)
            ref = [
                (o.op_id, o.job_id, o.machine_id, round(o.start, 6), round(o.end, 6))
                for o in reference_schedule(demo, get_policy(policy))
            ]
            assert ps_rows == ref
            assert drain_in == len(demo.jobs)

        ps_rows, drain_in = run_case("none_due_edd", none_due, "edd")
        ref = [
            (o.op_id, o.job_id, o.machine_id, round(o.start, 6), round(o.end, 6))
            for o in reference_schedule(none_due, get_policy("edd"))
        ]
        assert ps_rows == ref
        assert drain_in == len(none_due.jobs)

        ps_rows, drain_in = run_case("par_cap2_fifo", par_cap2, "fifo")
        assert ps_rows == [
            ("J1_O1", "J1", "M1", 0.0, 10.0),
            ("J2_O1", "J2", "M1", 0.0, 3.0),
            ("J2_O2", "J2", "M2", 3.0, 5.0),
            ("J1_O2", "J1", "M2", 10.0, 12.0),
        ]
        assert drain_in == len(par_cap2.jobs)
    finally:
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.disconnect()
        backend = None
        gc.collect()
        shutil.rmtree(tmp_dir, ignore_errors=True)
