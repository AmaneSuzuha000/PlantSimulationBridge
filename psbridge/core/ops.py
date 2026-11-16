"""Safety-critical operations on top of a SimBackend.

This is the only layer that should be used by tool wrappers and by the
experiment runner. It enforces core safety and verification invariants:

1. **Connect first, never list into the void.** Every listing calls
   ensure_connected() and checks that the frame exists, so an unconnected or
   model-less backend raises ObjectNotFound instead of silently returning an
   empty list. An empty frame is legitimate, and callers that need evidence can
   pass allow_empty=False.
2. **Create never overwrites.** create_device(overwrite=False) checks
   existence first and raises ObjectExists; the existing object and its values
   are left untouched. The only way to replace an object is an explicit
   overwrite=True, which is also refused on the original research model.
3. **Delete needs confirmation.** delete_device(confirm=True) refuses to run
   without the literal True and verifies the object is gone afterwards.
4. **Writes are verified twice.** set_property validates that the attribute is
   applicable to the object class, asks the backend to verify its own write,
   then reads the value back independently (VerificationFailed on mismatch).
5. **Originals stay untouched.** clone_model_for_experiment() hashes the
   original file before and after cloning, refuses to continue if the hash
   changed (ModelMutationDenied), and assert_original_untouched() re-checks the
   hash at any later point.

Optional executor: pass a ComExecutor and every backend call runs on its single
STA worker thread, with multi-call operations wrapped in an exclusive
operation scope so they can never interleave with another tool call.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from psbridge.com.backend import ObjectInfo, SimBackend, SimStatus
from psbridge.config import BridgeConfig
from psbridge.core import validation
from psbridge.errors import (
    BackendError,
    BackendUnavailable,
    ModelMutationDenied,
    ObjectExists,
    ObjectNotFound,
    PathNotAllowed,
    ValidationError,
    VerificationFailed,
)

__all__ = ["DEFAULT_FRAME_PATH", "CloneResult", "ModelFingerprint", "SafeOps", "hash_file"]

DEFAULT_FRAME_PATH = ".Models.Model"
_HASH_CHUNK_BYTES = 1 << 20
_MAX_PATH_LENGTH = 4096

T = TypeVar("T")


@dataclass(frozen=True)
class ModelFingerprint:
    """sha256 fingerprint of one model file on disk."""

    path: str
    exists: bool
    sha256: str | None
    size: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class CloneResult:
    """Evidence that cloning left the original untouched."""

    original_before: ModelFingerprint
    original_after: ModelFingerprint
    clone: ModelFingerprint
    identical: bool

    @property
    def original_unchanged(self) -> bool:
        """True when the original file hash and size did not change."""
        return (
            self.original_before.exists
            and self.original_after.exists
            and self.original_before.sha256 == self.original_after.sha256
            and self.original_before.size == self.original_after.size
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_before": self.original_before.to_dict(),
            "original_after": self.original_after.to_dict(),
            "clone": self.clone.to_dict(),
            "identical": self.identical,
            "original_unchanged": self.original_unchanged,
        }


def hash_file(path: str | Path) -> str:
    """Return the sha256 hex digest of a file (streamed, never loads it whole)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _checked_model_path(value: Any, *, model_root: Path | None) -> str:
    """Validate a model file path and enforce the configured model_root."""
    if not isinstance(value, str):
        raise ValidationError("model path must be a string", details={"path": repr(value)})
    if not value.strip():
        raise ValidationError("model path must not be empty")
    if len(value) > _MAX_PATH_LENGTH:
        raise ValidationError("model path is too long", details={"length": len(value)})
    if validation.CONTROL_CHAR_RE.search(value):
        raise ValidationError("model path must not contain control characters")
    if model_root is not None:
        root = model_root.expanduser().resolve()
        try:
            Path(value).expanduser().resolve().relative_to(root)
        except ValueError as exc:
            raise PathNotAllowed(
                "model path is outside the configured model_root",
                details={"path": value, "model_root": str(root)},
            ) from exc
    return value


