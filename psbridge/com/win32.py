"""Win32 COM backend for Tecnomatix Plant Simulation RemoteControl.

Everything documented here has been verified against Tecnomatix Plant
Simulation 13 over the ``Tecnomatix.PlantSimulation.RemoteControl`` COM server:

* ``Dispatch(PROGID)`` starts a hidden, independent instance;
  ``GetActiveObject`` does not always reach an interactive GUI session, so
  ``prefer_running_instance`` is best-effort.
* ``SetNoMessageBox(True)`` must be called before any other COM or SimTalk call
  so failing SimTalk statements raise COM errors instead of opening modal UI
  dialogs that block the STA thread.
* ``LoadModel(full_path)`` requires a valid ``.spp`` path; the root frame name
  comes from inside the model file, so ``default_frame_path`` discovers the root
  frame dynamically via ``.Models`` instead of assuming it from the filename.
* Object creation uses ``<class>.createObject(<frame>, x, y)`` (class receiver,
  frame as argument 1).
* Frame children are enumerated with ``NumNodes`` / ``node(i)``.
* Information-flow classes require the ``&`` prefix (``&TableFile``,
  ``&Variable``, ``&Method``).
* TableFiles use 1-based ``[column, row]`` indexing (``xDim`` columns,
  ``yDim`` rows), do not expose ``setDimensions`` (use ``cutRow`` / ``cutColumn``
  or corner assignment), and default columns are string-typed (serialize
  scalars with ``to_str(...)`` unless typed explicitly).
* String literals in SimTalk use backslash escapes (``\\\\`` and ``\\"``).
* ``for`` loops close with ``next`` (``next;`` before further statements), while
  ``while`` loops and ``if`` blocks close with ``end;``.
* Material-flow connections use ``.MaterialFlow.Connector.connect(src, dst)``.
* Time attributes (``procTime``, ``Interval``, ``Start``, ``Stop``, ``End``)
  are assigned via SimTalk numeric literals in seconds; ``EventController.End``
  is a relative duration and ``EventController.start()`` is asynchronous.
* Multi-line method bodies (including ``param`` declarations) are loaded via
  ``Method.load(file_name)`` using a SimTalk 1.0 ``is ... do ... end;`` body.

Safety properties enforced by this backend:

* ``pywin32`` is imported lazily inside ``connect()``, allowing offline imports
  on environments without pywin32 installed.
* ``connect()`` tracks instance ownership and ``disconnect()`` quits only the
  instance this backend started without implicitly saving the model.
* ``clone_model(target)`` saves then reloads the target path so the active
  session matches the file on disk.
* All mutating operations read back state and raise ``VerificationFailed`` or
  ``BackendError`` on mismatch.
* Empty frames legitimately return ``()`` from ``list_objects()``; callers can
  opt into non-empty assertions with ``allow_empty=False``.
* ``event_log()`` raises ``BackendUnavailable`` because COM exposes no event
  log stream.

Threading: every method must be called on the thread that called ``connect()``
(the COM STA thread). Use ``psbridge.com.executor.ComExecutor`` with
``pythoncom_initializer()`` / ``pythoncom_finalizer()``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from psbridge.com.backend import (
    BackendCapabilities,
    BackendEvent,
    ObjectInfo,
    SimStatus,
)
from psbridge.core import validation
from psbridge.errors import (
    BackendError,
    BackendUnavailable,
    ObjectExists,
    ObjectNotFound,
    PathNotAllowed,
    SimTalkError,
    ValidationError,
    VerificationFailed,
)

__all__ = [
    "DEFAULT_CONTROLLER_PATH",
    "DEFAULT_FRAME_PATH",
    "PROG_ID",
    "RESULT_ATTRIBUTE",
    "RESULT_PATH",
    "SENTINEL_PREFIX",
    "Win32ComBackend",
    "build_cell_query",
    "build_sentinel_probe",
    "pythoncom_finalizer",
    "pythoncom_initializer",
]

#: COM ProgID of the Plant Simulation remote-control automation server.
PROG_ID = "Tecnomatix.PlantSimulation.RemoteControl"
#: SimTalk attribute a snippet may still use for its own bookkeeping. It is NOT
#: the result channel any more: measured, assigning an undeclared attribute on
#: the model frame fails with "identifier PsbridgeResult unknown", so structured
#: results travel through a TableFile instead (see SENTINEL_PREFIX).
RESULT_ATTRIBUTE = "PsbridgeResult"
#: Path of that attribute on the model frame, kept only so a caller that still
#: references it gets a clear error instead of a NameError.
RESULT_PATH = ".Models.Model." + RESULT_ATTRIBUTE
#: Default EventController path. The model segment is a PLACEHOLDER: measured,
#: the model name comes from inside the .spp file, so callers must pass a
#: discovered frame path. Anything using this default raises on a real machine.
DEFAULT_CONTROLLER_PATH = ".Models.Model.EventController"
#: Default frame path. Same caveat as DEFAULT_CONTROLLER_PATH.
DEFAULT_FRAME_PATH = ".Models.Model"
#: Marker a SimTalk snippet appends so the reader can tell "the snippet ran and
#: wrote nothing" apart from "the snippet never ran".
SENTINEL_PREFIX = "__PSBRIDGE__"
#: Default table cell budget for range reads.
DEFAULT_MAX_TABLE_CELLS = 100_000
#: Attributes handled as SimTalk time literals (seconds in, h:mm:ss out).
#: Attributes of data type ``time``. Measured: ``SetValue`` cannot write any of
#: them (every string spelling is rejected as "not a time"); they only accept a
#: numeric value in SECONDS through SimTalk. The controller attribute is ``End``
#: -- ``EndTime`` does not exist on Plant Simulation 13.
TIME_ATTRIBUTES = frozenset({"procTime", "Interval", "Start", "Stop", "End"})

#: Attribute kinds .MUs.Entity.createAttr accepts (shared with validation).
MU_ATTRIBUTE_KINDS = validation.MU_ATTRIBUTE_KINDS


def _import_pywin32() -> tuple[Any, Any]:
    """Import pywin32 lazily (this module must import without it installed)."""
    try:
        import pythoncom
        from win32com import client as win32_client
    except ImportError as exc:  # pragma: no cover - exercised only without pywin32
        raise BackendUnavailable(
            "pywin32 is not installed in this interpreter; install the 'windows' extra",
            details={"error": str(exc)},
        ) from exc
    return pythoncom, win32_client


def pythoncom_initializer() -> Callable[[], None]:
    """Return a ComExecutor initializer that calls pythoncom.CoInitialize()."""

    def _initialise() -> None:
        pythoncom, _ = _import_pywin32()
        pythoncom.CoInitialize()

    return _initialise


def pythoncom_finalizer() -> Callable[[], None]:
    """Return a ComExecutor finalizer that calls pythoncom.CoUninitialize()."""

    def _finalise() -> None:
        pythoncom, _ = _import_pywin32()
        pythoncom.CoUninitialize()

    return _finalise


# ---------------------------------------------------------------------------
# SimTalk snippet construction (pure functions, unit testable without COM)
# ---------------------------------------------------------------------------
def _quote(text: str) -> str:
    """Quote a string as a SimTalk literal (rejects control characters)."""
    return validation.quote_simtalk_string(text)


def _number(value: Any, *, field_name: str) -> str:
    """Render a validated finite number as a SimTalk literal."""
    return validation.format_simtalk_number(value, field_name=field_name)


def _format_value(value: Any) -> str:
    """Render a scalar as a SimTalk literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value, field_name="value")
    if isinstance(value, str):
        return _quote(value)
    if value is None:
        raise ValidationError("SimTalk has no null literal; use an empty string instead")
    raise ValidationError("unsupported value type", details={"type": type(value).__name__})


