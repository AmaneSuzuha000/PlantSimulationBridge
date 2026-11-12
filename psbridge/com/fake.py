"""Deterministic in-memory SimBackend (no COM, no Plant Simulation).

The fake backend is the reference implementation of the failure contract in
psbridge.com.backend, and it is what CI and the experiment layer run against:

* an object that already exists is never deleted by create_object - with
  overwrite=False it raises ObjectExists, with overwrite=True it reconfigures
  the existing object in place and records an object_reconfigured event,
* a class outside the allow-list raises TypeNotAllowed, a malformed identifier
  raises ValidationError, an unknown object raises ObjectNotFound,
* set_value verifies its own write by reading the value back and raises
  VerificationFailed when the two disagree (see FakeFault.silent),
* a simulated SimTalk/COM runtime error raises SimTalkError (see
  FakeFault),
* nothing is ever saved implicitly: disconnect() only detaches, save_model()
  is the single path that writes a file.

Persistence: save_model writes a canonical JSON document (format marker
psbridge-fake-model) instead of a .spp file, so model-file hashes and clone
semantics can be exercised without Plant Simulation. load_model requires that
file to exist and to carry the marker; it never invents a model.

Simulation clock: the fake has no dynamics. start(end_time) marks the run as
running, advance(seconds) moves the deterministic clock forward (fake-only
method, not part of the SimBackend protocol), and reaching end_time finishes
the run. status() is a pure read.

The class/attribute allow-lists live in psbridge.core.validation, a
dependency-free leaf module shared with the safety layer, so the fake and the
safety checks cannot drift apart.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    ModelMutationDenied,
    ObjectExists,
    ObjectNotFound,
    SimTalkError,
    SimulationError,
    TypeNotAllowed,
    ValidationError,
    VerificationFailed,
)

__all__ = ["DEFAULT_FRAME_PATH", "FAKE_MODEL_FORMAT", "FakeBackend", "FakeFault"]

#: Marker written into every fake model file.
FAKE_MODEL_FORMAT = "psbridge-fake-model"
#: Version of the fake model document.
FAKE_MODEL_VERSION = 1
#: Default frame every object lives in.
DEFAULT_FRAME_PATH = ".Models.Model"
#: Largest accepted model file path.
MAX_PATH_LENGTH = 4096

_KIND_BY_CLASS = {
    ".InformationFlow.&Method": "method",
    ".InformationFlow.&TableFile": "table",
}


@dataclass(frozen=True)
class FakeFault:
    """An injected failure used to exercise the error paths of the bridge.

    operation:
        Backend operation the fault applies to, e.g. "set_value",
        "execute_method", "create_object", "write_table_cell",
        "execute_simtalk".
    target:
        Optional selector. It matches an attribute name, an object path, a
        class path or a "path.attribute" string, depending on the operation.
        None matches every invocation of that operation.
    message:
        Message carried by the raised SimTalkError.
    silent:
        When True the write is dropped without raising, so the read-back
        verification (and therefore the caller) sees the mismatch rather than
        reporting a silent skip as success.
    """

    operation: str
    target: str | None = None
    message: str = "simulated SimTalk failure"
    silent: bool = False


@dataclass
class _FakeObject:
    """One object in the fake model."""

    path: str
    name: str
    class_path: str
    x: float = 0.0
    y: float = 0.0
    kind: str = "object"
    values: dict[str, Any] = field(default_factory=dict)
    predecessors: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)
    source: str = ""
    executions: int = 0
    rows: int = 0
    columns: int = 0
    cells: list[list[Any]] = field(default_factory=list)
    controls: dict[str, str] = field(default_factory=dict)

    def info(self) -> ObjectInfo:
        return ObjectInfo(
            path=self.path,
            name=self.name,
            class_name=self.class_path,
            x=self.x,
            y=self.y,
            predecessors=tuple(self.predecessors),
            successors=tuple(self.successors),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "class_path": self.class_path,
            "x": self.x,
            "y": self.y,
            "kind": self.kind,
            "values": dict(self.values),
            "predecessors": list(self.predecessors),
            "successors": list(self.successors),
            "source": self.source,
            "executions": self.executions,
            "rows": self.rows,
            "columns": self.columns,
            "cells": [list(row) for row in self.cells],
            "controls": dict(self.controls),
        }


def _normalise_cell(value: Any, *, where: str) -> Any:
    """Validate a table cell value (scalars only)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError("table cell must be a finite number", details={"where": where, "value": repr(value)})
        return value
    if isinstance(value, str):
        if len(value) > validation.MAX_STRING_LITERAL_LENGTH:
            raise ValidationError("table cell string is too long", details={"where": where, "length": len(value)})
        if validation.CONTROL_CHAR_RE.search(value):
            raise ValidationError("table cell must not contain control characters", details={"where": where})
        return value
    raise ValidationError(
        "table cells must be scalars (None/bool/int/float/str)",
        details={"where": where, "type": type(value).__name__},
    )


