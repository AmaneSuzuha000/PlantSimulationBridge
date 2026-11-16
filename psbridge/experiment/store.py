"""Append-only run storage and atomic experiment summaries.

Layout under ResultStore.root
---------------------------
runs.jsonl                  one RunRecord.to_dict() JSON object per line, UTF-8
summaries/<exp>.json        one aggregated summary per experiment
summaries/<exp>.json.tmp*   transient temp file (never left behind on success)

Durability
----------
append_run writes one complete line, flushes and fsyncs before returning, so a
crash cannot lose an already-recorded run. A crash *during* a write can leave a
partial trailing line; read_runs() therefore tolerates an unparsable final line
(it is reported via the malformed counter) while still failing loudly on
malformed lines in the middle of the file, which would mean real corruption.

Summaries are written with tempfile + flush + fsync + os.replace, so a reader
always sees either the previous summary or the new one.

The summary is a pure function of the stored records (no timestamps), so
re-running it on the same runs.jsonl produces byte-identical output. That makes
summaries diffable in a research log.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from psbridge.errors import ValidationError
from psbridge.experiment.contracts import (
    RUN_STATUSES,
    Metrics,
    Provenance,
    RunRecord,
    ValidityReport,
)
from psbridge.schema import ScheduledOperation

__all__ = ["RUNS_FILENAME", "SUMMARY_DIRNAME", "ResultStore"]

RUNS_FILENAME = "runs.jsonl"
SUMMARY_DIRNAME = "summaries"
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

#: Metric fields aggregated in a summary (utilization is aggregated per machine).
METRIC_FIELDS = (
    "makespan",
    "total_tardiness",
    "weighted_tardiness",
    "max_tardiness",
    "tardy_jobs",
    "total_flow_time",
    "mean_flow_time",
    "avg_wip",
    "throughput",
)


def _stat_block(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "min": None, "max": None, "total": None}
    total = float(sum(values))
    return {
        "count": len(values),
        "mean": total / len(values),
        "min": float(min(values)),
        "max": float(max(values)),
        "total": total,
    }


def _accumulate_metrics(
    metrics: Mapping[str, Any] | None,
    buckets: dict[str, list[float]],
    utilization: dict[str, list[float]],
) -> None:
    if not isinstance(metrics, Mapping):
        return
    for field in METRIC_FIELDS:
        value = metrics.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            buckets[field].append(float(value))
    raw_util = metrics.get("utilization")
    if isinstance(raw_util, Mapping):
        for machine_id, value in raw_util.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                utilization[str(machine_id)].append(float(value))


def _finalize_metrics(
    buckets: Mapping[str, Sequence[float]],
    utilization: Mapping[str, Sequence[float]],
) -> dict[str, Any]:
    aggregates: dict[str, Any] = {field: _stat_block(buckets[field]) for field in METRIC_FIELDS}
    aggregates["utilization"] = {
        machine_id: _stat_block(values) for machine_id, values in sorted(utilization.items())
    }
    return aggregates


def _aggregate_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[float]] = {field: [] for field in METRIC_FIELDS}
    utilization: dict[str, list[float]] = defaultdict(list)
    for record in records:
        _accumulate_metrics(record.get("metrics"), buckets, utilization)
    return _finalize_metrics(buckets, utilization)


def summarize_records(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate run rows (as stored on disk) into a summary document."""
    run_count = 0
    valid_run_count = 0
    status_counts: Counter[str] = Counter()
    policy_total: Counter[str] = Counter()
    policy_valid_counts: Counter[str] = Counter()
    policy_status: dict[str, Counter[str]] = defaultdict(Counter)
    experiment_counts: Counter[str] = Counter()
    validity_failures: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    engine_kinds: Counter[str] = Counter()
    backends: Counter[str] = Counter()
    instance_hashes: dict[str, set[str]] = defaultdict(set)
    global_buckets: dict[str, list[float]] = {field: [] for field in METRIC_FIELDS}
    global_util: dict[str, list[float]] = defaultdict(list)
    policy_buckets: dict[str, dict[str, list[float]]] = {}
    policy_util: dict[str, dict[str, list[float]]] = {}

    for row in records:
        run_count += 1
        status = str(row.get("status", ""))
        policy = str(row.get("policy", ""))
        experiment_id = str(row.get("experiment_id", ""))
        status_counts[status] += 1
        policy_total[policy] += 1
        policy_status[policy][status] += 1
        if policy not in policy_buckets:
            policy_buckets[policy] = {field: [] for field in METRIC_FIELDS}
            policy_util[policy] = defaultdict(list)
        experiment_counts[experiment_id] += 1
        engine_kinds[str(row.get("engine_kind", ""))] += 1
        backends[str(row.get("backend", ""))] += 1
        instance_hashes[experiment_id].add(str(row.get("instance_hash", "")))
        validity = row.get("validity")
        if isinstance(validity, Mapping):
            checks = validity.get("checks")
            if isinstance(checks, Iterable):
                for check in checks:
                    if isinstance(check, Mapping) and not check.get("passed", True):
                        validity_failures[str(check.get("name", ""))] += 1
        error = row.get("error")
        if error:
            if status == "failed":
                failure_reasons[str(error)] += 1
            elif status == "invalid":
                invalid_reasons[str(error)] += 1
        if status == "valid":
            valid_run_count += 1
            policy_valid_counts[policy] += 1
            metrics = row.get("metrics")
            _accumulate_metrics(metrics, global_buckets, global_util)
            _accumulate_metrics(metrics, policy_buckets[policy], policy_util[policy])

    by_policy: dict[str, Any] = {}
    for policy in sorted(policy_status):
        by_policy[policy] = {
            "run_count": policy_total[policy],
            "counts_by_status": {status: policy_status[policy].get(status, 0) for status in RUN_STATUSES},
            "valid_run_count": policy_valid_counts[policy],
            "metrics": _finalize_metrics(policy_buckets[policy], policy_util[policy]),
        }
    return {
        "run_count": run_count,
        "valid_run_count": valid_run_count,
        "counts_by_status": {status: status_counts.get(status, 0) for status in RUN_STATUSES},
        "counts_by_policy": {
            policy: {status: policy_status[policy].get(status, 0) for status in RUN_STATUSES}
            for policy in sorted(policy_status)
        },
        "counts_by_experiment": {key: experiment_counts[key] for key in sorted(experiment_counts)},
        "metrics": _finalize_metrics(global_buckets, global_util),
        "by_policy": by_policy,
        "validity_failure_frequencies": {
            name: count for name, count in sorted(validity_failures.items(), key=lambda item: (-item[1], item[0]))
        },
        "failure_reasons": {
            reason: count for reason, count in sorted(failure_reasons.items(), key=lambda item: (-item[1], item[0]))
        },
        "invalid_reasons": {
            reason: count for reason, count in sorted(invalid_reasons.items(), key=lambda item: (-item[1], item[0]))
        },
        "engine_kinds": {kind: count for kind, count in sorted(engine_kinds.items())},
        "backends": {backend: count for backend, count in sorted(backends.items())},
        "instance_hashes": {key: sorted(values) for key, values in sorted(instance_hashes.items())},
    }


