"""Backend protocol: the only seam between the bridge and Plant Simulation.

Two implementations are expected:

* Win32ComBackend - talks to Tecnomatix Plant Simulation RemoteControl via
  pywin32 (Windows only, requires a real Plant Simulation installation),
* FakeBackend - deterministic in-memory implementation used by tests and by
  CI machines without Plant Simulation.

The protocol is intentionally transport-neutral. It does NOT expose a
dispatch/decision interface: scheduling experiments talk to
psbridge.experiment.engine.DispatchEngine instead, because the decision loop
on the real system is implemented in SimTalk and must be validated separately
(see docs/REAL_MACHINE_CHECKLIST.md).

Conventions (contract, not implementation detail):
  * every object path is absolute and dotted, e.g. .Models.Model.Station1
  * DataTable row/column indices passed to read_table_cell, write_table_cell,
    read_table_range and set_table_size are **1-based and inclusive**, matching
    SimTalk data-table indexing. read_table_range(t, 1, 1, n, m) therefore reads
    rows 1..n and columns 1..m. Do not mix 0-based Python conventions in.
  * a write with verify=True returns the value read back from the model, never
    the value that was sent.

Failure contract (all implementations must obey it):
  * unknown object -> ObjectNotFound
  * create with existing name and overwrite=False -> ObjectExists
  * create with a class outside the allow-list -> TypeNotAllowed
  * set_value with verify=True -> read back; mismatch -> VerificationFailed
  * SimTalk/COM failure -> SimTalkError or BackendError (never a silent no-op)
  * unreachable system -> BackendUnavailable
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ObjectInfo:
    """Snapshot of one model object."""

    path: str
    name: str
    class_name: str
    x: float = 0.0
    y: float = 0.0
    predecessors: tuple[str, ...] = ()
    successors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimStatus:
    """Simulation status snapshot."""

    running: bool
    finished: bool
    sim_time: float
    end_time: float | None = None
    state: str = "unknown"


@dataclass(frozen=True)
class BackendEvent:
    """One deterministic event emitted by the backend simulation."""

    seq: int
    time: float
    kind: str
    object_path: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendCapabilities:
    """Static capabilities, so callers can fail fast instead of guessing."""

    kind: str
    supports_simtalk: bool = True
    supports_tables: bool = True
    supports_methods: bool = True
    supports_clone_model: bool = True
    supports_screenshots: bool = False
    supports_dispatch_engine: bool = False


@runtime_checkable
class SimBackend(Protocol):
    """Transport-neutral Plant Simulation backend."""

    @property
    def capabilities(self) -> BackendCapabilities:
        ...

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        """Attach to a running Plant Simulation instance (or start one)."""

    def disconnect(self) -> None:
        """Detach. Must NOT save/overwrite the model unless explicitly asked."""

    def is_connected(self) -> bool:
        ...

    def load_model(self, path: str) -> None:
        ...

    def new_model(self) -> None:
        ...

    def save_model(self, path: str | None = None) -> None:
        """Save to path, and update the tracked model path on success."""

    def close_model(self) -> None:
        ...

    def clone_model(self, target_path: str) -> None:
        """Persist the current model to target_path and activate that copy."""

    @property
    def model_path(self) -> str | None:
        ...

    # -- structure -------------------------------------------------------
    def list_objects(self, frame_path: str = ".Models.Model") -> tuple[ObjectInfo, ...]:
        ...

    def object_exists(self, path: str) -> bool:
        ...

    def get_object_info(self, path: str) -> ObjectInfo:
        ...

    def create_object(
        self,
        class_name: str,
        name: str,
        *,
        frame_path: str = ".Models.Model",
        x: float = 0.0,
        y: float = 0.0,
        overwrite: bool = False,
    ) -> ObjectInfo:
        ...

    def delete_object(self, path: str) -> None:
        ...

    def connect_objects(self, source_path: str, target_path: str) -> None:
        ...

    # -- properties ------------------------------------------------------
    def get_value(self, path: str, attribute: str) -> Any:
        ...

    def set_value(self, path: str, attribute: str, value: Any, *, verify: bool = True) -> Any:
        """Set a value and return the verified read-back value."""

    # -- methods / SimTalk / tables / controls ---------------------------
    def configure_source(self, path: str, *, units: int, interval: float, start: float = 0.0) -> None:
        ...

    def read_counters(self, paths: dict[str, str]) -> dict[str, str]:
        ...

    def create_method_object(self, name: str, frame_path: str = ".Models.Model") -> str:
        ...

    def set_object_control(self, object_path: str, control: str, method_path: str) -> None:
        ...

    def create_mu_attribute(self, name: str, kind: str) -> None:
        ...

    def get_method_source(self, method_path: str) -> str:
        ...

    def set_method_source(self, method_path: str, source: str) -> None:
        ...

    def execute_method(self, method_path: str) -> Any:
        ...

    def execute_simtalk(self, source: str) -> Any:
        ...

    def read_table_cell(self, table_path: str, row: int, column: int) -> Any:
        ...

    def write_table_cell(self, table_path: str, row: int, column: int, value: Any) -> None:
        ...

    def read_table_range(self, table_path: str, row1: int, column1: int, row2: int, column2: int) -> tuple[tuple[Any, ...], ...]:
        ...

    def set_table_size(self, table_path: str, rows: int, columns: int) -> None:
        ...

    # -- simulation ------------------------------------------------------
    def set_statistics_start(self, seconds: float) -> None:
        ...

    def reset(self) -> None:
        ...

    def start(self, end_time: float | None = None) -> None:
        ...

    def stop(self) -> None:
        ...

    def status(self) -> SimStatus:
        ...

    def event_log(self, since_seq: int = 0) -> tuple[BackendEvent, ...]:
        ...

    def state_hash(self) -> str:
        """Stable hash of the observable model/simulation state."""
