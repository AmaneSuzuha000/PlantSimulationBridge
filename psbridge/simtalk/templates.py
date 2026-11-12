"""Pure SimTalk source generators verified on Siemens Plant Simulation 13.

Every generator builds deterministic SimTalk from validated identifiers,
dotted object paths, enumerated dispatch policies, and finite numbers. No
instance-supplied string can reach the generated SimTalk unvalidated.

Conventions verified against Plant Simulation 13
------------------------------------------------
* ``<class>.createObject(frame, x, y)`` creates objects inside ``frame``; newly
  created info-flow objects (``&TableFile``, ``&Method``) are named immediately
  and resolved via ``str_to_obj("<frame>.<name>")``.
* ``TableFile`` uses ``ColumnIndex := true`` with ``[column, row]`` indexing:
  column headers sit at row ``0`` and data rows occupy ``1 .. yDim``.
* Rows are trimmed via ``cutRow`` / ``cutColumn`` (``TableFile`` does not
  expose ``setDimensions`` on this build).
* Asynchronous dispatch hooks in ``OnSourceExit`` and ``OnStationExit`` call
  ``wait 0;`` before invoking ``Dispatch`` via ``executeNewCallChain()`` so all
  same-timestamp releases and completions settle into buffers and ``Table_Ready``
  before dispatching begins.
"""

from __future__ import annotations

import re

from psbridge.errors import ValidationError
from psbridge.scheduling.policies import POLICIES, available_policies
from psbridge.schema import Instance, validate_identifier, validate_number
from psbridge.simtalk.model_spec import (
    BUFFER_CLASS,
    DRAIN_CLASS,
    JOB_COLUMNS,
    JOB_TABLE,
    LINK_COLUMNS,
    LINK_TABLE,
    MACHINE_COLUMNS,
    MACHINE_TABLE,
    READY_COLUMNS,
    READY_TABLE,
    RESULT_COLUMNS,
    RESULT_TABLE,
    ROUTE_COLUMNS,
    ROUTE_TABLE,
    SOURCE_CLASS,
    LineModelSpec,
    build_model_spec,
    station_class_for,
)

__all__ = [
    "DEFAULT_FRAME_PATH",
    "METHOD_NAMES",
    "MU_CLASS",
    "NO_DUE_SENTINEL",
    "SIMTALK_CONVENTIONS",
    "build_model_source",
    "control_method_sources",
    "dispatch_method_source",
    "initialize_tables_source",
    "method_sources",
    "read_results_source",
    "validate_policy_name",
    "validate_simtalk_path",
]

#: Default frame path used when callers omit ``frame_path``.
DEFAULT_FRAME_PATH = ".Models.Model"

#: Custom MU subclass carrying scheduling attributes (JobID, Step, OpDuration).
MU_CLASS = ".MUs.PsEntity"

#: Named SimTalk Method objects created inside the target frame.
METHOD_NAMES: tuple[str, ...] = ("Dispatch", "OnSourceExit", "OnStationEnter", "OnStationExit")

