"""Contract tests for the buffered job-shop SimTalk generator.

Covers the verified uniform-duration fast path (40 drained crossover parts,
exact 120 s / 200 s machine work) and the per-operation dynamic-duration path
using MU ``OpDuration`` stamping and ``EntranceCtrlBeforeActions``.
"""
from __future__ import annotations

import pytest

from psbridge.errors import ValidationError
from psbridge.simtalk.jobshop import (
    Job,
    Operation,
    build_job_shop_statements,
    machine_durations,
    machine_schedule,
)

FRAME = ".Models.X.Frame"


def two_jobs():
    return [
        Job("A", 20, 0, 5, (Operation("M1", 3.0), Operation("M2", 5.0))),
        Job("B", 20, 0, 5, (Operation("M2", 5.0), Operation("M1", 3.0))),
    ]


def test_declares_the_two_tracking_attributes_first():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    assert statements[0] == '.MUs.Entity.createAttr("JobId", "integer");'
    assert statements[1] == '.MUs.Entity.createAttr("OpIndex", "integer");'


def test_buffer_exists_for_every_operation_after_the_first():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    assert any('Name := "Buf_A_2";' in s for s in statements)
    assert any('Name := "Buf_B_2";' in s for s in statements)


def test_buffer_is_wired_to_the_machine_it_feeds():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    assert ".MaterialFlow.Connector.connect(" + FRAME + ".Buf_A_2, " + FRAME + ".M_M2);" in statements
    assert ".MaterialFlow.Connector.connect(" + FRAME + ".Buf_B_2, " + FRAME + ".M_M1);" in statements


def test_method_is_created_programmed_and_wired_in_three_consecutive_statements():
    """A method handle is captured and attached immediately after creation."""
    statements = build_job_shop_statements(two_jobs(), FRAME)
    create = statements.index(".InformationFlow.&Method.createObject(" + FRAME + ", 100, 600);")
    assert statements[create + 1].startswith(FRAME + ".node(" + FRAME + ".NumNodes).Program := ")
    assert statements[create + 2] == FRAME + ".Src_A.ExitCtrl := " + FRAME + ".node(" + FRAME + ".NumNodes);"


def test_source_program_stamps_job_number_and_first_operation():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    body = next(s for s in statements if "Program" in s and "@.JobId := 1" in s)
    assert "@.OpIndex := 1" in body
    assert "@.move(" + FRAME + ".M_M1)" in body


def test_machine_program_is_flat_guards_that_return_after_moving():
    """The nested else form stalled on the real product; flat guards are the fix."""
    statements = build_job_shop_statements(two_jobs(), FRAME)
    bodies = [s for s in statements if "Program" in s and "@.OpIndex = " in s]
    assert len(bodies) == 2
    for body in bodies:
        assert " else " not in body
        assert body.count("return true;") >= 2


def test_crossover_routes_through_the_buffer_and_last_operation_to_the_drain():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    m1 = next(s for s in statements if "Program" in s and "@.JobId = 1 then if @.OpIndex = 1" in s)
    assert "@.OpIndex := 2" in m1 and "@.move(" + FRAME + ".Buf_A_2)" in m1
    assert "@.move(" + FRAME + ".Drain1)" in m1


def test_sources_use_number_adjustable_because_interval_adjustable_ignores_number():
    """Measured: with Interval adjustable the Number attribute has no effect."""
    statements = build_job_shop_statements(two_jobs(), FRAME)
    assert any('TimeOfGeneration := "Number adjustable";' in s for s in statements)
    assert sum(1 for s in statements if 'TimeOfGeneration := "Number adjustable";' in s) == 2


def test_machine_proctime_comes_from_its_operation_duration():
    statements = build_job_shop_statements(two_jobs(), FRAME)
    assert FRAME + ".M_M1.ProcTime := 3;" in statements
    assert FRAME + ".M_M2.ProcTime := 5;" in statements


