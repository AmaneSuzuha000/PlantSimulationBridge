"""Mapping from a scheduling instance to Plant Simulation line objects.

The experiment layer is engine agnostic; this module is where the instance is
translated into the names a real Plant Simulation model must contain. The
mapping is deliberately boring and total: every job gets a source and every
machine gets a buffer plus a station, and the operation route table records the
processing time per (job, step).

Naming scheme
-------------
======  =================  ==========================
kind    name               one per
======  =================  ==========================
source  `Src_<job>`        job
buffer  `Buf_<machine>`    machine
station `Stn_<machine>`    machine
drain   `Drain_Output`       instance
======  =================  ==========================

Every derived name is validated with :func:`psbridge.schema.validate_identifier`
and the length limit is checked on the *derived* name, so a generated model can
never contain a name that SimTalk would have to quote or escape. A job id that
is itself valid can still be rejected here when `Src_` + job id exceeds the
identifier limit -- that is a hard failure on purpose (silently truncating a
name would eventually collide two objects).

Routing caveat
--------------
A machine is shared by several jobs, so a station's successor depends on the
MU being processed. Plant Simulation conveys that with one connector plus a
routing decision in the station's exit control; the spec therefore marks those
links `requires_routing=True` and the model template emits them in a link
table instead of as a fixed connector. This keeps the assumption visible
instead of encoding a wrong line.

Everything here is pure data: no COM, no SimTalk, no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from psbridge.errors import ValidationError
from psbridge.schema import (
    MAX_IDENTIFIER_LENGTH,
    Instance,
    validate_identifier,
    validate_number,
)

__all__ = [
    "BUFFER_CLASS",
    "DRAIN_CLASS",
    "JOB_COLUMNS",
    "JOB_TABLE",
    "LINK_COLUMNS",
    "LINK_TABLE",
    "MACHINE_COLUMNS",
    "MACHINE_TABLE",
    "PARALLEL_STATION_CLASS",
    "READY_COLUMNS",
    "READY_TABLE",
    "RESULT_COLUMNS",
    "RESULT_TABLE",
    "ROUTE_COLUMNS",
    "ROUTE_TABLE",
    "SOURCE_CLASS",
    "STATION_CLASS",
    "TABLE_NAMES",
    "BufferSpec",
    "ConnectionSpec",
    "DrainSpec",
    "LineModelSpec",
    "SourceSpec",
    "StationSpec",
    "TableSpec",
    "build_model_spec",
    "station_class_for",
    "summarize_model_spec",
]

# -- object classes ----------------------------------------------------------
#: Plant Simulation class paths verified on Plant Simulation 13.
SOURCE_CLASS = ".MaterialFlow.Source"
STATION_CLASS = ".MaterialFlow.SingleProc"
PARALLEL_STATION_CLASS = ".MaterialFlow.ParallelProc"
BUFFER_CLASS = ".MaterialFlow.Buffer"
DRAIN_CLASS = ".MaterialFlow.Drain"


def station_class_for(capacity: int) -> str:
    """Return the Plant Simulation class path for a station of ``capacity``."""
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValidationError("station capacity must be a positive integer", details={"capacity": capacity})
    return STATION_CLASS if capacity == 1 else PARALLEL_STATION_CLASS

# -- table names and columns -------------------------------------------------
ROUTE_TABLE = "Table_Routes"
JOB_TABLE = "Table_Jobs"
MACHINE_TABLE = "Table_Machines"
LINK_TABLE = "Table_Links"
READY_TABLE = "Table_Ready"
RESULT_TABLE = "Table_Results"

TABLE_NAMES: tuple[str, ...] = (ROUTE_TABLE, JOB_TABLE, MACHINE_TABLE, LINK_TABLE, READY_TABLE, RESULT_TABLE)

ROUTE_COLUMNS: tuple[str, ...] = (
    "JobID",
    "Step",
    "OperationID",
    "MachineID",
    "Duration",
    "ReleaseTime",
    "DueDate",
    "HasDueDate",
)
JOB_COLUMNS: tuple[str, ...] = ("JobID", "ReleaseTime", "DueDate", "HasDueDate", "Weight", "OperationCount")
MACHINE_COLUMNS: tuple[str, ...] = ("MachineID", "Capacity", "BufferName", "StationName")
LINK_COLUMNS: tuple[str, ...] = ("SourceName", "TargetName", "JobID", "Step", "RequiresRouting")
READY_COLUMNS: tuple[str, ...] = (
    "OpID",
    "JobID",
    "Step",
    "MachineID",
    "Duration",
    "ReadySince",
    "JobRelease",
    "DueDate",
    "HasDueDate",
    "Weight",
)
RESULT_COLUMNS: tuple[str, ...] = ("OpID", "JobID", "MachineID", "Start", "End", "Duration")


def _derived(prefix: str, raw: str, *, field_name: str) -> str:
    """Build and validate a derived object name (length checked after prefixing)."""
    name = prefix + raw
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            field_name + " exceeds the SimTalk identifier length limit once prefixed",
            details={"name": name, "limit": MAX_IDENTIFIER_LENGTH, "raw": raw, "prefix": prefix},
        )
    return validate_identifier(name, field_name=field_name)


def source_name(job_id: str) -> str:
    """Plant Simulation name of the source that releases `job_id`."""
    return _derived("Src_", job_id, field_name="source name")


def buffer_name(machine_id: str) -> str:
    """Plant Simulation name of the input buffer of `machine_id`."""
    return _derived("Buf_", machine_id, field_name="buffer name")


def station_name(machine_id: str) -> str:
    """Plant Simulation name of the station that executes `machine_id`."""
    return _derived("Stn_", machine_id, field_name="station name")


@dataclass(frozen=True)
class SourceSpec:
    """One material-flow source, one per job."""

    name: str
    job_id: str
    release_time: float
    due_date: float | None
    mu_count: int = 1

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="source name")
        validate_identifier(self.job_id, field_name="job_id")
        object.__setattr__(self, "release_time", validate_number(self.release_time, field_name="release_time", minimum=0.0))
        if self.due_date is not None:
            object.__setattr__(self, "due_date", validate_number(self.due_date, field_name="due_date", minimum=0.0))
        if isinstance(self.mu_count, bool) or not isinstance(self.mu_count, int) or self.mu_count < 1:
            raise ValidationError("mu_count must be a positive integer", details={"source": self.name})


@dataclass(frozen=True)
class BufferSpec:
    """One input buffer in front of a station."""

    name: str
    machine_id: str
    capacity: int = 0

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="buffer name")
        validate_identifier(self.machine_id, field_name="machine_id")
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity < 0:
            raise ValidationError("buffer capacity must be a non-negative integer", details={"buffer": self.name})


@dataclass(frozen=True)
class StationSpec:
    """One processing station per machine (``SingleProc`` for 1, ``ParallelProc`` for >1)."""

    name: str
    machine_id: str
    index: int
    capacity: int = 1

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="station name")
        validate_identifier(self.machine_id, field_name="machine_id")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValidationError("station index must be a non-negative integer", details={"station": self.name})
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity < 1:
            raise ValidationError("station capacity must be a positive integer", details={"station": self.name})


@dataclass(frozen=True)
class DrainSpec:
    """The single sink of the line."""

    name: str = "Drain_Output"

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="drain name")


@dataclass(frozen=True)
class ConnectionSpec:
    """One potential material-flow link.

    `requires_routing` is True when the source object can have several
    successors (a shared station or a job whose next step differs per job), in
    which case the connector target must be chosen at runtime by the generated
    routing method rather than fixed in the model.
    """

    source: str
    target: str
    job_id: str | None = None
    step_index: int | None = None
    requires_routing: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.source, field_name="connection source")
        validate_identifier(self.target, field_name="connection target")
        if self.job_id is not None:
            validate_identifier(self.job_id, field_name="connection job_id")
        if self.step_index is not None and (
            isinstance(self.step_index, bool) or not isinstance(self.step_index, int) or self.step_index < 0
        ):
            raise ValidationError("connection step_index must be a non-negative integer or None")
        if not isinstance(self.requires_routing, bool):
            raise ValidationError("requires_routing must be a bool")


@dataclass(frozen=True)
class TableSpec:
    """A DataTable the generated model must contain."""

    name: str
    columns: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_identifier(self.name, field_name="table name")
        if not isinstance(self.columns, tuple):
            object.__setattr__(self, "columns", tuple(self.columns))
        if not self.columns:
            raise ValidationError("table needs at least one column", details={"table": self.name})
        for column in self.columns:
            validate_identifier(column, field_name="table column")


@dataclass(frozen=True)
class LineModelSpec:
    """Complete, validated description of the Plant Simulation model objects."""

    instance_id: str
    instance_hash: str
    sources: tuple[SourceSpec, ...]
    buffers: tuple[BufferSpec, ...]
    stations: tuple[StationSpec, ...]
    drain: DrainSpec
    connections: tuple[ConnectionSpec, ...]
    tables: tuple[TableSpec, ...] = field(default_factory=tuple)
    _station_map: dict[str, StationSpec] = field(init=False, repr=False, compare=False)
    _buffer_map: dict[str, BufferSpec] = field(init=False, repr=False, compare=False)
    _source_map: dict[str, SourceSpec] = field(init=False, repr=False, compare=False)
    _table_map: dict[str, TableSpec] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_identifier(self.instance_id, field_name="instance_id")
        if not isinstance(self.instance_hash, str) or not self.instance_hash:
            raise ValidationError("instance_hash must be a non-empty string")
        for name, value in (
            ("sources", self.sources),
            ("buffers", self.buffers),
            ("stations", self.stations),
            ("connections", self.connections),
            ("tables", self.tables),
        ):
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))
        if not self.sources:
            raise ValidationError("line model needs at least one source")
        if not self.stations:
            raise ValidationError("line model needs at least one station")
        if not isinstance(self.drain, DrainSpec):
            raise ValidationError("drain must be a DrainSpec")
        object_names = self.object_names()
        if len(set(object_names)) != len(object_names):
            duplicates = sorted(name for name, count in Counter(object_names).items() if count > 1)
            raise ValidationError("duplicate Plant Simulation object names", details={"names": duplicates})
        table_names = tuple(table.name for table in self.tables)
        if len(set(table_names)) != len(table_names):
            duplicates = sorted(name for name, count in Counter(table_names).items() if count > 1)
            raise ValidationError("duplicate table names", details={"names": duplicates})
        object.__setattr__(self, "_station_map", {station.machine_id: station for station in self.stations})
        object.__setattr__(self, "_buffer_map", {buffer.machine_id: buffer for buffer in self.buffers})
        object.__setattr__(self, "_source_map", {source.job_id: source for source in self.sources})
        object.__setattr__(self, "_table_map", {table.name: table for table in self.tables})

    # -- helpers ---------------------------------------------------------
    def object_names(self) -> tuple[str, ...]:
        """Every object name of the model, in creation order."""
        names: list[str] = [source.name for source in self.sources]
        names.extend(buffer.name for buffer in self.buffers)
        names.extend(station.name for station in self.stations)
        names.append(self.drain.name)
        return tuple(names)

    def station_for(self, machine_id: str) -> StationSpec | None:
        return self._station_map.get(machine_id)

    def buffer_for(self, machine_id: str) -> BufferSpec | None:
        return self._buffer_map.get(machine_id)

    def source_for(self, job_id: str) -> SourceSpec | None:
        return self._source_map.get(job_id)

    def table(self, name: str) -> TableSpec | None:
        return self._table_map.get(name)

    def to_dict(self) -> dict[str, Any]:
        """Plain-data view (JSON serialisable) for provenance records."""
        return {
            "instance_id": self.instance_id,
            "instance_hash": self.instance_hash,
            "sources": [
                {
                    "name": source.name,
                    "job_id": source.job_id,
                    "release_time": source.release_time,
                    "due_date": source.due_date,
                    "mu_count": source.mu_count,
                }
                for source in self.sources
            ],
            "buffers": [
                {"name": buffer.name, "machine_id": buffer.machine_id, "capacity": buffer.capacity}
                for buffer in self.buffers
            ],
            "stations": [
                {"name": station.name, "machine_id": station.machine_id, "index": station.index, "capacity": station.capacity}
                for station in self.stations
            ],
            "drain": {"name": self.drain.name},
            "connections": [
                {
                    "source": connection.source,
                    "target": connection.target,
                    "job_id": connection.job_id,
                    "step_index": connection.step_index,
                    "requires_routing": connection.requires_routing,
                }
                for connection in self.connections
            ],
            "tables": [{"name": table.name, "columns": list(table.columns)} for table in self.tables],
        }


def build_model_spec(instance: Instance) -> LineModelSpec:
    """Derive the full Plant Simulation object set for `instance`.

    Deterministic: object order follows the instance order (jobs, machines), and
    connections are ordered by (source, target, job, step).
    """
    if not isinstance(instance, Instance):
        raise ValidationError("instance must be a psbridge.schema.Instance", details={"type": type(instance).__name__})

    sources = tuple(
        SourceSpec(
            name=source_name(job.job_id),
            job_id=job.job_id,
            release_time=job.release_time,
            due_date=job.due_date,
            mu_count=1,
        )
        for job in instance.jobs
    )
    buffers = tuple(BufferSpec(name=buffer_name(machine.machine_id), machine_id=machine.machine_id) for machine in instance.machines)
    stations = tuple(
        StationSpec(
            name=station_name(machine.machine_id),
            machine_id=machine.machine_id,
            index=index,
            capacity=machine.capacity,
        )
        for index, machine in enumerate(instance.machines)
    )

    connections: list[ConnectionSpec] = []
    # Source -> buffer of the first operation of that job; collect station
    # transitions in the same pass so targets aren't recomputed.
    successors: dict[str, set[str]] = {}
    station_steps: list[tuple[str, str, str, int]] = []
    for job in instance.jobs:
        first_machine = job.operations[0].candidates[0]
        connections.append(
            ConnectionSpec(source=source_name(job.job_id), target=buffer_name(first_machine), job_id=job.job_id, step_index=0)
        )
        for position, operation in enumerate(job.operations):
            machine_id = operation.candidates[0]
            if position + 1 < len(job.operations):
                target = buffer_name(job.operations[position + 1].candidates[0])
            else:
                target = "Drain_Output"
            successors.setdefault(machine_id, set()).add(target)
            station_steps.append((machine_id, target, job.job_id, position))
    for machine_id, target, job_id, position in station_steps:
        connections.append(
            ConnectionSpec(
                source=station_name(machine_id),
                target=target,
                job_id=job_id,
                step_index=position,
                requires_routing=len(successors[machine_id]) > 1,
            )
        )
    unique_connections = sorted(
        {
            (connection.source, connection.target, connection.job_id, connection.step_index, connection.requires_routing)
            for connection in connections
        },
        key=lambda item: (item[0], item[1], "" if item[2] is None else item[2], -1 if item[3] is None else item[3]),
    )
    connections = [
        ConnectionSpec(source=source, target=target, job_id=job_id, step_index=step, requires_routing=routing)
        for source, target, job_id, step, routing in unique_connections
    ]

    tables = (
        TableSpec(ROUTE_TABLE, ROUTE_COLUMNS),
        TableSpec(JOB_TABLE, JOB_COLUMNS),
        TableSpec(MACHINE_TABLE, MACHINE_COLUMNS),
        TableSpec(LINK_TABLE, LINK_COLUMNS),
        TableSpec(READY_TABLE, READY_COLUMNS),
        TableSpec(RESULT_TABLE, RESULT_COLUMNS),
    )
    return LineModelSpec(
        instance_id=instance.instance_id,
        instance_hash=instance.content_hash(),
        sources=sources,
        buffers=buffers,
        stations=stations,
        drain=DrainSpec(),
        connections=tuple(connections),
        tables=tables,
    )


def summarize_model_spec(spec: LineModelSpec) -> dict[str, Any]:
    """Small JSON-friendly summary (counts + routing links) for reports."""
    if not isinstance(spec, LineModelSpec):
        raise ValidationError("spec must be a LineModelSpec", details={"type": type(spec).__name__})
    routing = [connection for connection in spec.connections if connection.requires_routing]
    return {
        "instance_id": spec.instance_id,
        "instance_hash": spec.instance_hash,
        "sources": len(spec.sources),
        "buffers": len(spec.buffers),
        "stations": len(spec.stations),
        "connections": len(spec.connections),
        "routing_connections": len(routing),
        "tables": [table.name for table in spec.tables],
        "object_names": list(spec.object_names()),
    }