_PATH_RE = re.compile(r"^\.?[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_MAX_PATH_LENGTH = 128

#: Filler written into Table_Jobs.DueDate / Table_Routes.DueDate when a job has
#: no due date. Comparisons always check HasDueDate first.
NO_DUE_SENTINEL = 1.0e12

#: Verified Plant Simulation 13 SimTalk conventions baked into these templates.
SIMTALK_CONVENTIONS: tuple[str, ...] = (
    "C1: Object creation uses <class>.createObject(frame, x, y) with leading-dot class paths.",
    "C2: TableFile uses ColumnIndex := true and [col, row] 1-based data indexing (row 0 = header).",
    "C3: Table resizing grows via cell writes and shrinks via cutRow/cutColumn (no setDimensions; xDim/yDim are read-only).",
    "C4: Buffer.Capacity := -1 provides unbounded waiting capacity without blocking stations.",
    "C5: Each Source uses a dedicated Delivery Table (DT_<job>) to inject one .MUs.PsEntity at exact ReleaseTime.",
    "C6: Dispatch assigns stn.ProcTime := bestDuration before chosenMu.move(stn) (verified for SingleProc & ParallelProc).",
    "C7: Station assignment checks stn.NumMU < stn.Capacity and moves MUs explicitly from Buf_<m>.",
    "C8: OnSourceExit and OnStationExit synchronize via wait 0 before str_to_obj Dispatch.executeNewCallChain().",
)

_TABLE_TYPES: dict[str, dict[str, str]] = {
    JOB_TABLE: {
        "JobID": "string",
        "ReleaseTime": "real",
        "DueDate": "real",
        "HasDueDate": "boolean",
        "Weight": "real",
        "OperationCount": "integer",
    },
    ROUTE_TABLE: {
        "JobID": "string",
        "Step": "integer",
        "OperationID": "string",
        "MachineID": "string",
        "Duration": "real",
        "ReleaseTime": "real",
        "DueDate": "real",
        "HasDueDate": "boolean",
    },
    MACHINE_TABLE: {
        "MachineID": "string",
        "Capacity": "integer",
        "BufferName": "string",
        "StationName": "string",
    },
    LINK_TABLE: {
        "SourceName": "string",
        "TargetName": "string",
        "JobID": "string",
        "Step": "integer",
        "RequiresRouting": "boolean",
    },
    READY_TABLE: {
        "OpID": "string",
        "JobID": "string",
        "Step": "integer",
        "MachineID": "string",
        "Duration": "real",
        "ReadySince": "real",
        "JobRelease": "real",
        "DueDate": "real",
        "HasDueDate": "boolean",
        "Weight": "real",
    },
    RESULT_TABLE: {
        "OpID": "string",
        "JobID": "string",
        "MachineID": "string",
        "Start": "real",
        "End": "real",
        "Duration": "real",
    },
}


def validate_simtalk_path(path: str, *, field_name: str = "frame_path") -> str:
    """Validate a dotted SimTalk object path (no quoting or escaping needed)."""
    if not isinstance(path, str) or not path:
        raise ValidationError(field_name + " must be a non-empty string", details={"field": field_name})
    if len(path) > _MAX_PATH_LENGTH:
        raise ValidationError(field_name + " is too long", details={"field": field_name, "length": len(path)})
    if not _PATH_RE.match(path):
        raise ValidationError(
            field_name + " must be a dotted SimTalk path of identifiers",
            details={"field": field_name, "value": path},
        )
    for segment in path.lstrip(".").split("."):
        validate_identifier(segment, field_name=field_name)
    return path


def validate_policy_name(policy_name: str) -> str:
    """Validate a dispatch policy name against the registry."""
    if not isinstance(policy_name, str):
        raise ValidationError("policy name must be a string", details={"policy": repr(policy_name)})
    if policy_name not in POLICIES:
        raise ValidationError(
            "unknown dispatch policy",
            details={"policy": policy_name, "available": list(available_policies())},
        )
    return policy_name


def _num(value: float) -> str:
    """Format a validated finite number as a SimTalk numeric literal."""
    number = validate_number(value, field_name="number")
    if number == int(number):
        if abs(number) < 2_000_000_000:
            return str(int(number))
        if abs(number) < 1e15:
            return str(int(number)) + ".0"
    text = format(number, ".10g")
    if "e" in text or "E" in text:
        text = format(number, ".10f").rstrip("0").rstrip(".")
    if not text or text == "-0":
        text = "0"
    return text


def _q(value: str) -> str:
    """Quote an already-validated identifier as a SimTalk string literal."""
    validate_identifier(value, field_name="identifier")
    return '"' + value + '"'


def _q_literal(value: str) -> str:
    """Quote a fixed non-identifier ASCII token generated by this module."""
    return '"' + value + '"'


def _col(columns: tuple[str, ...], name: str) -> int:
    try:
        return columns.index(name) + 1
    except ValueError:  # pragma: no cover - programming error
        raise ValidationError("unknown table column", details={"column": name}) from None


def _header(title: str, *, detail_lines: tuple[str, ...] = ()) -> list[str]:
    """Build a clean metadata header comment block."""
    lines = [
        "-- Generated by psbridge.simtalk.templates (Plant Simulation 13 verified)",
        "-- " + title,
        "--",
    ]
    for line in detail_lines:
        lines.append("-- " + line)
    lines.append("--")
    return lines


def _instance_lines(instance: Instance) -> tuple[str, ...]:
    return (
        "instance_id: " + instance.instance_id,
        "instance_hash: " + instance.content_hash(),
        "operations: " + str(instance.total_operations),
        "jobs: " + str(len(instance.jobs)),
        "machines: " + str(len(instance.machines)),
    )


# -- 1. model structure ------------------------------------------------------
def build_model_source(spec: LineModelSpec, *, frame_path: str = DEFAULT_FRAME_PATH) -> str:
    """Generate SimTalk that creates the line objects, tables, methods, and controls.

    Creation is create-only by construction: existing objects are never deleted.
    No physical connectors auto-push MUs out of machine input buffers because all
    dispatch moves happen explicitly inside ``Dispatch`` / exit controls.
    """
    if not isinstance(spec, LineModelSpec):
        raise ValidationError("spec must be a LineModelSpec", details={"type": type(spec).__name__})
    frame_path = validate_simtalk_path(frame_path)
    routed = [connection for connection in spec.connections if connection.requires_routing]
    lines = _header(
        "build_model_source: create MU subclass, line objects, DataTables, Methods, and controls",
        detail_lines=(
            "instance_id: " + spec.instance_id,
            "instance_hash: " + spec.instance_hash,
            "objects: " + str(len(spec.sources)) + " sources, " + str(len(spec.buffers)) + " buffers, "
            + str(len(spec.stations)) + " stations, 1 drain, " + str(len(spec.tables) + len(spec.sources)) + " tables, "
            + str(len(METHOD_NAMES)) + " methods",
            "connections: " + str(len(spec.connections)) + " link rows (" + str(len(routed)) + " multi-successor routes)",
        ),
    )
    lines.append("")
    lines.append("-- All var declarations precede statements (SimTalk ExecuteSimTalk rule).")
    lines.append("var obj : object;")
    lines.append("")
    lines.append("-- Custom MU class with job/step/duration tracking attributes")
    lines.append("if not existsObject(" + _q_literal(MU_CLASS) + ") then")
    lines.append("    obj := .MUs.Entity.derive();")
    lines.append('    obj.Name := "PsEntity";')
    lines.append('    obj.createAttr("JobID", "string");')
    lines.append('    obj.createAttr("Step", "integer");')
    lines.append('    obj.createAttr("OpDuration", "real");')
    lines.append("end;")
    lines.append("")

    def emit_create(class_path: str, name: str, x: int, y: int, extra: tuple[str, ...] = ()) -> None:
        full = frame_path + "." + name
        lines.append("if not existsObject(" + _q_literal(full) + ") then")
        lines.append("    obj := " + class_path + ".createObject(" + frame_path + ", " + str(x) + ", " + str(y) + ");")
        lines.append("    obj.Name := " + _q(name) + ";")
        for stmt in extra:
            lines.append("    " + stmt)
        lines.append("end;")

    lines.append("-- Sources and Delivery Tables (one per job)")
    for index, source in enumerate(spec.sources):
        emit_create(SOURCE_CLASS, source.name, 80, 100 + index * 90)
        emit_create(
            ".InformationFlow.&TableFile",
            "DT_" + source.job_id,
            20,
            100 + index * 90,
            extra=("obj.ColumnIndex := false;",),
        )
    lines.append("")

    lines.append("-- Buffers (one per machine; -1 = unbounded)")
    for index, buffer in enumerate(spec.buffers):
        cap = -1 if buffer.capacity == 0 else buffer.capacity
        emit_create(BUFFER_CLASS, buffer.name, 260, 100 + index * 90, extra=("obj.Capacity := " + str(cap) + ";",))
    lines.append("")

    lines.append("-- Stations (SingleProc for capacity=1, ParallelProc for capacity>1)")
    for index, station in enumerate(spec.stations):
        extra: list[str] = []
        if station.capacity > 1:
            extra.append("obj.XDim := " + str(station.capacity) + ";")
            extra.append("obj.YDim := 1;")
        emit_create(
            station_class_for(station.capacity),
            station.name,
            440,
            100 + index * 90,
            extra=tuple(extra),
        )
    lines.append("")

    lines.append("-- Drain")
    emit_create(DRAIN_CLASS, spec.drain.name, 640, 120)
    lines.append("")

    lines.append("-- DataTables")
    for index, table in enumerate(spec.tables):
        emit_create(
            ".InformationFlow.&TableFile",
            table.name,
            80 + index * 110,
            460,
            extra=("obj.ColumnIndex := true;",),
        )
    lines.append("")

    lines.append("-- Methods (bodies loaded separately via set_method_source / Method.load)")
    for index, method_name in enumerate(METHOD_NAMES):
        emit_create(".InformationFlow.&Method", method_name, 80 + index * 140, 560)
    lines.append("")

    lines.append("-- Control wiring (uses str_to_obj handles so renamed Methods resolve reliably)")
    for source in spec.sources:
        lines.append(
            frame_path + "." + source.name + ".ExitCtrl := str_to_obj(" + _q_literal(frame_path + ".OnSourceExit") + ");"
        )
    for station in spec.stations:
        lines.append(
            frame_path + "." + station.name + ".EntranceCtrl := str_to_obj(" + _q_literal(frame_path + ".OnStationEnter") + ");"
        )
        lines.append(
            frame_path + "." + station.name + ".ExitCtrl := str_to_obj(" + _q_literal(frame_path + ".OnStationExit") + ");"
        )
    lines.append("")
    lines.append("-- end of generated model source")
    return "\n".join(lines) + "\n"


# -- 2. tables ---------------------------------------------------------------
def _due_pair(due_date: float | None) -> tuple[str, str]:
    """Return (DueDate literal, HasDueDate literal) for a table row."""
    if due_date is None:
        return (_num(NO_DUE_SENTINEL), "false")
    return (_num(due_date), "true")


def initialize_tables_source(instance: Instance, *, frame_path: str = DEFAULT_FRAME_PATH) -> str:
    """Generate SimTalk that formats/populates all DataTables and configures Sources.

    ``Table_Machines`` is sorted by ascending ``MachineID`` to match
    :func:`psbridge.scheduling.reference.reference_schedule` rule S4.
    """
    if not isinstance(instance, Instance):
        raise ValidationError("instance must be a psbridge.schema.Instance", details={"type": type(instance).__name__})
    frame_path = validate_simtalk_path(frame_path)
    spec = build_model_spec(instance)

    job_rows: list[tuple[str, ...]] = []
    route_rows: list[tuple[str, ...]] = []
    for job in instance.jobs:
        due_literal, has_due = _due_pair(job.due_date)
        job_rows.append(
            (
                _q(job.job_id),
                _num(job.release_time),
                due_literal,
                has_due,
                _num(job.weight),
                str(len(job.operations)),
            )
        )
        for operation in job.operations:
            route_rows.append(
                (
                    _q(job.job_id),
                    str(operation.index + 1),
                    _q(operation.op_id),
                    _q(operation.candidates[0]),
                    _num(operation.duration),
                    _num(job.release_time),
                    due_literal,
                    has_due,
                )
            )
    sorted_machines = sorted(instance.machines, key=lambda machine: machine.machine_id)
    machine_rows = [
        (
            _q(machine.machine_id),
            str(machine.capacity),
            _q(spec.buffer_for(machine.machine_id).name if spec.buffer_for(machine.machine_id) else machine.machine_id),
            _q(spec.station_for(machine.machine_id).name if spec.station_for(machine.machine_id) else machine.machine_id),
        )
        for machine in sorted_machines
    ]
    link_rows = [
        (
            _q(connection.source),
            _q(connection.target),
            _q(connection.job_id) if connection.job_id is not None else _q(connection.source),
            "-1" if connection.step_index is None else str(connection.step_index + 1),
            "true" if connection.requires_routing else "false",
        )
        for connection in spec.connections
    ]

    lines = _header("initialize_tables_source: format and fill DataTables + configure Sources", detail_lines=_instance_lines(instance))
    lines.append("")
    lines.append("var tbl : object;")
    lines.append("var src : object;")
    lines.append("")

    def emit_table(table_name: str, columns: tuple[str, ...], rows: list[tuple[str, ...]], notes: tuple[str, ...] = ()) -> None:
        ncols = len(columns)
        nrows = len(rows)
        col_types = _TABLE_TYPES[table_name]
        lines.append("-- " + table_name + " (" + str(nrows) + " data rows, " + str(ncols) + " columns; ColumnIndex=true)")
        for note in notes:
            lines.append("--   " + note)
        lines.append("tbl := str_to_obj(" + _q_literal(frame_path + "." + table_name) + ");")
        lines.append("while tbl.yDim > 0 loop")
        lines.append("    tbl.cutRow(tbl.yDim);")
        lines.append("end;")
        lines.append("while tbl.xDim > " + str(ncols) + " loop")
        lines.append("    tbl.cutColumn(tbl.xDim);")
        lines.append("end;")
        lines.append("tbl.ColumnIndex := true;")
        for col_index, column in enumerate(columns, start=1):
            lines.append("tbl[" + str(col_index) + ", 0] := " + _q(column) + ";")
            lines.append("tbl.setDataType(" + str(col_index) + ", " + _q_literal(col_types[column]) + ");")
        for row_index, row in enumerate(rows, start=1):
            for col_index, cell in enumerate(row, start=1):
                lines.append("tbl[" + str(col_index) + ", " + str(row_index) + "] := " + cell + ";")
        lines.append("")

    emit_table(JOB_TABLE, JOB_COLUMNS, job_rows, notes=("HasDueDate = false means no due date (EDD sorts last).",))
    emit_table(ROUTE_TABLE, ROUTE_COLUMNS, route_rows, notes=("Step is 1-based; Duration is in simulation seconds.",))
    emit_table(MACHINE_TABLE, MACHINE_COLUMNS, machine_rows, notes=("Sorted in ascending MachineID order (rule S4).",))
    emit_table(LINK_TABLE, LINK_COLUMNS, link_rows)
    emit_table(READY_TABLE, READY_COLUMNS, [], notes=("Runtime ready queue populated by exit controls.",))
    emit_table(RESULT_TABLE, RESULT_COLUMNS, [], notes=("Runtime schedule output appended by Dispatch.",))

    lines.append("-- Source release schedule (Delivery Table, one .MUs.PsEntity per job at ReleaseTime)")
    for job in instance.jobs:
        src_path = frame_path + "." + spec.source_for(job.job_id).name
        dt_path = frame_path + ".DT_" + job.job_id
        lines.append("tbl := str_to_obj(" + _q_literal(dt_path) + ");")
        lines.append("while tbl.yDim > 0 loop")
        lines.append("    tbl.cutRow(tbl.yDim);")
        lines.append("end;")
        lines.append("while tbl.xDim > 4 loop")
        lines.append("    tbl.cutColumn(tbl.xDim);")
        lines.append("end;")
        lines.append("tbl.ColumnIndex := false;")
        lines.append('tbl.setDataType(1, "time");')
        lines.append('tbl.setDataType(2, "object");')
        lines.append('tbl.setDataType(3, "integer");')
        lines.append('tbl.setDataType(4, "string");')
        lines.append("tbl[1, 1] := " + _num(job.release_time) + ";")
        lines.append("tbl[2, 1] := str_to_obj(" + _q_literal(MU_CLASS) + ");")
        lines.append("tbl[3, 1] := 1;")
        lines.append("tbl[4, 1] := " + _q(job.job_id) + ";")
        lines.append("src := str_to_obj(" + _q_literal(src_path) + ");")
        lines.append('src.MuSelection := "Constant";')
        lines.append('src.TimeOfGeneration := "Delivery Table";')
        lines.append("src.Path := tbl;")
    lines.append("")
    lines.append("-- end of generated table initialisation")
    return "\n".join(lines) + "\n"


# -- 3. dispatch + control methods ------------------------------------------
_ALL_KEYS: tuple[tuple[str, str], ...] = (
    ("keyDuration", "real"),
    ("keyReady", "real"),
    ("keyRelease", "real"),
    ("keyHasDue", "real"),
    ("keyDue", "real"),
    ("keyJob", "string"),
    ("keyStep", "integer"),
    ("keyOp", "string"),
)

_FIFO_TERMS: tuple[str, ...] = ("keyReady", "keyRelease", "keyHasDue", "keyDue", "keyJob", "keyStep", "keyOp")
_SPT_TERMS: tuple[str, ...] = ("keyDuration",) + _FIFO_TERMS
_EDD_TERMS: tuple[str, ...] = ("keyHasDue", "keyDue", "keyReady", "keyRelease", "keyJob", "keyStep", "keyOp")

_POLICY_TERMS: dict[str, tuple[str, ...]] = {
    "fifo": _FIFO_TERMS,
    "spt": _SPT_TERMS,
    "edd": _EDD_TERMS,
}

_POLICY_PRIMARY: dict[str, str] = {
    "fifo": "ReadySince (ready_since)",
    "spt": "Duration (duration)",
    "edd": "HasDueDate then DueDate (effective_due)",
}


def _emit_cascade(
    lines: list[str],
    terms: tuple[str, ...],
    depth: int,
    indent: str,
) -> None:
    """Emit the lexicographic comparison cascade for one candidate row."""
    term = terms[depth]
    pad = indent + "  " * depth
    lines.append(pad + "if " + term + " < best" + term[3:] + " then")
    lines.append(pad + "  better := true;")
    lines.append(pad + "elseif " + term + " = best" + term[3:] + " then")
    if depth + 1 < len(terms):
        _emit_cascade(lines, terms, depth + 1, indent)
    else:
        lines.append(pad + "  better := false;")
    lines.append(pad + "end;")


def dispatch_method_source(policy_name: str, *, frame_path: str = DEFAULT_FRAME_PATH) -> str:
    """Generate the SimTalk 1.0 ``Dispatch`` Method body for ``policy_name``.

    Suitable for loading via ``set_method_source(frame + ".Dispatch", source)``.
    Walks ``Table_Machines`` in sorted ``MachineID`` order, dispatches ready
    operations while ``stn.NumMU < stn.Capacity``, records start/end in
    ``Table_Results``, removes the row via ``cutRow``, stamps ``@.OpDuration``,
    and moves the matching MU from ``Buf_<m>`` into ``Stn_<m>``.
    """
    policy_name = validate_policy_name(policy_name)
    frame_path = validate_simtalk_path(frame_path)
    terms = _POLICY_TERMS[policy_name]

    col_op = _col(READY_COLUMNS, "OpID")
    col_job = _col(READY_COLUMNS, "JobID")
    col_step = _col(READY_COLUMNS, "Step")
    col_machine = _col(READY_COLUMNS, "MachineID")
    col_duration = _col(READY_COLUMNS, "Duration")
    col_ready = _col(READY_COLUMNS, "ReadySince")
    col_release = _col(READY_COLUMNS, "JobRelease")
    col_due = _col(READY_COLUMNS, "DueDate")
    col_has_due = _col(READY_COLUMNS, "HasDueDate")

    m_col_id = _col(MACHINE_COLUMNS, "MachineID")
    m_col_buf = _col(MACHINE_COLUMNS, "BufferName")
    m_col_stn = _col(MACHINE_COLUMNS, "StationName")

    res_op = _col(RESULT_COLUMNS, "OpID")
    res_job = _col(RESULT_COLUMNS, "JobID")
    res_machine = _col(RESULT_COLUMNS, "MachineID")
    res_start = _col(RESULT_COLUMNS, "Start")
    res_end = _col(RESULT_COLUMNS, "End")
    res_duration = _col(RESULT_COLUMNS, "Duration")

    lines = _header(
        "Dispatch (policy '" + policy_name + "')",
        detail_lines=(
            "frame: " + frame_path,
            "primary key: " + _POLICY_PRIMARY[policy_name],
            "tie-break order: " + ", ".join(terms),
        ),
    )
    lines.extend(
        [
            "is",
            "  tblMach : object;",
            "  tblReady : object;",
            "  tblRes : object;",
            "  mRow : integer;",
            "  i : integer;",
            "  bIdx : integer;",
            "  bestRow : integer;",
            "  resRow : integer;",
            "  better : boolean;",
            "  keepGoing : boolean;",
            "  machId : string;",
            "  bufName : string;",
            "  stnName : string;",
            "  buf : object;",
            "  stn : object;",
            "  mu : object;",
            "  chosenMu : object;",
            "  nowTime : real;",
        ]
    )
    for key_name, key_type in _ALL_KEYS:
        lines.append("  " + key_name + " : " + key_type + ";")
        lines.append("  best" + key_name[3:] + " : " + key_type + ";")
    lines.extend(
        [
            "do",
            "  tblMach := str_to_obj(" + _q_literal(frame_path + "." + MACHINE_TABLE) + ");",
            "  tblReady := str_to_obj(" + _q_literal(frame_path + "." + READY_TABLE) + ");",
            "  tblRes := str_to_obj(" + _q_literal(frame_path + "." + RESULT_TABLE) + ");",
            "  nowTime := root.EventController.SimTime;",
            "",
            "  for mRow := 1 to tblMach.yDim loop",
            "    machId := tblMach[" + str(m_col_id) + ", mRow];",
            "    bufName := tblMach[" + str(m_col_buf) + ", mRow];",
            "    stnName := tblMach[" + str(m_col_stn) + ", mRow];",
            "    buf := str_to_obj(" + _q_literal(frame_path + ".") + " + bufName);",
            "    stn := str_to_obj(" + _q_literal(frame_path + ".") + " + stnName);",
            "    keepGoing := true;",
            "    while keepGoing and stn.NumMU < stn.Capacity loop",
            "      bestRow := 0;",
        ]
    )
    for key_name, key_type in _ALL_KEYS:
        default_lit = '""' if key_type == "string" else "0"
        lines.append("      best" + key_name[3:] + " := " + default_lit + ";")
    lines.extend(
        [
            "      for i := 1 to tblReady.yDim loop",
            "        if tblReady[" + str(col_machine) + ", i] = machId then",
            "          keyOp := tblReady[" + str(col_op) + ", i];",
            "          keyJob := tblReady[" + str(col_job) + ", i];",
            "          keyStep := tblReady[" + str(col_step) + ", i];",
            "          keyDuration := tblReady[" + str(col_duration) + ", i];",
            "          keyReady := tblReady[" + str(col_ready) + ", i];",
            "          keyRelease := tblReady[" + str(col_release) + ", i];",
            "          keyDue := tblReady[" + str(col_due) + ", i];",
            "          if tblReady[" + str(col_has_due) + ", i] then",
            "            keyHasDue := 0;",
            "          else",
            "            keyHasDue := 1;",
            "          end;",
            "          if bestRow = 0 then",
            "            better := true;",
            "          else",
            "            better := false;",
        ]
    )
    _emit_cascade(lines, terms, 0, "            ")
    lines.extend(
        [
            "          end;",
            "          if better then",
            "            bestRow := i;",
        ]
    )
    for key_name, _ in _ALL_KEYS:
        lines.append("            best" + key_name[3:] + " := " + key_name + ";")
    lines.extend(
        [
            "          end;",
            "        end;",
            "      next;",
            "",
            "      if bestRow = 0 then",
            "        keepGoing := false;",
            "      else",
            "        resRow := tblRes.yDim + 1;",
            "        tblRes[" + str(res_op) + ", resRow] := bestOp;",
            "        tblRes[" + str(res_job) + ", resRow] := bestJob;",
            "        tblRes[" + str(res_machine) + ", resRow] := machId;",
            "        tblRes[" + str(res_start) + ", resRow] := nowTime;",
            "        tblRes[" + str(res_end) + ", resRow] := nowTime + bestDuration;",
            "        tblRes[" + str(res_duration) + ", resRow] := bestDuration;",
            "        tblReady.cutRow(bestRow);",
            "",
            "        chosenMu := void;",
            "        for bIdx := 1 to buf.NumMU loop",
            "          mu := buf.MU(bIdx);",
            "          if mu.JobID = bestJob and mu.Step = bestStep then",
            "            chosenMu := mu;",
            "          end;",
            "        next;",
            "        if chosenMu /= void then",
            "          chosenMu.OpDuration := bestDuration;",
            "          stn.ProcTime := bestDuration;",
            "          chosenMu.move(stn);",
            "        else",
            "          keepGoing := false;",
            "        end;",
            "      end;",
            "    end;",
            "  next;",
            "end;",
        ]
    )
    return "\n".join(lines) + "\n"


def _on_source_exit_source(frame_path: str) -> str:
    l_src = _col(LINK_COLUMNS, "SourceName")
    l_job = _col(LINK_COLUMNS, "JobID")
    rt_job = _col(ROUTE_COLUMNS, "JobID")
    rt_step = _col(ROUTE_COLUMNS, "Step")
    rt_op = _col(ROUTE_COLUMNS, "OperationID")
    rt_mach = _col(ROUTE_COLUMNS, "MachineID")
    rt_dur = _col(ROUTE_COLUMNS, "Duration")
    rt_rel = _col(ROUTE_COLUMNS, "ReleaseTime")
    rt_due = _col(ROUTE_COLUMNS, "DueDate")
    rt_hdue = _col(ROUTE_COLUMNS, "HasDueDate")
    j_id = _col(JOB_COLUMNS, "JobID")
    j_weight = _col(JOB_COLUMNS, "Weight")
    m_id = _col(MACHINE_COLUMNS, "MachineID")
    m_buf = _col(MACHINE_COLUMNS, "BufferName")
    r_op = _col(READY_COLUMNS, "OpID")
    r_job = _col(READY_COLUMNS, "JobID")
    r_step = _col(READY_COLUMNS, "Step")
    r_mach = _col(READY_COLUMNS, "MachineID")
    r_dur = _col(READY_COLUMNS, "Duration")
    r_ready = _col(READY_COLUMNS, "ReadySince")
    r_rel = _col(READY_COLUMNS, "JobRelease")
    r_due = _col(READY_COLUMNS, "DueDate")
    r_hdue = _col(READY_COLUMNS, "HasDueDate")
    r_wt = _col(READY_COLUMNS, "Weight")

    lines = _header("OnSourceExit control method", detail_lines=("frame: " + frame_path,))
    lines.extend(
        [
            ": boolean",
            "is",
            "  tblLinks : object;",
            "  tblRoutes : object;",
            "  tblJobs : object;",
            "  tblMach : object;",
            "  tblReady : object;",
            "  i : integer;",
            "  rRow : integer;",
            "  jobId : string;",
            "  opId : string;",
            "  machId : string;",
            "  dur : real;",
            "  rel : real;",
            "  due : real;",
            "  hasDue : boolean;",
            "  jobWeight : real;",
            "  bufName : string;",
            "  nowTime : real;",
            "do",
            "  tblLinks := str_to_obj(" + _q_literal(frame_path + "." + LINK_TABLE) + ");",
            "  tblRoutes := str_to_obj(" + _q_literal(frame_path + "." + ROUTE_TABLE) + ");",
            "  tblJobs := str_to_obj(" + _q_literal(frame_path + "." + JOB_TABLE) + ");",
            "  tblMach := str_to_obj(" + _q_literal(frame_path + "." + MACHINE_TABLE) + ");",
            "  tblReady := str_to_obj(" + _q_literal(frame_path + "." + READY_TABLE) + ");",
            "  nowTime := root.EventController.SimTime;",
            "",
            '  jobId := "";',
            "  for i := 1 to tblLinks.yDim loop",
            "    if tblLinks[" + str(l_src) + ", i] = ?.Name then",
            "      jobId := tblLinks[" + str(l_job) + ", i];",
            "    end;",
            "  next;",
            "",
            '  opId := "";',
            '  machId := "";',
            "  dur := 0;",
            "  rel := 0;",
            "  due := 0;",
            "  hasDue := false;",
            "  for i := 1 to tblRoutes.yDim loop",
            "    if tblRoutes[" + str(rt_job) + ", i] = jobId and tblRoutes[" + str(rt_step) + ", i] = 1 then",
            "      opId := tblRoutes[" + str(rt_op) + ", i];",
            "      machId := tblRoutes[" + str(rt_mach) + ", i];",
            "      dur := tblRoutes[" + str(rt_dur) + ", i];",
            "      rel := tblRoutes[" + str(rt_rel) + ", i];",
            "      due := tblRoutes[" + str(rt_due) + ", i];",
            "      hasDue := tblRoutes[" + str(rt_hdue) + ", i];",
            "    end;",
            "  next;",
            "",
            "  jobWeight := 1;",
            "  for i := 1 to tblJobs.yDim loop",
            "    if tblJobs[" + str(j_id) + ", i] = jobId then",
            "      jobWeight := tblJobs[" + str(j_weight) + ", i];",
            "    end;",
            "  next;",
            "",
            '  bufName := "";',
            "  for i := 1 to tblMach.yDim loop",
            "    if tblMach[" + str(m_id) + ", i] = machId then",
            "      bufName := tblMach[" + str(m_buf) + ", i];",
            "    end;",
            "  next;",
            "",
            "  @.JobID := jobId;",
            "  @.Step := 1;",
            "  @.OpDuration := dur;",
            "",
            "  rRow := tblReady.yDim + 1;",
            "  tblReady[" + str(r_op) + ", rRow] := opId;",
            "  tblReady[" + str(r_job) + ", rRow] := jobId;",
            "  tblReady[" + str(r_step) + ", rRow] := 1;",
            "  tblReady[" + str(r_mach) + ", rRow] := machId;",
            "  tblReady[" + str(r_dur) + ", rRow] := dur;",
            "  tblReady[" + str(r_ready) + ", rRow] := nowTime;",
            "  tblReady[" + str(r_rel) + ", rRow] := rel;",
            "  tblReady[" + str(r_due) + ", rRow] := due;",
            "  tblReady[" + str(r_hdue) + ", rRow] := hasDue;",
            "  tblReady[" + str(r_wt) + ", rRow] := jobWeight;",
            "",
            "  @.move(str_to_obj(" + _q_literal(frame_path + ".") + " + bufName));",
            "  wait 0;",
            "  str_to_obj(" + _q_literal(frame_path + ".Dispatch") + ").executeNewCallChain();",
            "  return true;",
            "end;",
        ]
    )
    return "\n".join(lines) + "\n"


def _on_station_enter_source(frame_path: str) -> str:
    lines = _header("OnStationEnter control method", detail_lines=("frame: " + frame_path,))
    lines.extend(
        [
            ": boolean",
            "is",
            "do",
            "  ?.ProcTime := @.OpDuration;",
            "  return true;",
            "end;",
        ]
    )
    return "\n".join(lines) + "\n"


def _on_station_exit_source(frame_path: str) -> str:
    rt_job = _col(ROUTE_COLUMNS, "JobID")
    rt_step = _col(ROUTE_COLUMNS, "Step")
    rt_op = _col(ROUTE_COLUMNS, "OperationID")
    rt_mach = _col(ROUTE_COLUMNS, "MachineID")
    rt_dur = _col(ROUTE_COLUMNS, "Duration")
    rt_rel = _col(ROUTE_COLUMNS, "ReleaseTime")
    rt_due = _col(ROUTE_COLUMNS, "DueDate")
    rt_hdue = _col(ROUTE_COLUMNS, "HasDueDate")
    j_id = _col(JOB_COLUMNS, "JobID")
    j_weight = _col(JOB_COLUMNS, "Weight")
    m_id = _col(MACHINE_COLUMNS, "MachineID")
    m_buf = _col(MACHINE_COLUMNS, "BufferName")
    r_op = _col(READY_COLUMNS, "OpID")
    r_job = _col(READY_COLUMNS, "JobID")
    r_step = _col(READY_COLUMNS, "Step")
    r_mach = _col(READY_COLUMNS, "MachineID")
    r_dur = _col(READY_COLUMNS, "Duration")
    r_ready = _col(READY_COLUMNS, "ReadySince")
    r_rel = _col(READY_COLUMNS, "JobRelease")
    r_due = _col(READY_COLUMNS, "DueDate")
    r_hdue = _col(READY_COLUMNS, "HasDueDate")
    r_wt = _col(READY_COLUMNS, "Weight")

    lines = _header("OnStationExit control method", detail_lines=("frame: " + frame_path,))
    lines.extend(
        [
            ": boolean",
            "is",
            "  tblRoutes : object;",
            "  tblJobs : object;",
            "  tblMach : object;",
            "  tblReady : object;",
            "  i : integer;",
            "  rRow : integer;",
            "  jobId : string;",
            "  nextStep : integer;",
            "  foundNext : boolean;",
            "  opId : string;",
            "  machId : string;",
            "  dur : real;",
            "  rel : real;",
            "  due : real;",
            "  hasDue : boolean;",
            "  jobWeight : real;",
            "  bufName : string;",
            "  nowTime : real;",
            "do",
            "  tblRoutes := str_to_obj(" + _q_literal(frame_path + "." + ROUTE_TABLE) + ");",
            "  tblJobs := str_to_obj(" + _q_literal(frame_path + "." + JOB_TABLE) + ");",
            "  tblMach := str_to_obj(" + _q_literal(frame_path + "." + MACHINE_TABLE) + ");",
            "  tblReady := str_to_obj(" + _q_literal(frame_path + "." + READY_TABLE) + ");",
            "  nowTime := root.EventController.SimTime;",
            "",
            "  jobId := @.JobID;",
            "  nextStep := @.Step + 1;",
            "  foundNext := false;",
            '  opId := "";',
            '  machId := "";',
            "  dur := 0;",
            "  rel := 0;",
            "  due := 0;",
            "  hasDue := false;",
            "  for i := 1 to tblRoutes.yDim loop",
            "    if tblRoutes[" + str(rt_job) + ", i] = jobId and tblRoutes[" + str(rt_step) + ", i] = nextStep then",
            "      foundNext := true;",
            "      opId := tblRoutes[" + str(rt_op) + ", i];",
            "      machId := tblRoutes[" + str(rt_mach) + ", i];",
            "      dur := tblRoutes[" + str(rt_dur) + ", i];",
            "      rel := tblRoutes[" + str(rt_rel) + ", i];",
            "      due := tblRoutes[" + str(rt_due) + ", i];",
            "      hasDue := tblRoutes[" + str(rt_hdue) + ", i];",
            "    end;",
            "  next;",
            "",
            "  if foundNext then",
            "    jobWeight := 1;",
            "    for i := 1 to tblJobs.yDim loop",
            "      if tblJobs[" + str(j_id) + ", i] = jobId then",
            "        jobWeight := tblJobs[" + str(j_weight) + ", i];",
            "      end;",
            "    next;",
            '    bufName := "";',
            "    for i := 1 to tblMach.yDim loop",
            "      if tblMach[" + str(m_id) + ", i] = machId then",
            "        bufName := tblMach[" + str(m_buf) + ", i];",
            "      end;",
            "    next;",
            "    @.Step := nextStep;",
            "    @.OpDuration := dur;",
            "    rRow := tblReady.yDim + 1;",
            "    tblReady[" + str(r_op) + ", rRow] := opId;",
            "    tblReady[" + str(r_job) + ", rRow] := jobId;",
            "    tblReady[" + str(r_step) + ", rRow] := nextStep;",
            "    tblReady[" + str(r_mach) + ", rRow] := machId;",
            "    tblReady[" + str(r_dur) + ", rRow] := dur;",
            "    tblReady[" + str(r_ready) + ", rRow] := nowTime;",
            "    tblReady[" + str(r_rel) + ", rRow] := rel;",
            "    tblReady[" + str(r_due) + ", rRow] := due;",
            "    tblReady[" + str(r_hdue) + ", rRow] := hasDue;",
            "    tblReady[" + str(r_wt) + ", rRow] := jobWeight;",
            "    @.move(str_to_obj(" + _q_literal(frame_path + ".") + " + bufName));",
            "  else",
            "    @.move(str_to_obj(" + _q_literal(frame_path + ".Drain_Output") + "));",
            "  end;",
            "  wait 0;",
            "  str_to_obj(" + _q_literal(frame_path + ".Dispatch") + ").executeNewCallChain();",
            "  return true;",
            "end;",
        ]
    )
    return "\n".join(lines) + "\n"


def control_method_sources(*, frame_path: str = DEFAULT_FRAME_PATH) -> dict[str, str]:
    """Return SimTalk 1.0 sources for ``OnSourceExit``, ``OnStationEnter``, and ``OnStationExit``."""
    frame_path = validate_simtalk_path(frame_path)
    return {
        "OnSourceExit": _on_source_exit_source(frame_path),
        "OnStationEnter": _on_station_enter_source(frame_path),
        "OnStationExit": _on_station_exit_source(frame_path),
    }


def method_sources(policy_name: str, *, frame_path: str = DEFAULT_FRAME_PATH) -> dict[str, str]:
    """Return all four Method sources keyed by frame method object name."""
    policy_name = validate_policy_name(policy_name)
    frame_path = validate_simtalk_path(frame_path)
    sources = {"Dispatch": dispatch_method_source(policy_name, frame_path=frame_path)}
    sources.update(control_method_sources(frame_path=frame_path))
    return sources


# -- 4. results --------------------------------------------------------------
def read_results_source(*, frame_path: str = DEFAULT_FRAME_PATH) -> str:
    """Generate SimTalk that reads ``Table_Results`` and prints fixed summary lines."""
    frame_path = validate_simtalk_path(frame_path)
    result_path = frame_path + "." + RESULT_TABLE
    col_op = _col(RESULT_COLUMNS, "OpID")
    col_job = _col(RESULT_COLUMNS, "JobID")
    col_machine = _col(RESULT_COLUMNS, "MachineID")
    col_start = _col(RESULT_COLUMNS, "Start")
    col_end = _col(RESULT_COLUMNS, "End")
    col_duration = _col(RESULT_COLUMNS, "Duration")

    lines = _header(
        "read_results_source: print Table_Results as machine-readable lines",
        detail_lines=(
            "frame: " + frame_path,
            "line format: RESULT <op> <job> <machine> <start> <end> <duration>",
            "summary line: SUMMARY <count> <makespan>",
        ),
    )
    lines.extend(
        [
            "",
            "var tblRes : object;",
            "var i : integer;",
            "var makespan : real;",
            "var rowCount : integer;",
            "",
            "tblRes := str_to_obj(" + _q_literal(result_path) + ");",
            "rowCount := tblRes.yDim;",
            "if rowCount < 1 then",
            '    print "SUMMARY 0 0";',
            "    return;",
            "end;",
            "",
            "makespan := 0;",
            "for i := 1 to rowCount loop",
            '    print "RESULT " + tblRes[' + str(col_op) + ', i] + " " + tblRes[' + str(col_job) + ', i] + " " + tblRes[' + str(col_machine) + ', i] + " " + to_str(tblRes[' + str(col_start) + ', i], 3) + " " + to_str(tblRes[' + str(col_end) + ', i], 3) + " " + to_str(tblRes[' + str(col_duration) + ", i], 3);",
            "    if tblRes[" + str(col_end) + ", i] > makespan then",
            "        makespan := tblRes[" + str(col_end) + ", i];",
            "    end;",
            "next;",
            "",
            'print "SUMMARY " + to_str(rowCount, 0) + " " + to_str(makespan, 3);',
            "",
            "-- end of generated results reader",
        ]
    )
    return "\n".join(lines) + "\n"