def test_strict_machine_schedule_rejects_mixed_durations_and_dynamic_mode_supports_them():
    jobs = [
        Job("A", 2, 0, 1, (Operation("M1", 3.0), Operation("M2", 5.0))),
        Job("B", 2, 0, 1, (Operation("M2", 2.0), Operation("M1", 7.0))),
    ]
    with pytest.raises(ValidationError):
        machine_schedule(jobs, strict=True)

    durs = machine_durations(jobs)
    assert durs == {"M1": (3.0, 7.0), "M2": (5.0, 2.0)}

    statements = build_job_shop_statements(jobs, FRAME)
    assert statements[2] == '.MUs.Entity.createAttr("OpDuration", "real");'
    assert FRAME + ".M_M1.EntranceCtrlBeforeActions := true;" in statements
    assert FRAME + ".M_M2.EntranceCtrlBeforeActions := true;" in statements
    assert any(
        "@.OpDuration := 3;" in s and "@.move(" + FRAME + ".M_M1)" in s
        for s in statements
    )
    assert any(
        "@.OpDuration := 5;" in s and "@.move(" + FRAME + ".Buf_A_2)" in s
        for s in statements
    )
    assert any(
        "@.OpDuration := 7;" in s and "@.move(" + FRAME + ".Buf_B_2)" in s
        for s in statements
    )
    assert any(
        "?.ProcTime := @.OpDuration;" in s
        for s in statements
    )
    assert any(s.endswith(".M_M1.EntranceCtrl := " + FRAME + ".node(" + FRAME + ".NumNodes);") for s in statements)


def test_partial_heterogeneous_shop_only_hooks_mixed_machines():
    jobs = [
        Job("A", 2, 0, 1, (Operation("M1", 3.0), Operation("M2", 5.0))),
        Job("B", 2, 0, 1, (Operation("M2", 5.0), Operation("M1", 7.0))),
    ]
    statements = build_job_shop_statements(jobs, FRAME)
    assert FRAME + ".M_M1.EntranceCtrlBeforeActions := true;" in statements
    assert FRAME + ".M_M2.EntranceCtrlBeforeActions := true;" not in statements
    assert not any(".M_M2.EntranceCtrl := " in s for s in statements)


def test_a_job_without_operations_is_rejected():
    with pytest.raises(ValidationError):
        Job("A", 1, 0, 1, ())


def test_an_empty_shop_is_rejected():
    with pytest.raises(ValidationError):
        build_job_shop_statements([], FRAME)


def test_single_operation_job_goes_straight_to_the_drain():
    statements = build_job_shop_statements([Job("A", 5, 0, 1, (Operation("M1", 2.0),))], FRAME)
    body = next(s for s in statements if "Program" in s and "@.OpIndex = " in s)
    assert "@.move(" + FRAME + ".Drain1)" in body


def test_rejects_overlong_derived_object_names_and_keyword_drain():
    long_job = Job("J" * 61, 1, 0, 1, (Operation("M1", 2.0),))
    with pytest.raises(ValidationError):
        build_job_shop_statements([long_job], FRAME)

    long_machine = Job("A", 1, 0, 1, (Operation("M" * 63, 2.0),))
    with pytest.raises(ValidationError):
        build_job_shop_statements([long_machine], FRAME)

    valid_job = Job("A", 1, 0, 1, (Operation("M1", 2.0),))
    with pytest.raises(ValidationError, match="keyword"):
        build_job_shop_statements([valid_job], FRAME, drain="end")


def test_large_and_submillisecond_durations_keep_valid_simtalk_literals():
    jobs = [Job("A", 1, 0.0, 1.0, (Operation("M1", 3_000_000_000.0), Operation("M2", 1e-6)))]
    text = "\n".join(build_job_shop_statements(jobs, FRAME))
    assert "3000000000.0;" in text
    assert "1e-06;" in text