class FakeBackend:
    """In-memory SimBackend implementing the full protocol failure contract."""

    def __init__(
        self,
        *,
        frame_path: str = DEFAULT_FRAME_PATH,
        max_table_cells: int = 100_000,
        default_end_time: float = 60.0,
        clock_start: float = 0.0,
    ) -> None:
        self._frame_path = validation.validate_object_path(frame_path, field_name="frame_path")
        if isinstance(max_table_cells, bool) or not isinstance(max_table_cells, int) or max_table_cells < 1:
            raise ValidationError("max_table_cells must be a positive integer", details={"max_table_cells": repr(max_table_cells)})
        self._max_table_cells = max_table_cells
        self._default_end_time = validation.validate_coordinate(default_end_time, field_name="default_end_time")
        if self._default_end_time <= 0.0:
            raise ValidationError("default_end_time must be > 0", details={"default_end_time": default_end_time})

        self._objects: dict[str, _FakeObject] = {}
        self._mu_attributes: dict[str, str] = {}
        self._connected = False
        self._model_loaded = False
        self._model_path: str | None = None

        self._sim_time = validation.validate_coordinate(clock_start, field_name="clock_start")
        self._statistics_start = 0.0
        self._running = False
        self._finished = False
        self._end_time: float | None = None
        self._state = "idle"

        self._events: list[BackendEvent] = []
        self._seq = 0
        self._faults: list[FakeFault] = []
        self._simtalk_log: list[str] = []

        # Observability counters (used by tests and evidence dumps).
        self.connect_count = 0
        self.disconnect_count = 0
        self.new_model_count = 0
        self.load_count = 0
        self.save_count = 0
        self.clone_count = 0
        self.create_count = 0
        self.delete_count = 0
        self.saved_paths: list[str] = []

    # -- capabilities ----------------------------------------------------
    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            kind="fake",
            supports_simtalk=True,
            supports_tables=True,
            supports_methods=True,
            supports_clone_model=True,
            supports_screenshots=False,
            supports_dispatch_engine=False,
        )

    # -- introspection helpers -------------------------------------------
    @property
    def frame_path(self) -> str:
        return self._frame_path

    @property
    def model_path(self) -> str | None:
        return self._model_path

    @property
    def default_frame_path(self) -> str:
        """The fake model has one fixed root, whatever its file is called.

        The real backend derives this from the file name because its models
        really are rooted at .Models.<stem>; this stand-in keeps a single
        frame so tests can address it without guessing.
        """
        return self._frame_path

    @property
    def simtalk_log(self) -> tuple[str, ...]:
        """Every raw SimTalk block passed to execute_simtalk (in order)."""
        return tuple(self._simtalk_log)

    @property
    def faults(self) -> tuple[FakeFault, ...]:
        return tuple(self._faults)

    @property
    def statistics_start(self) -> float:
        return self._statistics_start

    @property
    def mu_attributes(self) -> dict[str, str]:
        return dict(self._mu_attributes)

    # -- fault injection --------------------------------------------------
    def inject_fault(self, fault: FakeFault) -> None:
        """Schedule a failure for a later backend call (test hook)."""
        if not isinstance(fault, FakeFault):
            raise ValidationError("fault must be a FakeFault instance")
        self._faults.append(fault)

    def clear_faults(self) -> None:
        """Remove every scheduled failure."""
        self._faults.clear()

    def _fault_for(self, operation: str, *targets: str) -> FakeFault | None:
        for fault in self._faults:
            if fault.operation != operation:
                continue
            if fault.target is None or fault.target in targets:
                return fault
        return None

    def _apply_fault(self, operation: str, *targets: str) -> FakeFault | None:
        """Raise for a loud fault; return the fault for a silent one."""
        fault = self._fault_for(operation, *targets)
        if fault is None:
            return None
        if fault.silent:
            return fault
        raise SimTalkError(fault.message, details={"operation": operation, "targets": list(targets), "injected": True})

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        if self._connected:
            return
        self._connected = True
        self.connect_count += 1
        self._state = "connected"
        self._emit("connected", self._frame_path)

    def disconnect(self) -> None:
        if not self._connected:
            return
        # Deliberately no save_model here: disconnect must never write the model.
        self._connected = False
        self.disconnect_count += 1
        self._state = "disconnected"
        self._emit("disconnected", self._frame_path)

    def is_connected(self) -> bool:
        return self._connected

    def new_model(self) -> None:
        self._require_connected()
        self._objects.clear()
        self._mu_attributes.clear()
        self._model_path = None
        self._model_loaded = True
        self._reset_sim_state()
        self.new_model_count += 1
        self._emit("model_created", self._frame_path)

    def load_model(self, path: str) -> None:
        self._require_connected()
        target = self._validate_file_path(path)
        file_path = Path(target)
        if not file_path.is_file():
            raise BackendError("model file does not exist", details={"path": target})
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BackendError("could not read the model file", details={"path": target, "error": str(exc)}) from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise BackendError("model file is not valid JSON", details={"path": target, "error": str(exc)}) from exc
        if not isinstance(data, dict) or data.get("format") != FAKE_MODEL_FORMAT:
            raise BackendError(
                "file is not a psbridge fake model document",
                details={"path": target, "format": FAKE_MODEL_FORMAT},
            )
        if data.get("version") != FAKE_MODEL_VERSION:
            raise BackendError(
                "unsupported fake model version",
                details={"path": target, "version": data.get("version"), "expected": FAKE_MODEL_VERSION},
            )
        objects = self._objects_from_payload(data.get("objects"))
        raw_mu_attrs = data.get("mu_attributes", {})
        if not isinstance(raw_mu_attrs, dict):
            raise BackendError("model document has invalid mu_attributes", details={"path": target})
        mu_attributes = {
            validation.validate_identifier(key, field_name="mu_attribute"): validation.validate_mu_attribute_kind(
                kind
            )
            for key, kind in raw_mu_attrs.items()
        }
        self._objects = objects
        self._mu_attributes = mu_attributes
        self._model_path = target
        self._model_loaded = True
        self._reset_sim_state()
        self.load_count += 1
        self._emit("model_loaded", self._frame_path, path=target, objects=len(objects))

    def save_model(self, path: str | None = None) -> None:
        self._require_connected()
        self._require_model()
        target = path if path is not None else self._model_path
        if target is None:
            raise BackendError("the model has no path yet; pass one explicitly")
        target = self._validate_file_path(target)
        payload = self._model_payload()
        text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        file_path = Path(target)
        if not file_path.parent.exists():
            raise BackendError("target directory does not exist", details={"path": target, "directory": str(file_path.parent)})
        try:
            file_path.write_text(text, encoding="utf-8")
            read_back = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BackendError("could not write the model file", details={"path": target, "error": str(exc)}) from exc
        if read_back != text:
            raise BackendError("model file read-back does not match what was written", details={"path": target})
        self._model_path = target
        self.save_count += 1
        self.saved_paths.append(target)
        self._emit("model_saved", self._frame_path, path=target)

    def close_model(self) -> None:
        self._require_connected()
        self._require_model()
        self._objects.clear()
        self._mu_attributes.clear()
        self._model_path = None
        self._model_loaded = False
        self._reset_sim_state()
        self._emit("model_closed", self._frame_path)

    def clone_model(self, target_path: str) -> None:
        self._require_connected()
        self._require_model()
        if self._model_path is None:
            raise BackendError("the current model has no file path; save it before cloning")
        target = self._validate_file_path(target_path)
        if target == self._model_path:
            raise ValidationError(
                "clone target must differ from the current model path",
                details={"path": target},
            )
        content_before = self.model_content_hash()
        self.save_model(target)
        self.load_model(target)
        if self.model_content_hash() != content_before:
            raise BackendError(
                "cloning changed the model content (serialisation is not lossless)",
                details={"source": self.saved_paths[-1], "target": target},
            )
        self.clone_count += 1
        self._emit("model_cloned", self._frame_path, target=target)

    # -- structure -------------------------------------------------------
    def list_objects(self, frame_path: str = DEFAULT_FRAME_PATH) -> tuple[ObjectInfo, ...]:
        self._require_connected()
        frame = validation.validate_object_path(frame_path, field_name="frame_path")
        if not self._model_loaded:
            raise ObjectNotFound("no model is loaded", details={"frame": frame})
        if frame != self._frame_path:
            raise ObjectNotFound("frame not found", details={"frame": frame, "known_frame": self._frame_path})
        return tuple(obj.info() for obj in self._objects.values())

    def object_exists(self, path: str) -> bool:
        self._require_connected()
        target = validation.validate_object_path(path)
        if self._model_loaded and target == self._frame_path:
            return True
        return target in self._objects

    def get_object_info(self, path: str) -> ObjectInfo:
        self._require_connected()
        target = validation.validate_object_path(path)
        if self._model_loaded and target == self._frame_path:
            return ObjectInfo(
                path=self._frame_path,
                name=self._frame_path.rsplit(".", 1)[-1],
                class_name=".Models.Frame",
            )
        return self._object(target).info()

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
        self._require_connected()
        self._require_model()
        class_path = validation.validate_class_name(class_name, allow_auxiliary=True)
        identifier = validation.validate_identifier(name, field_name="name")
        frame = self._require_frame(frame_path)
        x_value = validation.validate_coordinate(x, field_name="x")
        y_value = validation.validate_coordinate(y, field_name="y")
        self._apply_fault("create_object", class_path, identifier)
        path = frame + "." + identifier
        existing = self._objects.get(path)
        if existing is not None:
            if not overwrite:
                raise ObjectExists(
                    "an object with this name already exists (create never deletes)",
                    details={"path": path, "class_name": existing.class_path},
                )
            existing.class_path = class_path
            existing.kind = _KIND_BY_CLASS.get(class_path, "object")
            existing.x = x_value
            existing.y = y_value
            existing.values = validation.default_values(class_path)
            existing.predecessors = []
            existing.successors = []
            existing.source = ""
            existing.executions = 0
            existing.rows = 1 if existing.kind == "table" else 0
            existing.columns = 1 if existing.kind == "table" else 0
            existing.cells = [[None]] if existing.kind == "table" else []
            existing.controls = {}
            self._emit("object_reconfigured", path, class_path=class_path)
            return existing.info()
        obj = _FakeObject(
            path=path,
            name=identifier,
            class_path=class_path,
            x=x_value,
            y=y_value,
            kind=_KIND_BY_CLASS.get(class_path, "object"),
            values=validation.default_values(class_path),
        )
        if obj.kind == "table":
            obj.rows = 1
            obj.columns = 1
            obj.cells = [[None]]
        self._objects[path] = obj
        self.create_count += 1
        self._emit("object_created", path, class_path=class_path, x=x_value, y=y_value)
        return obj.info()

    def delete_object(self, path: str) -> None:
        self._require_connected()
        self._require_model()
        target = validation.validate_object_path(path)
        if target == self._frame_path:
            raise ModelMutationDenied("the root frame cannot be deleted", details={"path": target})
        if target not in self._objects:
            raise ObjectNotFound("unknown object", details={"path": target})
        self._apply_fault("delete_object", target)
        del self._objects[target]
        for other in self._objects.values():
            other.predecessors = [item for item in other.predecessors if item != target]
            other.successors = [item for item in other.successors if item != target]
        self.delete_count += 1
        self._emit("object_deleted", target)

    def connect_objects(self, source_path: str, target_path: str) -> None:
        self._require_connected()
        self._require_model()
        source = validation.validate_object_path(source_path, field_name="source_path")
        target = validation.validate_object_path(target_path, field_name="target_path")
        if source == target:
            raise ValidationError("an object cannot be connected to itself", details={"path": source})
        source_object = self._object(source)
        target_object = self._object(target)
        if target in source_object.successors:
            raise BackendError("connector already exists", details={"source": source, "target": target})
        self._apply_fault("connect_objects", source, target)
        source_object.successors.append(target)
        target_object.predecessors.append(source)
        self._emit("objects_connected", source, target=target)

    # -- properties ------------------------------------------------------
    def get_value(self, path: str, attribute: str) -> Any:
        self._require_connected()
        obj = self._object(validation.validate_object_path(path))
        name = validation.validate_attribute(obj.class_path, attribute)
        return obj.values[name]

    def set_value(self, path: str, attribute: str, value: Any, *, verify: bool = True) -> Any:
        self._require_connected()
        target = validation.validate_object_path(path)
        obj = self._object(target)
        name = validation.validate_attribute(obj.class_path, attribute)
        normalised = validation.validate_attribute_value(obj.class_path, name, value)
        fault = self._apply_fault("set_value", name, target, target + "." + name)
        if fault is None:
            obj.values[name] = normalised
        self._emit("value_set", target, attribute=name, value=normalised, applied=fault is None)
        if verify:
            actual = obj.values[name]
            if not validation.values_equal(normalised, actual):
                raise VerificationFailed(
                    "read-back after set_value did not match the requested value",
                    details={
                        "path": target,
                        "attribute": name,
                        "expected": normalised,
                        "actual": actual,
                        "silent_fault": fault is not None,
                    },
                )
            return actual
        return obj.values[name]

    # -- methods / SimTalk -----------------------------------------------
    def get_method_source(self, method_path: str) -> str:
        self._require_connected()
        obj = self._object(validation.validate_object_path(method_path))
        if obj.kind != "method":
            raise TypeNotAllowed("object is not a method", details={"path": obj.path, "kind": obj.kind})
        return obj.source

    def set_method_source(self, method_path: str, source: str) -> None:
        self._require_connected()
        target = validation.validate_object_path(method_path)
        obj = self._object(target)
        if obj.kind != "method":
            raise TypeNotAllowed("object is not a method", details={"path": target, "kind": obj.kind})
        text = validation.validate_simtalk_source(source, field_name="source")
        self._apply_fault("set_method_source", target)
        obj.source = text
        if obj.source != text:
            raise VerificationFailed(
                "method source read-back did not match the requested source",
                details={"path": target},
            )
        self._emit("method_source_set", target, length=len(text))

    def execute_method(self, method_path: str) -> Any:
        self._require_connected()
        target = validation.validate_object_path(method_path)
        obj = self._object(target)
        if obj.kind != "method":
            raise TypeNotAllowed("object is not a method", details={"path": target, "kind": obj.kind})
        self._apply_fault("execute_method", target)
        obj.executions += 1
        self._emit("method_executed", target, executions=obj.executions)
        return {
            "method": target,
            "executions": obj.executions,
            "sim_time": self._sim_time,
            "source_length": len(obj.source),
        }

    def execute_simtalk(self, source: str) -> Any:
        self._require_connected()
        text = validation.validate_simtalk_source(source, field_name="source")
        self._apply_fault("execute_simtalk", text)
        self._simtalk_log.append(text)
        self._emit("simtalk_executed", self._frame_path, length=len(text))
        return {
            "ok": True,
            "statements": len([line for line in text.splitlines() if line.strip()]),
            "sim_time": self._sim_time,
        }

    # -- tables ----------------------------------------------------------
    def read_table_cell(self, table_path: str, row: int, column: int) -> Any:
        self._require_connected()
        obj = self._require_table(table_path)
        row_index, column_index = validation.validate_table_cell_bounds(row, column, rows=obj.rows, columns=obj.columns)
        return obj.cells[row_index - 1][column_index - 1]

    def write_table_cell(self, table_path: str, row: int, column: int, value: Any) -> None:
        self._require_connected()
        obj = self._require_table(table_path)
        row_index, column_index = validation.validate_table_cell_bounds(row, column, rows=obj.rows, columns=obj.columns)
        cell = _normalise_cell(value, where=obj.path + "[" + str(row_index) + "," + str(column_index) + "]")
        fault = self._apply_fault("write_table_cell", obj.path)
        if fault is None:
            obj.cells[row_index - 1][column_index - 1] = cell
        self._emit("table_cell_written", obj.path, row=row_index, column=column_index, applied=fault is None)

    def read_table_range(
        self,
        table_path: str,
        row1: int,
        column1: int,
        row2: int,
        column2: int,
    ) -> tuple[tuple[Any, ...], ...]:
        self._require_connected()
        obj = self._require_table(table_path)
        first_row, first_column, last_row, last_column = validation.validate_table_range(
            row1,
            column1,
            row2,
            column2,
            rows=obj.rows,
            columns=obj.columns,
            max_cells=self._max_table_cells,
        )
        return tuple(
            tuple(obj.cells[row][column] for column in range(first_column - 1, last_column))
            for row in range(first_row - 1, last_row)
        )

    def set_table_size(self, table_path: str, rows: int, columns: int) -> None:
        self._require_connected()
        obj = self._require_table(table_path)
        row_count, column_count = validation.validate_table_size(rows, columns, max_cells=self._max_table_cells)
        self._apply_fault("set_table_size", obj.path)
        cells: list[list[Any]] = []
        for row in range(row_count):
            if row < obj.rows:
                previous = obj.cells[row]
                cells.append([previous[column] if column < obj.columns else None for column in range(column_count)])
            else:
                cells.append([None for _ in range(column_count)])
        obj.rows = row_count
        obj.columns = column_count
        obj.cells = cells
        self._emit("table_resized", obj.path, rows=row_count, columns=column_count)

    # -- simulation ------------------------------------------------------
    def configure_source(self, path: str, *, units: int, interval: float, start: float = 0.0) -> None:
        self._require_connected()
        self._require_model()
        target = validation.validate_object_path(path, field_name="path")
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise ValidationError("units must be a positive integer", details={"units": repr(units)})
        gap = validation.validate_coordinate(interval, field_name="interval")
        if gap <= 0.0:
            raise ValidationError("interval must be > 0", details={"interval": gap})
        offset = validation.validate_coordinate(start, field_name="start")
        if offset < 0.0:
            raise ValidationError("start must be >= 0", details={"start": offset})
        obj = self._object(target)
        if obj.class_path != ".MaterialFlow.Source":
            raise TypeNotAllowed(
                "configure_source requires a Source object",
                details={"path": target, "class_path": obj.class_path},
            )
        self._apply_fault("configure_source", target)
        obj.values.update(
            {
                "MuSelection": "Constant",
                "Path": ".MUs.Entity",
                "TimeOfGeneration": "Number adjustable",
                "Interval": gap,
                "Number": units,
                "Start": offset,
            }
        )
        self._emit("source_configured", target, units=units, interval=gap, start=offset)

    def read_counters(self, paths: Mapping[str, str]) -> dict[str, str]:
        self._require_connected()
        self._require_model()
        if not isinstance(paths, Mapping):
            raise ValidationError("paths must be a mapping of name to object path")
        counters: dict[str, str] = {}
        for name, object_path in paths.items():
            key = validation.validate_identifier(name, field_name="name")
            target = validation.validate_object_path(object_path, field_name="path")
            obj = self._objects.get(target)
            for attribute, default in (("StatNumIn", "0"), ("StatNumOut", "0"), ("StatWorkingTime", "0.0")):
                if obj is None:
                    counters[key + "." + attribute] = "ERR unknown object: " + target
                else:
                    counters[key + "." + attribute] = str(obj.values.get(attribute, default))
        return counters

    def create_method_object(self, name: str, frame_path: str = DEFAULT_FRAME_PATH) -> str:
        self._require_connected()
        self._require_model()
        return self.create_object("Method", name, frame_path=frame_path).path

    def set_object_control(self, object_path: str, control: str, method_path: str) -> None:
        self._require_connected()
        self._require_model()
        target = validation.validate_object_path(object_path, field_name="object_path")
        hook = validation.validate_control_name(control)
        source = validation.validate_object_handle(method_path, field_name="method_path")
        obj = self._object(target)
        if ".node(" not in source:
            method_obj = self._object(source)
            if method_obj.kind != "method":
                raise TypeNotAllowed(
                    "control target is not a Method",
                    details={"path": source, "kind": method_obj.kind},
                )
        self._apply_fault("set_object_control", target, hook, target + "." + hook)
        obj.controls[hook] = source
        self._emit("object_control_set", target, control=hook, method=source)

    def create_mu_attribute(self, name: str, kind: str) -> None:
        self._require_connected()
        self._require_model()
        identifier = validation.validate_identifier(name, field_name="name")
        kind_name = validation.validate_mu_attribute_kind(kind)
        self._apply_fault("create_mu_attribute", identifier)
        self._mu_attributes[identifier] = kind_name
        self._emit("mu_attribute_created", self._frame_path, name=identifier, kind=kind_name)

    def set_statistics_start(self, seconds: float) -> None:
        self._require_connected()
        self._require_model()
        offset = validation.validate_coordinate(seconds, field_name="seconds")
        if offset < 0.0:
            raise ValidationError("seconds must be >= 0", details={"seconds": offset})
        self._apply_fault("set_statistics_start", self._frame_path)
        self._statistics_start = offset
        self._emit("statistics_start_set", self._frame_path, seconds=offset)

    def reset(self) -> None:
        self._require_connected()
        self._require_model()
        self._reset_sim_state()
        self._state = "reset"
        self._emit("sim_reset", self._frame_path)

    def start(self, end_time: float | None = None) -> None:
        self._require_connected()
        self._require_model()
        if self._running:
            raise SimulationError("simulation is already running", details={"sim_time": self._sim_time})
        if end_time is None:
            resolved = self._default_end_time
        else:
            resolved = validation.validate_coordinate(end_time, field_name="end_time")
            if resolved <= self._sim_time:
                raise ValidationError(
                    "end_time must be greater than the current simulation time",
                    details={"end_time": resolved, "sim_time": self._sim_time},
                )
        self._running = True
        self._finished = False
        self._end_time = resolved
        self._state = "running"
        self._emit("sim_started", self._frame_path, end_time=resolved)

    def stop(self) -> None:
        self._require_connected()
        if not self._running:
            raise SimulationError("simulation is not running", details={"state": self._state})
        self._running = False
        self._state = "stopped"
        self._emit("sim_stopped", self._frame_path, sim_time=self._sim_time)

    def advance(self, seconds: float) -> float:
        """Move the deterministic fake clock forward (fake-only helper)."""
        self._require_connected()
        if not self._running:
            raise SimulationError("simulation is not running", details={"state": self._state})
        step = validation.validate_coordinate(seconds, field_name="seconds")
        if step <= 0.0:
            raise ValidationError("seconds must be > 0", details={"seconds": step})
        self._sim_time += step
        if self._end_time is not None and self._sim_time >= self._end_time:
            self._sim_time = self._end_time
            self._running = False
            self._finished = True
            self._state = "finished"
            self._emit("sim_finished", self._frame_path, sim_time=self._sim_time)
        else:
            self._emit("sim_advanced", self._frame_path, sim_time=self._sim_time)
        return self._sim_time

    def status(self) -> SimStatus:
        self._require_connected()
        state = self._state if self._model_loaded else "no_model"
        return SimStatus(
            running=self._running,
            finished=self._finished,
            sim_time=self._sim_time,
            end_time=self._end_time,
            state=state,
        )

    def event_log(self, since_seq: int = 0) -> tuple[BackendEvent, ...]:
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise ValidationError("since_seq must be a non-negative integer", details={"since_seq": repr(since_seq)})
        return tuple(event for event in self._events if event.seq > since_seq)

    # -- hashes ----------------------------------------------------------
    def state_hash(self) -> str:
        """sha256 over the observable model state plus the simulation state."""
        payload = {
            "frame_path": self._frame_path,
            "model_path": self._model_path,
            "mu_attributes": dict(self._mu_attributes),
            "objects": [obj.to_dict() for obj in self._sorted_objects()],
            "simulation": {
                "running": self._running,
                "finished": self._finished,
                "sim_time": self._sim_time,
                "statistics_start": self._statistics_start,
                "end_time": self._end_time,
                "state": self._state,
            },
        }
        return self._hash_payload(payload)

    def model_content_hash(self) -> str:
        """sha256 over the model content only (no simulation state)."""
        payload = {
            "frame_path": self._frame_path,
            "mu_attributes": dict(self._mu_attributes),
            "objects": [obj.to_dict() for obj in self._sorted_objects()],
        }
        return self._hash_payload(payload)

    # -- internals -------------------------------------------------------
    def _sorted_objects(self) -> list[_FakeObject]:
        return [self._objects[path] for path in sorted(self._objects)]

    @staticmethod
    def _hash_payload(payload: Mapping[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _emit(self, kind: str, object_path: str, /, **payload: Any) -> None:
        self._seq += 1
        self._events.append(
            BackendEvent(seq=self._seq, time=self._sim_time, kind=kind, object_path=object_path, payload=dict(payload))
        )

    def _require_connected(self) -> None:
        if not self._connected:
            raise BackendUnavailable("the fake backend is not connected; call connect() first")

    def _require_model(self) -> None:
        if not self._model_loaded:
            raise ObjectNotFound("no model is loaded; call new_model() or load_model() first")

    def _require_frame(self, frame_path: str) -> str:
        frame = validation.validate_object_path(frame_path, field_name="frame_path")
        if frame != self._frame_path:
            raise ObjectNotFound("frame not found", details={"frame": frame, "known_frame": self._frame_path})
        return frame

    def _object(self, path: str) -> _FakeObject:
        obj = self._objects.get(path)
        if obj is None:
            raise ObjectNotFound("unknown object", details={"path": path})
        return obj

    def _require_table(self, table_path: str) -> _FakeObject:
        target = validation.validate_object_path(table_path, field_name="table_path")
        obj = self._object(target)
        if obj.kind != "table":
            raise TypeNotAllowed("object is not a DataTable", details={"path": target, "kind": obj.kind})
        return obj

    def _reset_sim_state(self) -> None:
        self._sim_time = 0.0
        self._statistics_start = 0.0
        self._running = False
        self._finished = False
        self._end_time = None
        self._state = "idle"

    @staticmethod
    def _validate_file_path(value: Any) -> str:
        if not isinstance(value, str):
            raise ValidationError("model path must be a string", details={"path": repr(value)})
        if not value:
            raise ValidationError("model path must not be empty")
        if len(value) > MAX_PATH_LENGTH:
            raise ValidationError("model path is too long", details={"length": len(value)})
        if "\x00" in value:
            raise ValidationError("model path must not contain NUL", details={"path": value})
        return value

    def _model_payload(self) -> dict[str, Any]:
        return {
            "format": FAKE_MODEL_FORMAT,
            "version": FAKE_MODEL_VERSION,
            "frame_path": self._frame_path,
            "mu_attributes": dict(self._mu_attributes),
            "objects": [obj.to_dict() for obj in self._sorted_objects()],
        }

    def _objects_from_payload(self, payload: Any) -> dict[str, _FakeObject]:
        if not isinstance(payload, list):
            raise BackendError("model document has no object list", details={"payload_type": type(payload).__name__})
        objects: dict[str, _FakeObject] = {}
        for entry in payload:
            if not isinstance(entry, dict):
                raise BackendError("model document contains a non-object entry")
            path = validation.validate_object_path(entry.get("path"), field_name="path")
            class_path = validation.validate_class_name(entry.get("class_path"), allow_auxiliary=True)
            raw_controls = entry.get("controls", {})
            if not isinstance(raw_controls, dict):
                raise BackendError("model document object has invalid controls", details={"path": path})
            controls = {
                validation.validate_control_name(hook): validation.validate_object_handle(target, field_name="method_path")
                for hook, target in raw_controls.items()
            }
            obj = _FakeObject(
                path=path,
                name=validation.validate_identifier(entry.get("name"), field_name="name"),
                class_path=class_path,
                x=validation.validate_coordinate(entry.get("x", 0.0), field_name="x"),
                y=validation.validate_coordinate(entry.get("y", 0.0), field_name="y"),
                kind=str(entry.get("kind", _KIND_BY_CLASS.get(class_path, "object"))),
                values=dict(entry.get("values", {})),
                predecessors=[str(item) for item in entry.get("predecessors", [])],
                successors=[str(item) for item in entry.get("successors", [])],
                source=str(entry.get("source", "")),
                executions=int(entry.get("executions", 0)),
                rows=int(entry.get("rows", 0)),
                columns=int(entry.get("columns", 0)),
                cells=[[_normalise_cell(cell, where=path) for cell in row] for row in entry.get("cells", [])],
                controls=controls,
            )
            objects[path] = obj
        return objects