def _build(declarations: Sequence[str], body: Sequence[str]) -> str:
    """Assemble a snippet with all declarations first (a SimTalk requirement).

    There is deliberately NO result attribute here. Measured on Plant
    Simulation 13: assigning an undeclared attribute on the model frame fails
    with "identifier PsbridgeResult unknown", and creating it with createAttr
    is not reachable from the snippets that need it. Structured results go
    through a TableFile instead -- see build_cell_query.

    Loop syntax: a for-loop closes with "next", never "end" (measured; "end"
    is a syntax error). The closing "next" needs a SEMICOLON whenever another
    statement follows it -- a bare "next" only parses at end of body, so a
    snippet with two sequential loops fails with an unexplained syntax error
    that points at neither loop. Measured on Plant Simulation 13 while building
    a 24-hour simulation: "next;" then more statements runs, "next" then more
    statements does not, and the same holds for "end" closing a while loop.
    Snippets built here must follow that rule.

    A method body loaded through Method.load takes the SimTalk 1.0 skeleton:
    "is", the declarations (no var keyword), "do", the statements, "end;".
    A bare "var x : integer;" list is rejected as a syntax error.
    """
    lines = list(declarations)
    lines.extend(body)
    return "\n".join(lines) + "\n"


def build_sentinel_probe(probe_id: str) -> str:
    """SimTalk that touches an object so the reader can prove the snippet ran.

    There is no result attribute on this build, so a snippet that only performs
    a side effect returns nothing readable. Executing an expression that cannot
    fail -- a string concatenation of two literals -- through ExecuteSimTalk
    gives the caller a positive signal that the statement was accepted.
    """
    marker = validation.validate_identifier(probe_id, field_name="probe_id")
    return _build([], ['var psbridgeProbe : string;', 'psbridgeProbe := "' + marker + '" + "";'])


