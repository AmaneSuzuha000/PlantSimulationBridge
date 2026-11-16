"""Tests for the measured dispatch/schedule builders.

These assert the exact SimTalk text, because the text is the contract: it has
been executed against Plant Simulation 13 (scripts/real_machine_dispatch_smoke.py)
and the numbers reconciled. A change here invalidates that evidence.
"""

from __future__ import annotations

import pytest

from psbridge.errors import ValidationError
from psbridge.simtalk.dispatch import (
    MU_CLASS,
    SCHEDULE_TIME_OF_GENERATION,
    ScheduledJob,
    build_exit_control_program,
    build_queue_rule,
    build_schedule_source_statements,
)

FRAME = ".Models.Demo.Frame"
P1 = FRAME + ".Machine1"
P2 = FRAME + ".Machine2"


def test_schedule_statements_rename_before_use() -> None:
    statements = build_schedule_source_statements(ScheduledJob("JobA", 0, 6), FRAME)
    assert statements[0] == ".MaterialFlow.Source.createObject(" + FRAME + ", 120, 240);"
    # createObject takes no name, so the rename must address the appended node
    assert statements[1] == FRAME + ".node(" + FRAME + ".NumNodes).Name := \"JobA\";"
    # every later statement addresses the object by its new name
    assert all(FRAME + ".JobA" in statement for statement in statements[2:])


def test_schedule_statements_set_the_proven_attributes_only() -> None:
    text = " ".join(build_schedule_source_statements(ScheduledJob("JobA", 250, 4), FRAME))
    assert MU_CLASS in text
    assert 'TimeOfGeneration := "' + SCHEDULE_TIME_OF_GENERATION + '"' in text
    assert ".Number := 4;" in text
    assert ".Start := 250;" in text
    # Source has no MU and no Count attribute on this build -- never emit them
    assert ".MU :=" not in text
    assert ".Count :=" not in text


def test_schedule_statements_are_deterministic() -> None:
    job = ScheduledJob("JobB", 12.5, 3)
    assert build_schedule_source_statements(job, FRAME) == build_schedule_source_statements(job, FRAME)


def test_fractional_release_time_is_trimmed() -> None:
    text = " ".join(build_schedule_source_statements(ScheduledJob("J", 12.5, 1), FRAME))
    assert ".Start := 12.5;" in text
    whole = " ".join(build_schedule_source_statements(ScheduledJob("J", 250.0, 1), FRAME))
    assert ".Start := 250;" in whole


def test_rejects_bad_jobs() -> None:
    with pytest.raises(ValidationError):
        ScheduledJob("bad name!", 0, 1)
    with pytest.raises(ValidationError):
        ScheduledJob("J", -1, 1)
    with pytest.raises(ValidationError):
        ScheduledJob("J", 0, 0)


def test_rejects_relative_frame_path() -> None:
    with pytest.raises(ValidationError):
        build_schedule_source_statements(ScheduledJob("J", 0, 1), "Frame")


def test_queue_rule_picks_the_least_loaded_machine() -> None:
    program = build_exit_control_program((P1, P2))
    assert program.startswith(":boolean is do ")
    assert program.endswith(" return true; end;")
    # each branch must move to a DIFFERENT machine, or the rule is a no-op
    assert "@.move(" + P1 + ");" in program
    assert "@.move(" + P2 + ");" in program
    assert "(P1.statNumIn - P1.statNumOut)".replace("P1", P1) in program


def test_queue_rule_requires_two_candidates() -> None:
    with pytest.raises(ValidationError):
        build_queue_rule((P1,))


def test_queue_rule_rejects_relative_candidate() -> None:
    with pytest.raises(ValidationError):
        build_queue_rule((P1, "Machine2"))


def test_three_machines_get_three_distinct_targets() -> None:
    p3 = FRAME + ".Machine3"
    program = build_exit_control_program((P1, P2, p3))
    for target in (P1, P2, p3):
        assert "@.move(" + target + ");" in program


def test_rejects_keyword_job_name_and_formats_extreme_releases() -> None:
    with pytest.raises(ValidationError, match="keyword"):
        build_schedule_source_statements(ScheduledJob("end", 0, 1), FRAME)
    large = " ".join(build_schedule_source_statements(ScheduledJob("J", 3_000_000_000.0, 1), FRAME))
    assert ".Start := 3000000000.0;" in large
    tiny = " ".join(build_schedule_source_statements(ScheduledJob("J", 1e-6, 1), FRAME))
    assert ".Start := 1e-06;" in tiny
