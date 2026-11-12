"""Acceptance test for the closed-loop demo (FIFO/SPT/EDD vs reference oracle).

Runs without Plant Simulation: the demo uses the deterministic FakeDispatchEngine
and the independent reference oracle, then persists the records.
"""

from __future__ import annotations

import json
from pathlib import Path

from psbridge import demo


def test_demo_instance_is_valid_and_deterministic() -> None:
    first = demo.build_demo_instance()
    second = demo.build_demo_instance()
    assert first == second
    assert first.total_operations == 15
    assert len(first.jobs) == 5
    assert len(first.machines) == 4


def test_demo_runs_all_policies_on_all_seeds(tmp_path: Path) -> None:
    _instance, records = demo.run_comparison(tmp_path / "work", seeds=(0, 1), jitter=0.0, policies=("fifo", "spt", "edd"))
    assert len(records) == 6
    assert all(record.status == "valid" for record in records), [(r.policy, r.seed, r.status, r.validity.to_dict()) for r in records]
    assert all(record.metrics is not None for record in records)
    summary = demo.summarize(records)
    assert [row["policy"] for row in summary] == ["edd", "fifo", "spt"]
    assert all(row["valid"] == 2 for row in summary)
    assert all(row["invalid"] == 0 and row["failed"] == 0 for row in summary)
    # With zero jitter the same policy/seed is deterministic across replications.
    fifo_makespans = {record.metrics.makespan for record in records if record.policy == "fifo"}
    assert len(fifo_makespans) == 1


def test_demo_jitter_changes_durations_but_remains_valid(tmp_path: Path) -> None:
    from psbridge.experiment.instance import sample_variant

    instance = demo.build_demo_instance()
    first = sample_variant(instance, 7, 0.2)
    second = sample_variant(instance, 7, 0.2)
    third = sample_variant(instance, 8, 0.2)
    assert first == second
    assert first.content_hash() == second.content_hash()
    assert first.content_hash() != third.content_hash()
    assert first.content_hash() != instance.content_hash()


def test_demo_cli_json_output(tmp_path: Path, capsys) -> None:
    exit_code = demo.main(["--work-dir", str(tmp_path / "cli"), "--seeds", "0", "--jitter", "0", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert payload["instance"]["instance_id"] == "demo_5x3"
    assert {row["policy"] for row in payload["summary"]} == {"fifo", "spt", "edd"}
    assert all(row["valid"] == 1 for row in payload["summary"])


def test_demo_rejects_unknown_policy(tmp_path: Path) -> None:
    try:
        demo.main(["--work-dir", str(tmp_path / "bad"), "--policies", "nope"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected SystemExit for unknown policy")