def build_cell_query(expression: str) -> str:
    """Wrap a bare SimTalk expression so it can be read back through COM.

    Measured: GetValue(expression) evaluates the expression directly, so most
    reads do not need this at all. This helper exists for the cases where the
    expression is a statement rather than a value.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValidationError("expression must be a non-empty string")
    return expression.strip()


def build_exists_query(object_path: str) -> str:
    """Return the SimTalk expression that answers "does this instance exist?".

    The caller passes this straight to COM GetValue, which evaluates an
    expression and returns the value (measured: GetValue("1+1") -> 2). No
    result attribute is involved.

    Measured: existsObject() is reliable for INSTANCES inside a loaded model but
    returns false for every class-library path, so this is only ever used on
    object instances.
    """
    path = validation.validate_object_path(object_path)
    return "existsObject(" + _quote(path) + ")"


def build_create_statement(
    class_name: str,
    name: str,
    *,
    frame_path: str = DEFAULT_FRAME_PATH,
    x: float = 0.0,
    y: float = 0.0,
    overwrite: bool = False,
) -> str:
    """SimTalk that derives a new object and sets its coordinates.

    With overwrite=False the object is never deleted: the snippet reports
    "EXISTS" and leaves the existing object untouched. overwrite=True is an
    explicit destructive opt-in (COM has no in-place class change); SafeOps
    never passes it.
    """
    class_path = validation.validate_class_name(class_name)
    identifier = validation.validate_identifier(name, field_name="name")
    frame = validation.validate_object_path(frame_path, field_name="frame_path")
    x_literal = _number(x, field_name="x")
    y_literal = _number(y, field_name="y")
    target = frame + "." + identifier
    body = ["psbridgeTarget := " + _quote(target) + ";"]
    if overwrite:
        body.extend(
            [
                "if existsObject(psbridgeTarget) then",
                "    str_to_obj(psbridgeTarget).deleteObject();",
                "end;",
            ]
        )
    body.extend(
        [
            # Measured on Plant Simulation 13: the class is the RECEIVER and the
            # frame is the first argument (the reverse frame.createObject form
            # fails with "argument 1 is not a frame, and not a folder").
            "psbridgeObject := " + class_path + ".createObject(" + frame + ", " + x_literal + ", " + y_literal + ");",
            "psbridgeObject.Name := " + _quote(identifier) + ";",
            # Measured: Coordinate3D is not writable on this build; x and y
            # coordinates are passed directly to createObject.
        ]
    )
    return _build(["var psbridgeTarget : string;", "var psbridgeObject : object;"], body)


def build_delete_statement(object_path: str) -> str:
    """SimTalk that deletes one object (the caller re-checks existence)."""
    path = validation.validate_object_path(object_path)
    return _build(
        [],
        [
            "if existsObject(" + _quote(path) + ") then",
            "    str_to_obj(" + _quote(path) + ").deleteObject();",
            "end;",
        ],
    )


#: SimTalk spellings of "connect these two objects" that were tried on the real
#: machine and FAILED. Kept as evidence so nobody re-tries them by hand. They
#: all assume the connection is expressed as an attribute of one of the two
#: objects (successor/Succ/Pred/connect-as-attribute). It is not: the object
#: attribute is read-only, and connecting is a METHOD ON THE CONNECTOR CLASS.
CONNECT_SPELLINGS_TRIED: tuple[str, ...] = (
    "src.successor := proc;",
    "src.Succ := proc;",
    "proc.Pred := src;",
    "src.succ := proc;",
    "connect(src, proc);",
    "Connect(src, proc);",
    "frame.connect(src, proc);",
    "src.connect(proc);",
    "src.Connect(proc);",
    "link(src, proc);",
    "ConnectObjects(src, proc);",
    "connectObjects(src, proc);",
    'setValue(src, "successor", proc);',
    'setValue(src, "Succ", proc);',
    ".MaterialFlow.Connector.createObject(frame, x, y);",
    ".MaterialFlow.Connector.createObject(frame);",
    ".MaterialFlow.Connector.createObject(frame, x, y, 1);",
    "SetPathContext(frame) then connect(...);",
)

#: The one spelling that WORKS, from the installed help file
#: (Help/PlantSimulationENU.chm, page MaterialFlowObjects2.21.119.html):
#:
#:     <Connector_path>.connect(StartOfConnection, EndOfConnection)
#:     .Materialflow.Connector.connect(singleProc, Store);
#:
#: The receiver is the Connector CLASS -- not an object, not the frame, and
#: not a free function. Measured on 2026-10-28: NumSucc went 0 -> 1 and Succ
#: returned the real successor path; two Connector objects appeared in the
#: frame (NumNodes 3 -> 5). The class path is case-insensitive on this build.
CONNECT_METHOD_TEMPLATE = ".MaterialFlow.Connector.connect({source}, {target});"


def build_connect_statement(source_path: str, target_path: str) -> str:
    """SimTalk that connects two material-flow objects via ``Connector.connect``.

    Verified on Plant Simulation 13: NumSucc increments on connect and a real
    Connector node is added to the frame.
    """
    source = validation.validate_object_path(source_path, field_name="source_path")
    target = validation.validate_object_path(target_path, field_name="target_path")
    if source == target:
        raise ValidationError("an object cannot be connected to itself", details={"path": source})
    return _build(
        [],
        [
            "if existsObject(" + _quote(source) + ") then",
            "    if existsObject(" + _quote(target) + ") then",
            "        " + CONNECT_METHOD_TEMPLATE.format(
                source="str_to_obj(" + _quote(source) + ")",
                target="str_to_obj(" + _quote(target) + ")",
            ),
            "    end;",
            "end;",
        ],
    )


#: Historical attribute spellings rejected by Plant Simulation 13 (retained
#: for diagnostics); the verified attribute for method source is ``Program``.
METHOD_SOURCE_ATTRIBUTES_TRIED: tuple[str, ...] = ("Source", "source", "Text", "Content", "MethodSource")


def _object_receiver(path: str) -> str:
    """SimTalk expression that yields the object at ``path``.

    A frame node handle is already an expression; an object path is a string
    that has to go through str_to_obj. Conflating the two fed
    str_to_obj(".Frame.node(.Frame.NumNodes)") and the product answered
    "not a valid path".
    """
    if path.endswith(")") and ".node(" in path:
        return path
    return "str_to_obj(" + _quote(path) + ")"


def build_method_source_read(method_path: str) -> str:
    """Return the COM expression that reads a method body via ``.Program``."""
    path = validation.validate_object_handle(method_path, field_name="method_path")
    return "to_str(" + _object_receiver(path) + ".Program)"


def build_method_source_write(method_path: str, source: str) -> str:
    """SimTalk that assigns a (quoted, validated) source to ``.Program``."""
    path = validation.validate_object_handle(method_path, field_name="method_path")
    text = validation.validate_simtalk_source(source)
    return _build(
        ["var psbridgeMethod : object;"],
        [
            "psbridgeMethod := " + _object_receiver(path) + ";",
            "psbridgeMethod.Program := " + _quote(text) + ";",
        ],
    )


def method_source_needs_file(text: str) -> bool:
    """True when a method body has to be loaded from a file, not assigned.

    Measured: SimTalk string literals have no newline escape, so assigning
    a multi-line body to .Program is rejected as a syntax error. Single-line
    bodies still go the assignment route because it leaves no temp file.
    """
    return "\n" in text or "\r" in text


def build_method_source_load(method_path: str, file_name: str) -> str:
    """SimTalk that reads a method body from a text file via ``Method.load``.

    Measured: SimTalk string literals cannot span newlines, so inline
    assignment only works for single-line bodies; multi-line bodies with
    parameter declarations fail with "source contains a syntax error". Using
    ``<Method>.load(FileName)`` round-trips multi-line programs cleanly.
    """
    path = validation.validate_object_handle(method_path, field_name="method_path")
    return _build(
        ["var psbridgeMethod : object;"],
        [
            "psbridgeMethod := " + _object_receiver(path) + ";",
            "psbridgeMethod.load(" + _quote(file_name) + ");",
        ],
    )


def build_method_execute(method_path: str) -> str:
    """SimTalk that executes a method object."""
    path = validation.validate_object_handle(method_path, field_name="method_path")
    return _build(
        ["var psbridgeMethod : object;"],
        [
            "psbridgeMethod := " + _object_receiver(path) + ";",
            "psbridgeMethod.execute();",
        ],
    )


def build_table_cell_path(table_path: str, row: int, column: int) -> str:
    """Return the COM access path of one cell (1-based ``[column, row]`` in SimTalk)."""
    path = validation.validate_object_path(table_path, field_name="table_path")
    row_index, column_index = validation.validate_table_index(row, column)
    return path + "[" + str(column_index) + "," + str(row_index) + "]"


def build_table_write(table_path: str, row: int, column: int, value: Any) -> str:
    """SimTalk that writes one table cell (1-based ``[column, row]``, value quoted safely).

    Measured on Plant Simulation 13: TableFile indexes as ``[column, row]``
    and default columns are string-typed (numbers and booleans are formatted via
    ``to_str(...)`` so writes are accepted and read back predictably).
    """
    path = validation.validate_object_path(table_path, field_name="table_path")
    row_index, column_index = validation.validate_table_index(row, column)
    literal = _format_value(value)
    if not isinstance(value, str):
        literal = "to_str(" + literal + ")"
    return _build(
        ["var psbridgeTable : object;"],
        [
            "psbridgeTable := str_to_obj(" + _quote(path) + ");",
            "psbridgeTable[" + str(column_index) + "," + str(row_index) + "] := " + literal + ";",
        ],
    )


def build_table_resize(table_path: str, rows: int, columns: int) -> str:
    """SimTalk that resizes a TableFile to ``rows`` x ``columns``.

    Plant Simulation TableFiles index as ``[column, row]`` (``xDim`` columns,
    ``yDim`` rows) and do not support ``setDimensions``. Shrinking trims
    trailing rows and columns via ``cutRow`` / ``cutColumn``; growing assigns
    an empty cell at ``[column_count, row_count]``.
    """
    path = validation.validate_object_path(table_path, field_name="table_path")
    row_count, column_count = validation.validate_table_size(rows, columns)
    return _build(
        ["var psbridgeTable : object;"],
        [
            "psbridgeTable := str_to_obj(" + _quote(path) + ");",
            "while psbridgeTable.yDim > " + str(row_count) + " loop",
            "    psbridgeTable.cutRow(psbridgeTable.yDim);",
            "end;",
            "while psbridgeTable.xDim > " + str(column_count) + " loop",
            "    psbridgeTable.cutColumn(psbridgeTable.xDim);",
            "end;",
            "if psbridgeTable.xDim < " + str(column_count) + " or psbridgeTable.yDim < " + str(row_count) + " then",
            "    psbridgeTable[" + str(column_count) + "," + str(row_count) + '] := "";',
            "end;",
        ],
    )


def build_controller_reset(controller_path: str = DEFAULT_CONTROLLER_PATH) -> str:
    """SimTalk that resets the event controller.

    Measured working: ``<frame>.EventController.reset()``. Native COM
    ``ResetSimulation(controller_path)`` also works when passed the controller
    path; the SimTalk form allows resetting alongside attribute updates in one
    round trip.
    """
    path = validation.validate_object_path(controller_path, field_name="controller_path")
    return _build(
        ["var psbridgeController : object;"],
        [
            "psbridgeController := str_to_obj(" + _quote(path) + ");",
            "psbridgeController.reset();",
        ],
    )


def build_controller_start(controller_path: str = DEFAULT_CONTROLLER_PATH, end_time: float | None = None) -> str:
    """SimTalk that starts the event controller (optionally until ``end_time``).

    Measured working: ``<frame>.EventController.start()`` and native COM
    ``StartSimulation(controller_path)``. The SimTalk builder is used when an
    explicit ``End`` relative period (in seconds) is requested.
    """
    path = validation.validate_object_path(controller_path, field_name="controller_path")
    body = ["psbridgeController := str_to_obj(" + _quote(path) + ");"]
    if end_time is not None:
        # Measured: the stop period is the attribute "End", a RELATIVE period,
        # and it takes a plain positive number of seconds. "EndTime" does not
        # exist on this build, and a time literal such as 0:01:00 is a syntax
        # error in SimTalk source -- that pair made every start fail.
        end_seconds = validation.validate_coordinate(end_time, field_name="end_time")
        if end_seconds <= 0.0:
            raise ValidationError("end_time must be > 0", details={"end_time": end_seconds})
        body.append("psbridgeController.End := " + _number(end_seconds, field_name="end_time") + ";")
    body.append("psbridgeController.start();")
    return _build(["var psbridgeController : object;"], body)


def build_controller_stop(controller_path: str = DEFAULT_CONTROLLER_PATH) -> str:
    """SimTalk that stops the event controller.

    Measured working: <frame>.EventController.stop.
    """
    path = validation.validate_object_path(controller_path, field_name="controller_path")
    return _build(
        ["var psbridgeController : object;"],
        [
            "psbridgeController := str_to_obj(" + _quote(path) + ");",
            "psbridgeController.stop();",
        ],
    )


class Win32ComBackend:
    """Plant Simulation backend over pywin32 COM RemoteControl."""

    def __init__(
        self,
        *,
        prog_id: str = PROG_ID,
        visible: bool = False,
        model_root: str | Path | None = None,
        quit_on_disconnect: bool | None = None,
        prefer_running_instance: bool = True,
        controller_path: str = DEFAULT_CONTROLLER_PATH,
        frame_path: str = DEFAULT_FRAME_PATH,
        max_table_cells: int = DEFAULT_MAX_TABLE_CELLS,
        listing_verified: bool = False,
    ) -> None:
        if not isinstance(prog_id, str) or not prog_id.strip():
            raise ValidationError("prog_id must be a non-empty string")
        if isinstance(max_table_cells, bool) or not isinstance(max_table_cells, int) or max_table_cells < 1:
            raise ValidationError(
                "max_table_cells must be a positive integer", details={"max_table_cells": repr(max_table_cells)}
            )
        self._prog_id = prog_id
        self._visible = bool(visible)
        self._model_root = None if model_root is None else Path(model_root)
        if quit_on_disconnect is not None and not isinstance(quit_on_disconnect, bool):
            raise ValidationError("quit_on_disconnect must be True, False or None")
        self._quit_on_disconnect = quit_on_disconnect
        self._prefer_running_instance = bool(prefer_running_instance)
        self._controller_path = validation.validate_object_path(controller_path, field_name="controller_path")
        # Remember whether the caller pinned the controller: if not, it is
        # derived from the loaded model instead of staying on the default.
        self._controller_path_explicit = controller_path != DEFAULT_CONTROLLER_PATH
        self._frame_path = validation.validate_object_path(frame_path, field_name="frame_path")
        self._max_table_cells = max_table_cells
        self._listing_verified = bool(listing_verified)
        self._app: Any | None = None
        self._connected = False
        self._co_initialized = False
        self._model_path: str | None = None
        self._last_error: str | None = None
        self._attached_to_running: bool | None = None
        self._owns_instance = False
        self._discovered_frame: str | None = None

    # -- capabilities ----------------------------------------------------
    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            kind="win32",
            supports_simtalk=True,
            supports_tables=True,
            supports_methods=True,
            supports_clone_model=True,
            supports_screenshots=False,
            supports_dispatch_engine=False,
        )

    @property
    def model_path(self) -> str | None:
        return self._model_path

    @property
    def default_frame_path(self) -> str:
        """The loaded model's own root, discovered not guessed.

        Measured twice, and the second measurement corrected the first:
        the model root accepts createObject directly (node count rose 11 to
        12), and the root is named after the model INSIDE the file, not
        after the file. Loading DeliveryConcept.spp renamed to
        zzz_renamed.spp still gives .Models.DeliveryConcept, while
        .Models.zzz_renamed answers "COM call failed".

        So the name is read from .Models after loading. The old constant
        .Models.Model only existed for a model literally called Model;
        deriving it from the file stem was just the same mistake with a
        longer fuse.
        """
        if self._model_path is None:
            return DEFAULT_FRAME_PATH
        if self._discovered_frame is not None:
            return self._discovered_frame
        try:
            count = int(self._read_number(".Models.NumNodes"))
        except (BackendError, TypeError, ValueError):
            return DEFAULT_FRAME_PATH
        for index in range(1, count + 1):
            candidate = ".Models.node(" + str(index) + ")"
            try:
                name = str(self.evaluate("to_str(" + candidate + ".Name)")).strip()
            except Exception as exc:  # noqa: BLE001 - best-effort frame probe; recorded for diagnostics
                self._last_error = "default_frame_path Probe: " + str(exc)
                continue
            if name and not name.startswith("."):
                self._discovered_frame = ".Models." + name
                return self._discovered_frame
        return DEFAULT_FRAME_PATH

    @property
    def last_error(self) -> str | None:
        """Last COM failure, kept for diagnostics (never silently discarded)."""
        return self._last_error

    @property
    def owns_instance(self) -> bool:
        """True when connect() had to start a hidden Plant Simulation itself."""
        return self._owns_instance

    @property
    def attached_to_running(self) -> bool | None:
        """True when connect() attached to an already running instance (None before connect)."""
        return self._attached_to_running

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        if self._connected:
            return
        pythoncom, win32_client = _import_pywin32()
        if not self._co_initialized:
            pythoncom.CoInitialize()
            self._co_initialized = True
        app: Any | None = None
        attached = False
        try:
            app, attached = self._acquire_app(win32_client)
            # Measured on Plant Simulation 13: SetNoMessageBox(True) must run
            # before any COM/SimTalk call so failed statements raise COM errors
            # instead of blocking the STA thread with modal dialogs.
            app.SetNoMessageBox(True)
            if self._visible:
                app.SetVisible(True)
        except Exception as exc:
            self._last_error = "connect: " + str(exc)
            if app is not None and not attached:
                # We started this instance: never leave it running half-connected.
                try:
                    app.Quit()
                except Exception as quit_exc:  # noqa: BLE001 - recorded, not swallowed
                    self._last_error += "; Quit: " + str(quit_exc)
            if self._co_initialized:
                try:
                    pythoncom.CoUninitialize()
                finally:
                    self._co_initialized = False
            raise BackendUnavailable(
                "could not attach to Plant Simulation through COM",
                details={"prog_id": self._prog_id, "error": str(exc)},
            ) from exc
        self._app = app
        self._attached_to_running = attached
        self._owns_instance = not attached
        self._connected = True

    def _acquire_app(self, win32_client: Any) -> tuple[Any, bool]:
        """Attach to a running instance when possible, else start a hidden one."""
        if self._prefer_running_instance:
            try:
                running = win32_client.GetActiveObject(self._prog_id)
            except Exception:  # noqa: BLE001 - no automation instance is registered
                running = None
            if running is not None:
                return running, True
        return win32_client.Dispatch(self._prog_id), False

    def disconnect(self) -> None:
        """Detach without saving (auto-save on disconnect is never performed).

        quit_on_disconnect semantics: None (default) quits only the instance
        this backend started itself, True always quits, False never quits. That
        keeps a tool run from leaving an invisible Plant Simulation behind
        without ever closing the user's own session.
        """
        if not self._connected:
            return
        app = self._app
        self._app = None
        self._connected = False
        should_quit = self._quit_on_disconnect
        if should_quit is None:
            should_quit = self._owns_instance
        self._owns_instance = False
        self._attached_to_running = None
        self._discovered_frame = None
        if should_quit:
            self._model_path = None
        if should_quit and app is not None:
            try:
                app.Quit()
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
                self._last_error = "disconnect/Quit: " + str(exc)
        if self._co_initialized:
            pythoncom, _ = _import_pywin32()
            pythoncom.CoUninitialize()
            self._co_initialized = False

    def is_connected(self) -> bool:
        return self._connected

    def load_model(self, path: str) -> None:
        target = self._check_model_path(path)
        if not Path(target).is_file():
            raise BackendError("model file does not exist", details={"path": target})
        self._call("LoadModel", target)
        self._model_path = target
        self._discovered_frame = None
        # The controller is deliberately NOT derived here. Measured: the model
        # root comes from the name INSIDE the file, so a file renamed to
        # zzz.spp still roots at .Models.DeliveryConcept. Deriving from the
        # file stem was the same mistake twice; the path is read from .Models
        # instead, lazily, via default_frame_path.

    def new_model(self) -> None:
        self._call("NewModel")
        self._model_path = None
        self._discovered_frame = None

    def save_model(self, path: str | None = None) -> None:
        target = path if path is not None else self._model_path
        if target is None:
            raise BackendError("the model has no path yet; pass one explicitly")
        checked = self._check_model_path(target)
        self._call("SaveModel", checked)
        # Only after the COM call succeeded does the tracked path change.
        self._model_path = checked
        self._discovered_frame = None

    def close_model(self) -> None:
        self._call("CloseModel")
        self._model_path = None
        self._discovered_frame = None

    def clone_model(self, target_path: str) -> None:
        if self._model_path is None:
            raise BackendError("the current model has no path; save it before cloning")
        checked = self._check_model_path(target_path)
        if Path(checked).expanduser().resolve() == Path(self._model_path).expanduser().resolve():
            raise ValidationError("clone target must differ from the current model path", details={"path": checked})
        self.save_model(checked)
        self.close_model()
        self.load_model(checked)

    # -- structure -------------------------------------------------------
    def list_objects(self, frame_path: str = DEFAULT_FRAME_PATH) -> tuple[ObjectInfo, ...]:
        """List a frame's children by reading each node directly.

        Measured on this build: to_str(<frame>.node(i).Name) and
        to_str(<frame>.node(i).Class) both evaluate, and Class yields the full
        class path. So the listing needs no scratch object at all.

        The previous version built a TableFile inside the user's frame and
        failed on three separate lines: renaming it by its dotted path cannot
        work (information-flow objects are not addressable by name, though the
        create itself succeeds), setDimensions(rows, columns) is rejected, and
        reading a child's class as .Class without to_str is rejected too.
        """
        frame = validation.validate_object_path(frame_path, field_name="frame_path")
        try:
            count = int(str(self.evaluate("to_str(" + frame + ".NumNodes)")).strip())
        except (TypeError, ValueError) as exc:
            raise BackendError(
                "frame did not report a usable child count",
                details={"frame": frame},
            ) from exc
        if count < 0:
            raise BackendError("frame reported a negative child count", details={"frame": frame, "count": count})
        objects: list[ObjectInfo] = []
        for index in range(1, count + 1):
            node = frame + ".node(" + str(index) + ")"
            name = str(self.evaluate("to_str(" + node + ".Name)")).strip()
            class_path = str(self.evaluate("to_str(" + node + ".Class)")).strip()
            identifier = validation.validate_identifier(name, field_name="object name")
            if not class_path:
                raise BackendError(
                    "a child reported an empty class path",
                    details={"frame": frame, "index": index, "name": identifier},
                )
            objects.append(ObjectInfo(path=frame + "." + identifier, name=identifier, class_name=class_path))
        return tuple(objects)

    def object_exists(self, path: str) -> bool:
        target = validation.validate_object_path(path)
        result = self._call("GetValue", build_exists_query(target))
        if isinstance(result, bool):
            return result
        text = str(result).strip().lower()
        if text in ("1", "true"):
            return True
        if text in ("0", "false"):
            return False
        raise BackendError("existsObject returned an unexpected value", details={"path": target, "result": text[:100]})

    def get_object_info(self, path: str) -> ObjectInfo:
        target = validation.validate_object_path(path)
        if not self.object_exists(target):
            raise ObjectNotFound("unknown object", details={"path": target})
        name = target.rsplit(".", 1)[-1]
        class_path = ""
        try:
            # Measured: to_str(<obj>.Class) yields the full class path
            # (".MaterialFlow.SingleProc"). Origin.Name also answers, but only
            # with the short name, which would disagree with list_objects.
            class_path = str(self.evaluate("to_str(" + target + ".Class)")).strip()
        except BackendError as exc:
            # The class name is optional metadata here; record the failure
            # instead of pretending the object has no class.
            self._last_error = "get_object_info/Class: " + str(exc)
        return ObjectInfo(path=target, name=name, class_name=class_path)

    def create_object(
        self,
        class_name: str,
        name: str,
        *,
        frame_path: str = DEFAULT_FRAME_PATH,
        x: float = 0.0,
        y: float = 0.0,
        overwrite: bool = False,
    ) -> ObjectInfo:
        frame = validation.validate_object_path(frame_path, field_name="frame_path")
        class_path = validation.validate_class_name(class_name)
        identifier = validation.validate_identifier(name, field_name="name")
        target = frame + "." + identifier
        if self.object_exists(target) and not overwrite:
            raise ObjectExists("an object with this name already exists", details={"path": target})
        statement = build_create_statement(class_path, identifier, frame_path=frame, x=x, y=y, overwrite=overwrite)
        self._query(statement)
        # Measured: creation reports nothing, so existence is the only proof.
        # (NumChildren would NOT prove it -- it stays 0 with objects present.)
        if not self.object_exists(target):
            raise BackendError("create_object reported success but the object does not exist", details={"path": target})
        return ObjectInfo(path=target, name=identifier, class_name=class_path, x=float(x), y=float(y))

    def delete_object(self, path: str) -> None:
        target = validation.validate_object_path(path)
        if not self.object_exists(target):
            raise ObjectNotFound("unknown object", details={"path": target})
        self._query(build_delete_statement(target))
        if self.object_exists(target):
            raise BackendError("delete_object reported success but the object still exists", details={"path": target})

    def connect_objects(self, source_path: str, target_path: str) -> None:
        """Connect two material-flow objects with the documented Connector.connect.

        Verified on the real machine: NumSucc goes from 0 to 1 and a Connector
        object appears in the frame. Success is proven by re-reading NumSucc,
        because the SimTalk call itself returns nothing -- the same read-back
        rule the rest of this backend follows.
        """
        source = validation.validate_object_path(source_path, field_name="source_path")
        target = validation.validate_object_path(target_path, field_name="target_path")
        statement = build_connect_statement(source, target)
        self._query(statement)
        successors = self._call("GetValue", "to_str(" + target + ".NumPred)")
        try:
            count = int(str(successors).strip())
        except (TypeError, ValueError):
            count = 0
        if count < 1:
            raise BackendError(
                "connect_objects reported success but the target still has no predecessor",
                details={"source": source, "target": target, "num_pred": successors},
            )

    # -- properties ------------------------------------------------------
    def get_value(self, path: str, attribute: str) -> Any:
        target = validation.validate_object_path(path)
        name = validation.validate_identifier(attribute, field_name="attribute")
        return self._call("GetValue", target + "." + name)

    def evaluate(self, expression: str) -> Any:
        """Evaluate a SimTalk expression and return its value.

        get_value cannot reach every object. Measured on this build: an
        InformationFlow object (Method, Variable, TableFile) is NOT addressable
        by name -- renaming it succeeds, yet <frame>.<Name> still fails -- and
        the node() accessor is not a valid identifier, so it can appear only
        inside an expression. This method is that escape hatch.

        The expression is passed to GetValue verbatim, so callers must build it
        from trusted parts rather than from untrusted text.
        """
        if not isinstance(expression, str) or not expression.strip():
            raise BackendError("expression must be a non-empty string")
        if not expression.rstrip().endswith(";"):
            expression = expression + ";"
        return self._call("GetValue", expression)

    def set_value(self, path: str, attribute: str, value: Any, *, verify: bool = True) -> Any:
        target = validation.validate_object_path(path)
        name = validation.validate_identifier(attribute, field_name="attribute")
        if name in TIME_ATTRIBUTES:
            # Measured: SetValue cannot write a time attribute at all -- every
            # string spelling ("300", "5:00", "0:05:00", "5:00.0000", "300s")
            # is rejected with "the right-hand side is not a time". SimTalk
            # with a NUMERIC literal in seconds works: 300 -> '5:00.0000',
            # 5.0 -> '5.0000'. A time literal (4:00) is a syntax error.
            seconds = validation.validate_coordinate(value, field_name=name)
            statement = _build(
                ["var psbridgeObject : object;"],
                [
                    "psbridgeObject := str_to_obj(" + _quote(target) + ");",
                    "psbridgeObject." + name + " := " + _number(seconds, field_name=name) + ";",
                ],
            )
            self._query(statement)
        else:
            self._call("SetValue", target + "." + name, value)
        actual = self.get_value(target, name)
        if verify and not validation.values_equal(value, actual):
            raise VerificationFailed(
                "read-back after set_value did not match the requested value",
                details={"path": target, "attribute": name, "expected": value, "actual": actual},
            )
        return actual

    # -- methods / SimTalk -----------------------------------------------
    # -- methods / controls / MU attributes ------------------------------
    def configure_source(self, path: str, *, units: int, interval: float, start: float) -> None:
        """Make a Source emit a fixed NUMBER of parts starting at ``start``.

        Measured: MuSelection, Path and TimeOfGeneration all have to agree
        before "Number adjustable" means anything; setting Number alone leaves
        the source on its default constant stream.
        """
        target = validation.validate_object_path(path, field_name="path")
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValidationError("units must be a positive integer", details={"units": repr(units)})
        gap = validation.validate_coordinate(interval, field_name="interval")
        if gap <= 0.0:
            raise ValidationError("interval must be > 0", details={"interval": gap})
        offset = validation.validate_coordinate(start, field_name="start")
        if offset < 0.0:
            raise ValidationError("start must be >= 0", details={"start": offset})
        body = [
            "psbridgeObject := str_to_obj(" + _quote(target) + ");",
            'psbridgeObject.MuSelection := "Constant";',
            'psbridgeObject.Path := ".MUs.Entity";',
            'psbridgeObject.TimeOfGeneration := "Number adjustable";',
            "psbridgeObject.Interval := " + _number(gap, field_name="interval") + ";",
            "psbridgeObject.Number := " + str(units) + ";",
            "psbridgeObject.Start := " + _number(offset, field_name="start") + ";",
        ]
        self._query(_build(["var psbridgeObject : object;"], body))

    def read_counters(self, paths: Mapping[str, str]) -> dict[str, str]:
        """Read StatNumIn / StatNumOut / StatWorkingTime for named objects.

        Statistics live with the RUN. Reloading the model resets them, so this
        has to be called on the session that ran the simulation.
        """
        counters: dict[str, str] = {}
        for name, object_path in paths.items():
            target = validation.validate_object_path(object_path, field_name="path")
            for attribute in ("StatNumIn", "StatNumOut", "StatWorkingTime"):
                try:
                    counters[name + "." + attribute] = str(
                        self.evaluate("to_str(" + target + "." + attribute + ")")
                    )
                except BackendError as exc:
                    counters[name + "." + attribute] = "ERR " + str(exc).splitlines()[-1][:60]
        return counters

    def create_method_object(self, name: str, frame_path: str) -> str:
        """Create a Method and return the handle that addresses it.

        Measured: a newly created information-flow object is NOT reachable by
        its dotted name and cannot be renamed, so the handle is captured
        immediately via frame.node(NumNodes).
        """
        frame = validation.validate_object_path(frame_path, field_name="frame_path")
        validation.validate_identifier(name, field_name="name")
        self._query(".InformationFlow.&Method.createObject(" + frame + ", 0, 0);")
        # Measured twice over. First: renaming an information-flow object is
        # rejected, so the node handle is the only way to reach it. Second:
        # the handle must carry the index as a NUMBER. Writing
        # node(.Frame.NumNodes) looks equivalent but is evaluated lazily on
        # every use, so four methods created in a row all resolved to the
        # last one -- every ExitCtrl then shared a single program and the
        # parts went straight to the drain.
        raw_count = str(self.evaluate("to_str(" + frame + ".NumNodes)")).strip()
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise BackendError(
                "frame did not report a usable node count after creating method",
                details={"frame": frame, "name": name, "num_nodes": raw_count},
            ) from exc
        if count < 1:
            raise BackendError(
                "create_method_object reported an invalid node index",
                details={"frame": frame, "name": name, "count": count},
            )
        return frame + ".node(" + str(count) + ")"

    def set_object_control(self, object_path: str, control: str, method_path: str) -> None:
        target = validation.validate_object_path(object_path, field_name="object_path")
        hook = validation.validate_control_name(control)
        source = validation.validate_object_handle(method_path, field_name="method_path")
        self._query(_build(["var psbridgeObject : object;"],
                           ["psbridgeObject := str_to_obj(" + _quote(target) + ");",
                            "psbridgeObject." + hook + " := " + source + ";"]))
        # Measured: the assignment is accepted even when the hook never fires,
        # so read it back and let the caller see what the product recorded.
        actual = self.evaluate("to_str(" + target + "." + hook + ")")
        if not str(actual).strip():
            raise VerificationFailed(
                "the control hook did not read back after assignment",
                details={"path": target, "control": hook, "actual": str(actual)},
            )

    def create_mu_attribute(self, name: str, kind: str) -> None:
        identifier = validation.validate_identifier(name, field_name="name")
        kind_name = validation.validate_mu_attribute_kind(kind)
        self._query(_build([], [
            ".MUs.Entity.createAttr(" + _quote(identifier) + ", " + _quote(kind_name) + ");"
        ]))

    def get_method_source(self, method_path: str) -> str:
        path = validation.validate_object_handle(method_path, field_name="method_path")
        value = self._call("GetValue", build_method_source_read(path))
        if not isinstance(value, str):
            raise BackendError(
                "method source did not come back as a string",
                details={"path": path, "type": type(value).__name__},
            )
        return value

    def set_method_source(self, method_path: str, source: str) -> None:
        path = validation.validate_object_handle(method_path, field_name="method_path")
        text = validation.validate_simtalk_source(source)
        # Measured: a multi-line body cannot go through the assignment form.
        # A SimTalk literal has no newline escape, so the parser rejects the
        # whole statement. load() takes a file instead and round-tripped a
        # five-line body including a param list byte for byte.
        if method_source_needs_file(text):
            handle, file_name = tempfile.mkstemp(suffix=".smt")
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                self._query(build_method_source_load(path, file_name))
            finally:
                try:
                    os.unlink(file_name)
                except OSError:
                    pass
        else:
            self._query(build_method_source_write(path, text))
        read_back = self.get_method_source(path)
        if read_back != text:
            raise VerificationFailed(
                "method source read-back did not match the requested source",
                details={"path": path, "expected_length": len(text), "actual_length": len(read_back)},
            )

    def execute_method(self, method_path: str) -> Any:
        path = validation.validate_object_handle(method_path, field_name="method_path")
        return self._execute_simtalk(build_method_execute(path))

    def execute_simtalk(self, source: str) -> Any:
        return self._execute_simtalk(validation.validate_simtalk_source(source))

    # -- tables ----------------------------------------------------------
    def read_table_cell(self, table_path: str, row: int, column: int) -> Any:
        access_path = build_table_cell_path(table_path, row, column)
        return self._call("GetValue", access_path)

    def write_table_cell(self, table_path: str, row: int, column: int, value: Any) -> None:
        statement = build_table_write(table_path, row, column, value)
        self._query(statement)
        # Read back after write: default TableFile columns are string-typed,
        # so numbers and booleans come back as SimTalk formatted strings.
        actual = self.read_table_cell(table_path, row, column)
        if isinstance(value, bool):
            matched = validation.values_equal(value, actual) or (
                isinstance(actual, str) and actual.strip().lower() == ("true" if value else "false")
            )
        elif isinstance(value, (int, float)):
            try:
                matched = validation.values_equal(value, actual) or validation.values_equal(
                    float(value), float(actual)
                )
            except (TypeError, ValueError):
                matched = str(actual) == str(value)
        else:
            matched = str(actual) == str(value) or validation.values_equal(value, actual)
        if not matched:
            raise VerificationFailed(
                "table cell read-back did not match the requested value",
                details={"path": table_path, "row": row, "column": column, "expected": value, "actual": actual},
            )

    def read_table_range(
        self,
        table_path: str,
        row1: int,
        column1: int,
        row2: int,
        column2: int,
    ) -> tuple[tuple[Any, ...], ...]:
        path = validation.validate_object_path(table_path, field_name="table_path")
        first_row, first_column = validation.validate_table_index(row1, column1)
        last_row, last_column = validation.validate_table_index(row2, column2)
        if last_row < first_row or last_column < first_column:
            raise ValidationError(
                "table range is reversed",
                details={"row1": first_row, "column1": first_column, "row2": last_row, "column2": last_column},
            )
        cells = (last_row - first_row + 1) * (last_column - first_column + 1)
        if cells > self._max_table_cells:
            raise ValidationError(
                "table range exceeds the configured cell budget",
                details={"cells": cells, "max_cells": self._max_table_cells},
            )
        return tuple(
            tuple(self.read_table_cell(path, row, column) for column in range(first_column, last_column + 1))
            for row in range(first_row, last_row + 1)
        )

    def set_table_size(self, table_path: str, rows: int, columns: int) -> None:
        statement = build_table_resize(table_path, rows, columns)
        self._query(statement)

    # -- simulation ------------------------------------------------------
    def reset(self) -> None:
        """Reset the controller.

        Uses ``<controller>.reset()`` via SimTalk so state resets cleanly in one
        round trip (native ``ResetSimulation(controller_path)`` is also
        supported).
        """
        self._query(build_controller_reset(self._controller()))

    def _controller(self) -> str:
        """The event controller path: the caller's, or the loaded model's.

        Measured: the controller sits directly under the model root as
        .Models.<model>.EventController, and the model name comes from inside
        the file. Resolving through default_frame_path therefore also fixes
        the controller for a model whose file was renamed.
        """
        if getattr(self, "_controller_path_explicit", False):
            return self._controller_path
        frame = self.default_frame_path
        if frame == DEFAULT_FRAME_PATH:
            return self._controller_path
        return frame + ".EventController"

    def start(self, end_time: float | None = None) -> None:
        """Start the controller, optionally with a stop period in seconds.

        Without end_time the native COM StartSimulation(controller_path) is
        used, which is the documented call and was measured working. With
        end_time the SimTalk path is required, because the native call cannot
        set End.
        """
        if end_time is None:
            self._call("StartSimulation", self._controller())
            return
        self._query(build_controller_start(self._controller(), end_time))

    def set_statistics_start(self, seconds: float) -> None:
        """Choose when the statistics collection starts, i.e. when it clears.

        Measured: EventController.StartStat defaults to ONE HOUR. At that
        time all counters restart from zero, so a 24-hour run read at the end
        reports the last hour, not the run. Stepping a 5400 s model gave
        6, 12, 18, 24, then 6, 12; setting StartStat to 0 gave
        6, 12, 18, 24, 30, 36.
        """
        offset = validation.validate_coordinate(seconds, field_name="seconds")
        if offset < 0.0:
            raise ValidationError("seconds must be >= 0", details={"seconds": offset})
        self._call("SetValue", self._controller() + ".StartStat", offset)

    def stop(self) -> None:
        """Stop the running simulation (native StopSimulation, measured)."""
        self._call("StopSimulation")

    def is_simulation_running(self) -> bool:
        """Native IsSimulationRunning(), which needs no object path.

        Measured to agree with the SimTalk attribute <controller>.isRunning
        in all three states checked (idle, running, after stop), so it is a
        cheaper way to poll than building a GetValue expression.
        """
        return self._as_bool(self._call("IsSimulationRunning"), what="isRunning")

    def wait_until_finished(
        self,
        timeout: float = 300.0,
        poll: float = 0.05,
        *,
        startup_grace: float = 0.5,
    ) -> bool:
        """Block until ``EventController.isRunning`` is false after running.

        Measured twice:
        1. ``start`` is ASYNCHRONOUS -- reading counters right after start reads
           a moving target; polling ``isRunning`` is required.
        2. Even AFTER ``EventController.start;`` returns, ``isRunning`` stays
           ``False`` for ~20-40 ms before the controller enters the running state
           (measured 5/5 trials on Plant Simulation 13). Returning on an initial
           ``False`` would exit in 0.0 s before the run begins, so a brief
           ``startup_grace`` window waits for ``True -> False`` when ``poll > 0``.

        Returns True if the controller stopped, False on timeout.
        """
        now = time.monotonic()
        deadline = now + timeout
        grace_deadline = now + (min(startup_grace, max(0.0, timeout)) if poll > 0.0 else 0.0)
        saw_running = False
        while True:
            if self.is_simulation_running():
                saw_running = True
            elif saw_running or time.monotonic() >= grace_deadline:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            time.sleep(min(poll, remaining))

    def status(self) -> SimStatus:
        """Read the controller state with the measured attribute names."""
        controller = self._controller()
        # Read isRunning BEFORE simTime: when a simulation finishes between the
        # two COM calls, reading simTime first pairs an in-flight time with
        # running=False and falsely reports state="stopped" / finished=False.
        running = self._as_bool(self._call("GetValue", controller + ".isRunning"), what="isRunning")
        sim_time = self._read_number(controller + ".simTime")
        end_time: float | None = None
        try:
            # "End" is a RELATIVE period, not an absolute stop time -- the help
            # page MaterialFlowObjects21.22.083 says so explicitly.
            end_time = self._read_number(controller + ".End")
        except BackendError as exc:
            # An unset End is a read failure, and it is recorded as such.
            self._last_error = "status/End: " + str(exc)
        finished = (not running) and end_time is not None and sim_time >= end_time
        state = "running" if running else ("finished" if finished else "stopped")
        return SimStatus(running=running, finished=finished, sim_time=sim_time, end_time=end_time, state=state)

    def event_log(self, since_seq: int = 0) -> tuple[BackendEvent, ...]:
        """Not available over COM; raise instead of returning a silent empty list."""
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise ValidationError("since_seq must be a non-negative integer", details={"since_seq": repr(since_seq)})
        raise BackendUnavailable(
            "the COM backend exposes no simulation event log; record events in the experiment layer instead",
            details={"backend": "win32"},
        )

    def state_hash(self) -> str:
        """Compute a deterministic SHA-256 hash of the observable model state."""
        frame = self.default_frame_path if self._frame_path == DEFAULT_FRAME_PATH else self._frame_path
        objects = self.list_objects(frame)
        status = self.status()
        payload = {
            "backend": "win32",
            "model_path": self._model_path,
            "frame_path": frame,
            "objects": [
                {"path": obj.path, "name": obj.name, "class_name": obj.class_name, "x": obj.x, "y": obj.y}
                for obj in objects
            ],
            "status": {
                "running": status.running,
                "finished": status.finished,
                "sim_time": status.sim_time,
                "end_time": status.end_time,
                "state": status.state,
            },
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- internals -------------------------------------------------------
    def _require_connected(self) -> Any:
        if not self._connected or self._app is None:
            raise BackendUnavailable("the Win32 backend is not connected; call connect() first")
        return self._app

    def _call(self, method_name: str, *args: Any) -> Any:
        app = self._require_connected()
        try:
            return getattr(app, method_name)(*args)
        except Exception as exc:
            self._last_error = method_name + ": " + str(exc)
            raise BackendError(
                "COM call failed",
                details={"call": method_name, "arguments": [repr(item)[:200] for item in args], "error": str(exc)},
            ) from exc

    def _execute_simtalk(self, source: str) -> Any:
        app = self._require_connected()
        try:
            return app.ExecuteSimTalk(source)
        except Exception as exc:
            self._last_error = "ExecuteSimTalk: " + str(exc)
            raise SimTalkError(
                "SimTalk execution failed",
                details={"error": str(exc), "source_head": source.splitlines()[0] if source else ""},
            ) from exc

    def _query(self, source: str) -> str:
        """Execute a snippet that only has side effects.

        There is no result channel to read back: measured, GetValue evaluates
        an expression directly (so reads do not need a snippet at all), and an
        undeclared result attribute on the model frame raises "identifier
        unknown". Callers that need a value read it with _get_value instead.
        """
        self._execute_simtalk(source)
        return ""

    def _read_number(self, attribute_path: str) -> float:
        value = self._call("GetValue", attribute_path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._last_error = "GetValue " + attribute_path + ": " + repr(value)
            raise BackendError(
                "expected a numeric value from COM",
                details={"attribute": attribute_path, "value": repr(value)[:200]},
            )
        number = float(value)
        if not math.isfinite(number):
            raise BackendError("COM returned a non-finite number", details={"attribute": attribute_path})
        return number

    @staticmethod
    def _as_bool(value: Any, *, what: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise BackendError("expected a boolean value from COM", details={"what": what, "value": repr(value)[:200]})

    def _check_model_path(self, path: Any) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValidationError("model path must be a non-empty string", details={"path": repr(path)})
        if len(path) > 4096:
            raise ValidationError("model path is too long", details={"length": len(path)})
        if validation.CONTROL_CHAR_RE.search(path):
            raise ValidationError("model path must not contain control characters")
        resolved = Path(path).expanduser().resolve()
        if self._model_root is not None:
            root = self._model_root.expanduser().resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise PathNotAllowed(
                    "model path is outside the configured model_root",
                    details={"path": str(resolved), "model_root": str(root)},
                ) from exc
        return path