class ResultStore:
    """Crash-safe run log plus atomic per-experiment summaries."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self.malformed_lines: list[int] = []

    # -- paths ----------------------------------------------------------
    @property
    def runs_path(self) -> Path:
        return self.root / RUNS_FILENAME

    @property
    def summary_dir(self) -> Path:
        return self.root / SUMMARY_DIRNAME

    def summary_path(self, experiment_id: str) -> Path:
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            raise ValidationError("experiment_id must be a non-empty string", details={"experiment_id": repr(experiment_id)})
        if "\x00" in experiment_id or ".." in Path(experiment_id.replace("\\", "/")).parts:
            raise ValidationError("experiment_id cannot contain path traversal segments", details={"experiment_id": repr(experiment_id)})
        safe = _UNSAFE_FILENAME_RE.sub("_", experiment_id.strip())
        if safe in ("", ".", "..") or safe.startswith("."):
            raise ValidationError("experiment_id produces an invalid summary filename", details={"experiment_id": repr(experiment_id)})
        summary_root = self.summary_dir.resolve(strict=False)
        candidate = (self.summary_dir / (safe + ".json")).resolve(strict=False)
        if candidate.parent != summary_root:
            raise ValidationError("summary path escapes summary directory", details={"experiment_id": repr(experiment_id)})
        return self.summary_dir / (safe + ".json")

    def ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.summary_dir.mkdir(parents=True, exist_ok=True)

    # -- run log --------------------------------------------------------
    def append_run(self, record: RunRecord) -> dict[str, Any]:
        """Append one run and return the row that was written."""
        if not isinstance(record, RunRecord):
            raise ValidationError(
                "append_run expects a RunRecord", details={"type": type(record).__name__}
            )
        row = record.to_dict()
        line = json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=False)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.runs_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return row

    def append_runs(self, records: Iterable[RunRecord]) -> int:
        count = 0
        for record in records:
            self.append_run(record)
            count += 1
        return count

    def read_run_dicts(self, *, strict: bool = False) -> list[dict[str, Any]]:
        """Read stored run rows. A partial trailing line is tolerated unless strict."""
        with self._lock:
            if not self.runs_path.exists():
                self.malformed_lines = []
                return []
            raw = self.runs_path.read_text(encoding="utf-8")
            # Split strictly on '\n' (not str.splitlines()) so U+2028/U+2029 inside
            # JSON strings never split a valid record across lines.
            ends_with_newline = raw.endswith("\n")
            lines = raw.split("\n")
            if ends_with_newline:
                lines.pop()
            rows: list[dict[str, Any]] = []
            malformed: list[int] = []
            last_index = len(lines) - 1
            for index, raw_line in enumerate(lines):
                stripped = raw_line.removesuffix("\r").strip()
                is_partial_tail = index == last_index and not ends_with_newline
                if not stripped:
                    if is_partial_tail:
                        break
                    malformed.append(index + 1)
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    if is_partial_tail:
                        break
                    malformed.append(index + 1)
                    continue
                if not isinstance(row, dict):
                    malformed.append(index + 1)
                    continue
                rows.append(row)
            self.malformed_lines = malformed
            if strict and malformed:
                raise ValidationError(
                    "runs.jsonl contains malformed lines", details={"lines": malformed, "path": str(self.runs_path)}
                )
            return rows

    def read_runs(self, *, strict: bool = False) -> list[dict[str, Any]]:
        """Alias for read_run_dicts (rows are plain dicts)."""
        return self.read_run_dicts(strict=strict)

    def read_records(self, *, strict: bool = False) -> list[RunRecord]:
        """Reconstruct RunRecord objects from the log (round-trip helper)."""
        return [run_record_from_dict(row) for row in self.read_run_dicts(strict=strict)]

    # -- summaries ------------------------------------------------------
    def summarize(self, rows: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        if rows is None:
            rows = self.read_run_dicts()
        return summarize_records(rows)

    def write_summary(
        self,
        experiment_id: str,
        records: Iterable[RunRecord] | None = None,
        *,
        rows: Iterable[Mapping[str, Any]] | None = None,
    ) -> Path:
        """Atomically write (and return) the summary for one experiment."""
        if rows is not None:
            # Explicit rows/records are the caller's selection; do not re-filter.
            selected = [dict(row) for row in rows]
        elif records is not None:
            selected = [record.to_dict() for record in records]
        else:
            selected = [row for row in self.read_run_dicts() if row.get("experiment_id") == experiment_id]
        document = summarize_records(selected)
        document["experiment_id"] = experiment_id
        return self._atomic_write_json(self.summary_path(experiment_id), document)

    def write_global_summary(self, name: str = "all_runs") -> Path:
        document = self.summarize()
        document["experiment_id"] = name
        return self._atomic_write_json(self.summary_path(name), document)

    def _atomic_write_json(self, target: Path, document: Mapping[str, Any]) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
        handle_fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    def summary_equals(self, experiment_id: str, other: Mapping[str, Any]) -> bool:
        path = self.summary_path(experiment_id)
        if not path.exists():
            return False
        return json.loads(path.read_text(encoding="utf-8")) == dict(other)


def run_record_from_dict(data: Mapping[str, Any]) -> RunRecord:
    """Rebuild a RunRecord from its to_dict() representation."""
    if not isinstance(data, Mapping):
        raise ValidationError("run record must be a mapping", details={"type": type(data).__name__})
    validity_data = data.get("validity") or {"valid": False, "checks": []}
    if not isinstance(validity_data, Mapping):
        raise ValidationError("run record 'validity' must be a mapping")
    checks = tuple(
        _check_from_dict(check) for check in validity_data.get("checks", ())
    )
    validity = ValidityReport.from_checks(checks)
    metrics_data = data.get("metrics")
    metrics = None if metrics_data is None else Metrics.from_dict(metrics_data)
    provenance_data = data["provenance"]
    if not isinstance(provenance_data, Mapping):
        raise ValidationError("run record 'provenance' must be a mapping")
    provenance = Provenance(
        bridge_version=provenance_data["bridge_version"],
        instance_hash=provenance_data["instance_hash"],
        policy=provenance_data["policy"],
        seed=provenance_data["seed"],
        duration_jitter=provenance_data["duration_jitter"],
        backend=provenance_data["backend"],
        engine_kind=provenance_data["engine_kind"],
        timestamp=provenance_data["timestamp"],
        python_version=provenance_data["python_version"],
        platform=provenance_data["platform"],
        model_hash=provenance_data.get("model_hash"),
    )
    schedule = tuple(
        ScheduledOperation(
            op_id=item["op_id"],
            job_id=item["job_id"],
            machine_id=item["machine_id"],
            start=item["start"],
            end=item["end"],
        )
        for item in data.get("schedule", ())
    )
    return RunRecord(
        run_id=data["run_id"],
        experiment_id=data["experiment_id"],
        instance_id=data["instance_id"],
        instance_hash=data["instance_hash"],
        policy=data["policy"],
        seed=data["seed"],
        replication=data["replication"],
        status=data["status"],
        metrics=metrics,
        schedule=schedule,
        validity=validity,
        provenance=provenance,
        backend=data["backend"],
        engine_kind=data["engine_kind"],
        error=data.get("error"),
    )


def _check_from_dict(data: Mapping[str, Any]):
    from psbridge.experiment.contracts import CheckResult

    return CheckResult(name=data["name"], passed=bool(data["passed"]), detail=data.get("detail", ""))