class SafeOps:
    """Verified, fail-fast operations over a SimBackend."""

    def __init__(
        self,
        backend: SimBackend,
        config: BridgeConfig | None = None,
        *,
        executor: Any | None = None,
        protect_original: bool = True,
    ) -> None:
        if backend is None:
            raise ValidationError("backend is required")
        self._backend = backend
        self._config = config if config is not None else BridgeConfig()
        self._executor = executor
        self._protect_original = bool(protect_original)
        self._connect_lock = threading.Lock()
        self._original: ModelFingerprint | None = None
        self._clone: CloneResult | None = None

    # -- introspection ---------------------------------------------------
    @property
    def backend(self) -> SimBackend:
        return self._backend

    @property
    def config(self) -> BridgeConfig:
        return self._config

    @property
    def model_path(self) -> str | None:
        return self._get_model_path()

    @property
    def original_fingerprint(self) -> ModelFingerprint | None:
        return self._original

    @property
    def clone_result(self) -> CloneResult | None:
        return self._clone

    # -- connection ------------------------------------------------------
    def ensure_connected(self) -> None:
        """Connect the backend if it is not connected yet (never returns a stale state)."""
        with self._connect_lock:
            def _connect_if_needed() -> bool:
                if not self._backend.is_connected():
                    self._backend.connect()
                return self._backend.is_connected()

            if not self._run(_connect_if_needed):
                raise BackendUnavailable("backend did not report a connection after connect()")

    def connect(self) -> None:
        """Explicitly connect (same effect as the implicit connect of a listing)."""
        self.ensure_connected()

    def disconnect(self) -> None:
        """Detach from the backend; saves first only if the config demands it.

        BridgeConfig.auto_save_on_disconnect defaults to False on purpose: a
        disconnect must never overwrite the research model.
        """
        with self._exclusive("disconnect"):
            if self._config.auto_save_on_disconnect and self._run(self._backend.is_connected):
                path = self._get_model_path()
                if path is None:
                    raise BackendError("auto_save_on_disconnect is set but the model has no path")
                self._assert_not_overwriting_original("save_model", path)
                self._run(self._backend.save_model, path)
            self._run(self._backend.disconnect)

    # -- model lifecycle -------------------------------------------------
    def load_model(self, path: str) -> ModelFingerprint:
        """Load a model, track it as the original, and fingerprint the file."""
        self.ensure_connected()
        target = _checked_model_path(path, model_root=self._config.model_root)
        with self._exclusive("load_model"):
            self._run(self._backend.load_model, target)
            actual = self._get_model_path()
            if actual != target:
                raise BackendError(
                    "load_model did not update the tracked model path",
                    details={"expected": target, "actual": actual},
                )
            fingerprint = self.snapshot_model_hash(target)
            self._original = fingerprint
            self._clone = None
            return fingerprint

    def new_model(self) -> None:
        """Create an empty model (clears original/clone tracking)."""
        self.ensure_connected()
        with self._exclusive("new_model"):
            self._run(self._backend.new_model)
            self._original = None
            self._clone = None

    def save_model(self, path: str | None = None) -> ModelFingerprint:
        """Save the current model and fingerprint the written file."""
        self.ensure_connected()
        target = path if path is not None else self._get_model_path()
        if target is None:
            raise BackendError("the model has no path yet; pass one explicitly")
        checked = _checked_model_path(target, model_root=self._config.model_root)
        with self._exclusive("save_model"):
            self._assert_not_overwriting_original("save_model", checked)
            self._run(self._backend.save_model, checked)
            actual = self._get_model_path()
            if actual != checked:
                raise BackendError(
                    "save_model did not update the tracked model path",
                    details={"expected": checked, "actual": actual},
                )
            fingerprint = self.snapshot_model_hash(checked)
            if not fingerprint.exists:
                raise BackendError("save_model reported success but no file exists", details={"path": checked})
            return fingerprint

    # -- structure -------------------------------------------------------
    def list_objects(
        self,
        frame_path: str | None = None,
        *,
        allow_empty: bool = True,
    ) -> tuple[ObjectInfo, ...]:
        """List a frame after guaranteeing a live connection and a loaded model."""
        self.ensure_connected()
        frame = self._frame(frame_path)
        if not self._run(self._backend.object_exists, frame):
            raise ObjectNotFound(
                "frame not found; no model is loaded in the backend",
                details={"frame": frame, "model_path": self._get_model_path()},
            )
        objects = tuple(self._run(self._backend.list_objects, frame))
        if not objects and not allow_empty:
            raise ObjectNotFound("frame contains no objects", details={"frame": frame})
        return objects

    def object_info(self, path: str) -> ObjectInfo:
        """Return the backend snapshot of one object (ObjectNotFound when unknown)."""
        self.ensure_connected()
        target = validation.validate_object_path(path)
        return self._run(self._backend.get_object_info, target)

    def create_device(
        self,
        class_name: str,
        name: str,
        *,
        x: float = 0.0,
        y: float = 0.0,
        overwrite: bool = False,
        frame_path: str | None = None,
    ) -> ObjectInfo:
        """Create a device without ever deleting an existing object.

        overwrite=False (the default) raises ObjectExists when the name is
        taken. overwrite=True is an explicit replacement request and is refused
        on the original research model (ModelMutationDenied).
        """
        self.ensure_connected()
        class_path = validation.validate_class_name(class_name)
        identifier = validation.validate_identifier(name, field_name="name")
        frame = self._frame(frame_path)
        x_value = validation.validate_coordinate(x, field_name="x")
        y_value = validation.validate_coordinate(y, field_name="y")
        path = validation.join_object_path(frame, identifier)
        with self._exclusive("create_device"):
            if self._run(self._backend.object_exists, path) and not overwrite:
                raise ObjectExists(
                    "an object with this name already exists; create_device never overwrites",
                    details={"path": path},
                )
            self._assert_mutation_allowed("create_device", path)
            info = self._run(
                self._backend.create_object,
                class_path,
                identifier,
                frame_path=frame,
                x=x_value,
                y=y_value,
                overwrite=overwrite,
            )
            if not self._run(self._backend.object_exists, path):
                raise BackendError(
                    "create_object reported success but the object does not exist",
                    details={"path": path},
                )
            return info

    def delete_device(
        self,
        name_or_path: str,
        *,
        confirm: bool = False,
        frame_path: str | None = None,
    ) -> None:
        """Delete one object; requires confirm=True and verifies the deletion."""
        if confirm is not True:
            raise ValidationError(
                "delete_device requires confirm=True (destructive operation)",
                details={"confirm": repr(confirm)},
            )
        self.ensure_connected()
        path = self._resolve_path(name_or_path, frame_path=frame_path)
        if not self._run(self._backend.object_exists, path):
            raise ObjectNotFound("unknown object", details={"path": path})
        with self._exclusive("delete_device"):
            self._assert_mutation_allowed("delete_device", path)
            self._run(self._backend.delete_object, path)
            if self._run(self._backend.object_exists, path):
                raise BackendError(
                    "delete_object reported success but the object still exists",
                    details={"path": path},
                )

    def connect_objects(self, source: str, target: str, *, frame_path: str | None = None) -> None:
        """Connect two objects after checking that both exist."""
        self.ensure_connected()
        source_path = self._resolve_path(source, frame_path=frame_path)
        target_path = self._resolve_path(target, frame_path=frame_path)
        for path in (source_path, target_path):
            if not self._run(self._backend.object_exists, path):
                raise ObjectNotFound("unknown object", details={"path": path})
        with self._exclusive("connect_objects"):
            self._assert_mutation_allowed("connect_objects", source_path)
            self._run(self._backend.connect_objects, source_path, target_path)

    # -- properties ------------------------------------------------------
    def get_property(self, path: str, attribute: str, *, frame_path: str | None = None) -> Any:
        """Read one allow-listed attribute of an object."""
        self.ensure_connected()
        target = self._resolve_path(path, frame_path=frame_path)
        info = self._run(self._backend.get_object_info, target)
        name = validation.validate_attribute(self._class_name_of(info), attribute)
        return self._run(self._backend.get_value, target, name)

    def set_property(
        self,
        path: str,
        attribute: str,
        value: Any,
        *,
        frame_path: str | None = None,
    ) -> Any:
        """Write one allow-listed attribute with an independent read-back check."""
        self.ensure_connected()
        target = self._resolve_path(path, frame_path=frame_path)
        info = self._run(self._backend.get_object_info, target)
        class_name = self._class_name_of(info)
        name = validation.validate_attribute(class_name, attribute)
        normalised = validation.validate_attribute_value(class_name, name, value)
        with self._exclusive("set_property"):
            self._assert_mutation_allowed("set_property", target)
            self._run(self._backend.set_value, target, name, normalised, verify=True)
            actual = self._run(self._backend.get_value, target, name)
            if not validation.values_equal(normalised, actual):
                raise VerificationFailed(
                    "independent read-back after set_value did not match the requested value",
                    details={"path": target, "attribute": name, "expected": normalised, "actual": actual},
                )
            return actual

    # -- model hashes / clones -------------------------------------------
    def configure_source(
        self,
        path: str,
        *,
        units: int,
        interval: float,
        start: float = 0.0,
        frame_path: str | None = None,
    ) -> None:
        self.ensure_connected()
        target = self._resolve_path(path, frame_path=frame_path)
        # Measured: an interval of 0 was accepted here and only failed deep in
        # SimTalk as a bare "execution failed". A Source that emits infinitely
        # often is nonsense, so say so before touching the model.
        if isinstance(units, bool) or not isinstance(units, int) or units < 1:
            raise ValidationError("units must be a positive integer", details={"units": repr(units)})
        gap = validation.validate_coordinate(interval, field_name="interval")
        if gap <= 0.0:
            raise ValidationError("interval must be a positive number of seconds", details={"interval": repr(interval)})
        offset = validation.validate_coordinate(start, field_name="start")
        if offset < 0.0:
            raise ValidationError("start must be zero or a positive number of seconds", details={"start": repr(start)})
        with self._exclusive("configure_source"):
            self._assert_mutation_allowed("configure_source", target)
            self._run(self._backend.configure_source, target, units=units, interval=gap, start=offset)

    def read_counters(
        self, names: list[str], *, frame_path: str | None = None
    ) -> dict[str, str]:
        self.ensure_connected()
        frame = self._frame(frame_path)
        paths = {}
        for name in names:
            identifier = validation.validate_identifier(name, field_name="object name")
            paths[identifier] = frame + "." + identifier
        return dict(self._run(self._backend.read_counters, paths))

    # -- methods, controls, MU attributes ---------------------------------
    def create_method(self, name: str, source: str, *, frame_path: str | None = None) -> str:
        """Create a Method object and write its program.

        Measured: the Method class is .InformationFlow.&Method, and once created
        the object is NOT addressable by its dotted name -- so the path is
        resolved through frame.node(NumNodes) immediately after creation.
        """
        self.ensure_connected()
        identifier = validation.validate_identifier(name, field_name="name")
        frame = self._frame(frame_path)
        text = validation.validate_simtalk_source(source, field_name="source")
        with self._exclusive("create_method"):
            self._assert_mutation_allowed("create_method", validation.join_object_path(frame, identifier))
            path = self._run(self._backend.create_method_object, identifier, frame)
            self._run(self._backend.set_method_source, path, text)
        return path

    def set_method_source(self, path: str, source: str) -> str:
        self.ensure_connected()
        target = validation.validate_object_handle(path, field_name="path")
        text = validation.validate_simtalk_source(source, field_name="source")
        with self._exclusive("set_method_source"):
            self._assert_mutation_allowed("set_method_source", target)
            self._run(self._backend.set_method_source, target, text)
        return text

    def get_method_source(self, path: str) -> str:
        self.ensure_connected()
        target = validation.validate_object_handle(path, field_name="path")
        return str(self._run(self._backend.get_method_source, target))

    def set_object_control(self, path: str, control: str, method: str) -> str:
        """Attach a Method as one of the object's control hooks.

        Measured: a control only runs when the method body is declared
        ":boolean is ... return true; end;" -- a body without it is accepted by
        the assignment and never fires.
        """
        self.ensure_connected()
        target = validation.validate_object_path(path, field_name="path")
        hook = validation.validate_control_name(control)
        source = validation.validate_object_handle(method, field_name="method")
        with self._exclusive("set_object_control"):
            self._assert_mutation_allowed("set_object_control", target)
            self._run(self._backend.set_object_control, target, hook, source)
        return source

    def create_mu_attribute(self, name: str, kind: str) -> None:
        self.ensure_connected()
        identifier = validation.validate_identifier(name, field_name="name")
        kind_name = validation.validate_mu_attribute_kind(kind)
        with self._exclusive("create_mu_attribute"):
            self._assert_mutation_allowed("create_mu_attribute", identifier)
            self._run(self._backend.create_mu_attribute, identifier, kind_name)

    # -- simulation ------------------------------------------------------
    def reset_simulation(self) -> None:
        """Reset the EventController. Does not touch the model."""
        self.ensure_connected()
        with self._exclusive("reset"):
            self._run(self._backend.reset)

    def start_simulation(self, *, end_time: float | None = None,
                         statistics_start: float | None = 0.0) -> None:
        """Start the controller and return immediately.

        Measured: EventController.start is ASYNCHRONOUS. The COM call comes
        back long before the run ends, so a caller that reads counters next
        reads a moving target. Use run_simulation unless you mean to poll.

        Measured, and the reason statistics_start defaults to 0: StartStat
        is the time at which statistics collection begins, and the counters
        restart from zero there. The default of one hour means any run
        longer than an hour reports only its last hour. 0 keeps them
        cumulative. Pass None to leave the model's own value alone.
        """
        self.ensure_connected()
        seconds: float | None = None
        if end_time is not None:
            seconds = validation.validate_coordinate(end_time, field_name="end_time")
            if seconds <= 0.0:
                raise ValidationError("end_time must be > 0", details={"end_time": seconds})
        offset: float | None = None
        if statistics_start is not None:
            offset = validation.validate_coordinate(
                statistics_start, field_name="statistics_start")
            if offset < 0.0:
                raise ValidationError("statistics_start must be >= 0", details={"statistics_start": offset})
        with self._exclusive("start"):
            if offset is not None:
                self._run(self._backend.set_statistics_start, offset)
            self._run(self._backend.start, seconds)

    def stop_simulation(self) -> None:
        self.ensure_connected()
        with self._exclusive("stop"):
            self._run(self._backend.stop)

    def simulation_status(self) -> SimStatus:
        self.ensure_connected()
        return self._run(self._backend.status)

    def run_simulation(
        self,
        *,
        end_time: float | None = None,
        timeout: float = 300.0,
        poll: float = 0.05,
        statistics_start: float | None = 0.0,
        startup_grace: float = 1.0,
    ) -> SimStatus:
        """Start the controller and block until it stops.

        Polling isRunning is the only trustworthy completion signal on this
        build; sleeping for a guessed duration gave 232 parts on one run and
        223 on the next. On Plant Simulation 13, ``EventController.start``
        also exhibits a ~20-40 ms startup window where ``isRunning`` is still
        ``False`` and ``finished`` is ``False``, so ``run_simulation`` waits up
        to ``startup_grace`` for the run to enter ``running=True`` or report
        ``finished=True`` before accepting a stopped state.
        """
        budget = validation.validate_coordinate(timeout, field_name="timeout")
        interval = validation.validate_coordinate(poll, field_name="poll")
        grace = validation.validate_coordinate(startup_grace, field_name="startup_grace")
        if budget <= 0 or interval <= 0:
            raise ValidationError("timeout and poll must be positive")
        if grace < 0:
            raise ValidationError("startup_grace must be non-negative")
        self.start_simulation(end_time=end_time, statistics_start=statistics_start)
        now = time.monotonic()
        deadline = now + budget
        grace_deadline = now + min(grace, budget)
        saw_running = False
        while True:
            status = self.simulation_status()
            if status.running:
                saw_running = True
            elif status.finished or saw_running or time.monotonic() >= grace_deadline:
                return status
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(min(interval, max(0.0, deadline - now)))
        with contextlib.suppress(Exception):
            self._run(self._backend.stop)
        raise BackendError(
            "simulation did not stop before the timeout",
            details={"timeout": budget, "end_time": end_time},
        )

    def snapshot_model_hash(self, path: str | None = None) -> ModelFingerprint:
        """Fingerprint a model file (the tracked original by default)."""
        target: str | None
        if path is not None:
            target = path
        elif self._original is not None:
            target = self._original.path
        else:
            target = self._get_model_path()
        if target is None:
            raise ValidationError("no model path is available for hashing")
        file_path = Path(target).expanduser()
        if not file_path.is_file():
            return ModelFingerprint(path=str(file_path), exists=False, sha256=None, size=None)
        return ModelFingerprint(
            path=str(file_path),
            exists=True,
            sha256=hash_file(file_path),
            size=file_path.stat().st_size,
        )

    def verify_model_hash(self, fingerprint: ModelFingerprint | None = None) -> bool:
        """Return True when the current file still matches the reference fingerprint."""
        reference = fingerprint if fingerprint is not None else self._original
        if reference is None:
            raise BackendError("no reference fingerprint is available; call load_model() first")
        current = self.snapshot_model_hash(reference.path)
        return bool(current.exists and reference.exists and current.sha256 == reference.sha256)

    def assert_original_untouched(self) -> ModelFingerprint:
        """Raise VerificationFailed if the tracked original file changed."""
        if self._original is None:
            raise BackendError("no original model is tracked; call load_model() first")
        current = self.snapshot_model_hash(self._original.path)
        if not current.exists:
            raise VerificationFailed(
                "the original model file disappeared",
                details={"path": self._original.path},
            )
        if current.sha256 != self._original.sha256 or current.size != self._original.size:
            raise VerificationFailed(
                "the original model file changed",
                details={
                    "path": self._original.path,
                    "expected_sha256": self._original.sha256,
                    "actual_sha256": current.sha256,
                    "expected_size": self._original.size,
                    "actual_size": current.size,
                },
            )
        return current

    def clone_model_for_experiment(
        self,
        work_dir: str | Path,
        *,
        tag: str = "exp",
    ) -> CloneResult:
        """Clone the tracked original into work_dir and prove it stayed untouched."""
        self.ensure_connected()
        if self._original is None:
            raise BackendError("no original model is tracked; call load_model() first")
        identifier = validation.validate_identifier(tag, field_name="tag")
        before = self.snapshot_model_hash(self._original.path)
        if not before.exists or before.sha256 is None:
            raise BackendError("the original model file is missing", details={"path": self._original.path})
        directory = Path(work_dir).expanduser().resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError("could not create the experiment directory", details={"path": str(directory), "error": str(exc)}) from exc
        target_path = str(directory / (Path(self._original.path).stem + "__" + identifier + ".spp"))
        if Path(target_path).resolve() == Path(self._original.path).resolve():
            raise PathNotAllowed("clone target would overwrite the original model", details={"path": target_path})
        with self._exclusive("clone_model_for_experiment"):
            self._run(self._backend.clone_model, target_path)
            actual = self._get_model_path()
            if actual != target_path:
                raise BackendError(
                    "clone_model did not activate the clone",
                    details={"expected": target_path, "actual": actual},
                )
            after = self.snapshot_model_hash(self._original.path)
            if not after.exists or after.sha256 != before.sha256 or after.size != before.size:
                raise ModelMutationDenied(
                    "cloning changed the original model file",
                    details={"path": self._original.path, "before": before.to_dict(), "after": after.to_dict()},
                )
            clone = self.snapshot_model_hash(target_path)
            if not clone.exists:
                raise BackendError("clone_model reported success but no clone file exists", details={"path": target_path})
            result = CloneResult(
                original_before=before,
                original_after=after,
                clone=clone,
                identical=clone.sha256 == before.sha256,
            )
            self._clone = result
            return result

    # -- internals -------------------------------------------------------
    def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call the backend directly, or through the executor when one is configured."""
        if self._executor is None:
            return fn(*args, **kwargs)
        return self._executor.call(fn, *args, timeout=self._config.com_timeout_s, **kwargs)

    @contextlib.contextmanager
    def _exclusive(self, name: str) -> Iterator[Any]:
        """Exclusive operation scope when an executor is configured."""
        if self._executor is None:
            yield None
            return
        with self._executor.operation(name, self._config.operation_timeout_s) as scope:
            yield scope

    @staticmethod
    def _class_name_of(info: ObjectInfo) -> str:
        """Return the object's class, or fail loudly when the backend omits it.

        Without a class name the attribute allow-list cannot be applied, and
        silently skipping the check would defeat the whole safety layer.
        """
        name = getattr(info, "class_name", "") or ""
        if not isinstance(name, str) or not name.strip():
            raise BackendError(
                "the backend did not report the object's class, so attribute applicability cannot be checked",
                details={"path": info.path},
            )
        return name

    def _get_model_path(self) -> str | None:
        if self._executor is not None:
            return self._run(lambda: self._backend.model_path)
        return self._backend.model_path

    def _frame(self, frame_path: str | None) -> str:
        """Resolve the frame a bare object name lives in.

        Callers rarely know the model root, and the constant default
        .Models.Model only exists for a model literally called Model. Asking
        the backend for the loaded model's root is both correct and better
        at reporting: without it every bare name failed as "unknown object"
        and there was no way to tell a missing object from a missing frame.
        """
        if frame_path is not None:
            return validation.validate_object_path(frame_path, field_name="frame_path")
        if self._executor is not None:
            derived = self._run(lambda: getattr(self._backend, "default_frame_path", None))
        else:
            derived = getattr(self._backend, "default_frame_path", None)
        if derived:
            return validation.validate_object_path(derived, field_name="frame_path")
        return validation.validate_object_path(DEFAULT_FRAME_PATH, field_name="frame_path")

    def resolve_frame(self, frame_path: str | None = None) -> str:
        """Resolve a caller frame path or the loaded model's default frame path."""
        self.ensure_connected()
        return self._frame(frame_path)

    def _resolve_path(self, value: str, *, frame_path: str | None = None) -> str:
        """Accept either an absolute object path or a bare object name."""
        if not isinstance(value, str):
            raise ValidationError("path must be a string", details={"path": repr(value)})
        if value.startswith("."):
            return validation.validate_object_path(value)
        frame = self._frame(frame_path)
        return validation.join_object_path(frame, value)

    def _assert_not_overwriting_original(self, action: str, path: str) -> None:
        """Refuse saving over the tracked original research model."""
        if not self._protect_original or self._original is None:
            return
        if Path(path).expanduser().resolve() == Path(self._original.path).expanduser().resolve():
            raise ModelMutationDenied(
                "refusing to overwrite the original research model; save to a clone path instead",
                details={"action": action, "path": path},
            )

    def _assert_mutation_allowed(self, action: str, path: str) -> None:
        """Refuse mutations that would hit the tracked original model in place."""
        if not self._protect_original:
            return
        if self._original is None:
            return
        current = self._get_model_path()
        if current is None:
            return
        if Path(current).expanduser().resolve() == Path(self._original.path).expanduser().resolve():
            raise ModelMutationDenied(
                "refusing to mutate the original research model in place; clone it for experiments",
                details={"action": action, "path": path, "model_path": current},
            )
