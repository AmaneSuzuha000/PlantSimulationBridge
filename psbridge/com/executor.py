"""Single-worker execution gateway for COM (STA) calls.

Why this module exists
----------------------
Plant Simulation's COM objects live in a single-threaded apartment: a COM proxy
may only be used from the thread that created it. Every backend call therefore
has to run on one dedicated worker thread, never on the caller's thread and
never on two threads at once.

Two levels of serialisation are provided:

1. **Call level** - call(fn, *args, timeout=...) queues fn on the worker thread
   and returns its result. A timeout raises OperationTimeout but never
   corrupts the gateway: the worker finishes the stale call, then keeps serving
   the queue (a queued call that timed out before it started is cancelled and
   never executes).
2. **Operation level** - operation(name, timeout=...) is an exclusive context
   for multi-call tool operations (create = exists-check + create + verify).
   While one thread owns an operation, calls from other threads block at the
   gate, so a multi-call operation can never interleave with another call. The
   context is re-entrant for the owning thread, so nested operations and the
   calls they issue are allowed.

Failure contract
----------------
* fn raises on the worker thread  -> the same exception (same type, same
  message, original traceback) is raised to the caller.
* call or operation deadline exceeded -> OperationTimeout.
* executor already closed             -> ExecutorClosed.
* initializer failed                  -> BackendUnavailable (chained).
* fn is not callable, or a timeout argument is not a positive finite number
  -> ValidationError.

There is no "wait forever" mode: default_timeout is mandatory and every
operation budget also caps the calls issued inside it.

The threading logic is covered by offline unit tests, while the COM STA
initializer path (`pythoncom.CoInitialize`) is exercised with Win32ComBackend
against Plant Simulation 13.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psbridge.config import DEFAULT_TIMEOUT_S
from psbridge.errors import (
    BackendUnavailable,
    ExecutorClosed,
    OperationTimeout,
    ValidationError,
)

if TYPE_CHECKING:
    from typing import Self

__all__ = ["ComExecutor", "ExecutorStats", "OperationScope"]

Initializer = Callable[[], None]
Finalizer = Callable[[], None]

#: Queue sentinel that tells the worker to run the finalizer and exit.
_STOP = object()


@dataclass(frozen=True)
class ExecutorStats:
    """Immutable snapshot of gateway activity (for logs and MCP diagnostics)."""

    name: str
    started: bool
    closed: bool
    worker_thread_id: int | None
    calls: int
    failures: int
    timeouts: int
    rejected: int
    last_duration_s: float | None
    total_busy_s: float
    last_error: str | None
    current_operation: str | None
    operation_depth: int
    pending: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of the snapshot."""
        return {
            "name": self.name,
            "started": self.started,
            "closed": self.closed,
            "worker_thread_id": self.worker_thread_id,
            "calls": self.calls,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "rejected": self.rejected,
            "last_duration_s": self.last_duration_s,
            "total_busy_s": self.total_busy_s,
            "last_error": self.last_error,
            "current_operation": self.current_operation,
            "operation_depth": self.operation_depth,
            "pending": self.pending,
        }


@dataclass
class _WorkItem:
    """One queued backend call."""

    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any]
    enqueued_at: float


def _validate_timeout(value: Any, *, field_name: str = "timeout") -> float:
    """Validate a positive, finite timeout in seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field_name + " must be a number", details={field_name: repr(value)})
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValidationError(field_name + " must be a positive finite number", details={field_name: value})
    return number


class OperationScope:
    """Exclusive, re-entrant operation context returned by ComExecutor.operation.

    Attributes
    ----------
    name:
        Caller supplied operation label (recorded in stats and errors).
    deadline:
        Monotonic deadline (time.monotonic based) or None.
    depth:
        1 for an outermost operation, 2 for a nested one.
    calls:
        Number of backend calls issued while this scope was the innermost one.
    """

    def __init__(self, executor: ComExecutor, name: str, timeout: float | None) -> None:
        self._executor = executor
        self.name = name
        self._timeout = timeout
        self.deadline: float | None = None
        self.depth = 0
        self.calls = 0
        self.entered_at: float | None = None
        self._entered = False

    # -- introspection ---------------------------------------------------
    @property
    def elapsed_s(self) -> float:
        """Seconds spent inside the scope so far."""
        if self.entered_at is None:
            return 0.0
        return time.monotonic() - self.entered_at

    @property
    def remaining_s(self) -> float | None:
        """Seconds left before the scope deadline (None if unbounded)."""
        if self.deadline is None:
            return None
        return max(self.deadline - time.monotonic(), 0.0)

    # -- context manager -------------------------------------------------
    def __enter__(self) -> Self:
        if self._entered:
            raise ValidationError("operation scope is already active", details={"operation": self.name})
        self._executor._enter_operation(self)
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        # _entered is cleared only after a successful exit: a rejected
        # out-of-order exit must not mark the scope closed while it is still on
        # the stack, otherwise the exclusivity gate would stay held forever.
        if not self._entered:
            return
        self._executor._exit_operation(self)
        self._entered = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "OperationScope(name=" + repr(self.name) + ", depth=" + str(self.depth) + ", calls=" + str(self.calls) + ")"


class ComExecutor:
    """Owns the single worker thread every backend call must run on."""

    def __init__(
        self,
        *,
        name: str = "com",
        default_timeout: float = DEFAULT_TIMEOUT_S,
        default_operation_timeout: float | None = None,
        initializer: Initializer | None = None,
        finalizer: Finalizer | None = None,
        daemon: bool = True,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name must be a non-empty string")
        self._name = name
        self._default_timeout = _validate_timeout(default_timeout, field_name="default_timeout")
        self._default_operation_timeout = (
            self._default_timeout
            if default_operation_timeout is None
            else _validate_timeout(default_operation_timeout, field_name="default_operation_timeout")
        )
        self._initializer = initializer
        self._finalizer = finalizer
        self._daemon = bool(daemon)

        self._queue: queue.Queue[Any] = queue.Queue()
        self._gate = threading.RLock()
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._worker_thread_id: int | None = None
        self._init_error: BaseException | None = None
        self._init_done = threading.Event()
        self._finalizer_error: BaseException | None = None

        self._calls = 0
        self._failures = 0
        self._timeouts = 0
        self._rejected = 0
        self._last_duration: float | None = None
        self._total_busy = 0.0
        self._last_error: str | None = None

        self._op_stack: list[OperationScope] = []
        self._op_owner: int | None = None

    # -- lifecycle -------------------------------------------------------
    def start(
        self,
        *,
        initializer: Initializer | None = None,
        finalizer: Finalizer | None = None,
    ) -> None:
        """Start the worker thread and run the initializer on it.

        The initializer is where pythoncom.CoInitialize() belongs: it must run
        on the thread that will own the COM proxies.
        """
        with self._state_lock:
            if self._closed:
                raise ExecutorClosed("executor is closed", details={"name": self._name})
            if not self._started:
                if initializer is not None:
                    self._initializer = initializer
                if finalizer is not None:
                    self._finalizer = finalizer
                self._started = True
                thread = threading.Thread(target=self._worker_main, name=self._name + "-worker", daemon=self._daemon)
                self._thread = thread
                thread.start()
        if not self._init_done.wait(timeout=self._default_timeout):
            raise OperationTimeout(
                "executor initializer did not finish in time",
                details={"name": self._name, "timeout_s": self._default_timeout},
            )
        if self._init_error is not None:
            raise BackendUnavailable(
                "executor worker failed to initialise",
                details={"name": self._name, "error": repr(self._init_error)},
            ) from self._init_error

    def close(self, *, timeout: float | None = None, finalizer: Finalizer | None = None) -> None:
        """Stop the worker thread after running the finalizer on it.

        Closing is idempotent. It waits for an in-flight operation owned by
        another thread (bounded by timeout) so the finalizer cannot run in the
        middle of a multi-call operation.
        """
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            started = self._started
        if finalizer is not None:
            self._finalizer = finalizer
        if not started or thread is None:
            return
        budget = _validate_timeout(timeout) if timeout is not None else self._default_timeout
        deadline = time.monotonic() + budget
        acquired = False
        if not self._is_op_owner():
            remaining = deadline - time.monotonic()
            if remaining > 0.0:
                acquired = self._gate.acquire(timeout=remaining)
        try:
            self._queue.put(_STOP)
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        finally:
            if acquired:
                self._gate.release()
        if thread.is_alive():
            raise OperationTimeout(
                "executor worker did not stop in time",
                details={"name": self._name, "timeout_s": budget, "worker_thread_id": self.thread_id},
            )

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    # -- properties ------------------------------------------------------
    @property
    def name(self) -> str:
        return self._name

    @property
    def thread_id(self) -> int | None:
        """Thread id of the worker (None before it started)."""
        with self._state_lock:
            return self._worker_thread_id

    @property
    def is_started(self) -> bool:
        with self._state_lock:
            return self._started

    @property
    def is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    @property
    def finalizer_error(self) -> BaseException | None:
        """Exception raised by the finalizer, if any (never silently dropped)."""
        with self._state_lock:
            return self._finalizer_error

    # -- calls -----------------------------------------------------------
    def call(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        """Run fn(*args, **kwargs) on the worker thread and return its result."""
        if not callable(fn):
            raise ValidationError("fn must be callable", details={"fn": repr(fn)})
        self._ensure_usable()
        budget = self._resolve_budget(timeout, kind="call")
        deadline = time.monotonic() + budget
        acquired = self._acquire_gate(deadline, context="call")
        try:
            item = _WorkItem(fn=fn, args=args, kwargs=kwargs, future=Future(), enqueued_at=time.monotonic())
            with self._state_lock:
                if self._closed:
                    self._count_rejected()
                    raise ExecutorClosed("executor is closed", details={"name": self._name})
                self._queue.put(item)
            scope = self._current_scope()
            if scope is not None:
                scope.calls += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                item.future.cancel()
                self._count_timeout()
                raise OperationTimeout(
                    "call timed out before the worker could start it",
                    details={"name": self._name, "timeout_s": budget},
                )
            try:
                return item.future.result(timeout=remaining)
            except FutureTimeout:
                if item.future.done():
                    # The callable itself raised TimeoutError: propagate it as is.
                    raise
                item.future.cancel()
                self._count_timeout()
                raise OperationTimeout(
                    "backend call exceeded its timeout; the worker stays usable",
                    details={"name": self._name, "timeout_s": budget},
                ) from None
        finally:
            if acquired:
                self._gate.release()

    # -- operations ------------------------------------------------------
    def operation(self, name: str, timeout: float | None = None) -> OperationScope:
        """Return an exclusive operation context (re-entrant for its owner)."""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("operation name must be a non-empty string", details={"name": repr(name)})
        budget = self._resolve_budget(timeout, kind="operation")
        return OperationScope(self, name, budget)

    def _enter_operation(self, scope: OperationScope) -> None:
        self._ensure_usable()
        budget = scope._timeout if scope._timeout is not None else self._default_operation_timeout
        deadline = time.monotonic() + budget
        owner = threading.get_ident()
        if self._is_op_owner(owner):
            self._gate.acquire()
        else:
            acquired = self._gate.acquire(timeout=budget)
            if not acquired:
                self._count_timeout()
                raise OperationTimeout(
                    "timed out waiting for exclusive operation access",
                    details={"name": self._name, "operation": scope.name, "timeout_s": budget},
                )
        with self._stats_lock:
            if self._op_stack:
                outer = self._op_stack[-1]
                if outer.deadline is not None:
                    deadline = min(deadline, outer.deadline)
                scope.depth = outer.depth + 1
            else:
                scope.depth = 1
                self._op_owner = owner
            scope.deadline = deadline
            scope.entered_at = time.monotonic()
            self._op_stack.append(scope)

    def _exit_operation(self, scope: OperationScope) -> None:
        with self._stats_lock:
            if not self._op_stack or self._op_stack[-1] is not scope:
                # Only the innermost scope may be exited; anything else is a bug.
                raise ValidationError(
                    "operation scopes must be exited in reverse order",
                    details={"operation": scope.name, "stack": [item.name for item in self._op_stack]},
                )
            self._op_stack.pop()
            if not self._op_stack:
                self._op_owner = None
        self._gate.release()

    def _current_scope(self) -> OperationScope | None:
        with self._stats_lock:
            return self._op_stack[-1] if self._op_stack else None

    def _is_op_owner(self, thread_id: int | None = None) -> bool:
        target = threading.get_ident() if thread_id is None else thread_id
        with self._stats_lock:
            return self._op_owner == target

    # -- stats -----------------------------------------------------------
    def stats(self) -> ExecutorStats:
        """Return a consistent snapshot of gateway activity."""
        with self._stats_lock:
            calls = self._calls
            failures = self._failures
            timeouts = self._timeouts
            rejected = self._rejected
            last_duration = self._last_duration
            total_busy = self._total_busy
            last_error = self._last_error
            current = self._op_stack[-1].name if self._op_stack else None
            depth = len(self._op_stack)
        with self._state_lock:
            started = self._started
            closed = self._closed
            worker_thread_id = self._worker_thread_id
        return ExecutorStats(
            name=self._name,
            started=started,
            closed=closed,
            worker_thread_id=worker_thread_id,
            calls=calls,
            failures=failures,
            timeouts=timeouts,
            rejected=rejected,
            last_duration_s=last_duration,
            total_busy_s=total_busy,
            last_error=last_error,
            current_operation=current,
            operation_depth=depth,
            pending=self._queue.qsize(),
        )

    # -- internals -------------------------------------------------------
    def _ensure_usable(self) -> None:
        with self._state_lock:
            if self._closed:
                self._count_rejected()
                raise ExecutorClosed("executor is closed", details={"name": self._name})
            ready = self._started and self._init_done.is_set()
        if not ready:
            self.start()
        if self._init_error is not None:
            raise BackendUnavailable(
                "executor worker failed to initialise",
                details={"name": self._name, "error": repr(self._init_error)},
            ) from self._init_error

    def _resolve_budget(self, timeout: float | None, *, kind: str) -> float:
        """Return the effective budget, capped by an enclosing operation."""
        if timeout is not None:
            budget = _validate_timeout(timeout)
        else:
            budget = self._default_timeout if kind == "call" else self._default_operation_timeout
        scope = self._current_scope()
        if scope is not None and scope.deadline is not None:
            remaining = scope.deadline - time.monotonic()
            if remaining <= 0.0:
                self._count_timeout()
                raise OperationTimeout(
                    "operation budget is exhausted",
                    details={"name": self._name, "operation": scope.name},
                )
            budget = min(budget, remaining)
        return budget

    def _acquire_gate(self, deadline: float, *, context: str) -> bool:
        """Take the exclusivity gate; returns True when it must be released."""
        if self._is_op_owner():
            self._gate.acquire()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            self._count_timeout()
            raise OperationTimeout(
                "no time left to acquire exclusive access",
                details={"name": self._name, "context": context},
            )
        if not self._gate.acquire(timeout=remaining):
            self._count_timeout()
            raise OperationTimeout(
                "timed out waiting for exclusive access (another operation is running)",
                details={"name": self._name, "context": context, "timeout_s": remaining},
            )
        return True

    def _count_timeout(self) -> None:
        with self._stats_lock:
            self._timeouts += 1

    def _count_rejected(self) -> None:
        with self._stats_lock:
            self._rejected += 1

    def _record_call(self, duration: float, error: BaseException | None) -> None:
        with self._stats_lock:
            self._calls += 1
            self._last_duration = duration
            self._total_busy += duration
            if error is not None:
                self._failures += 1
                self._last_error = type(error).__name__ + ": " + str(error)

    def _worker_main(self) -> None:
        """Worker thread body: initializer, queue loop, finalizer."""
        with self._state_lock:
            self._worker_thread_id = threading.get_ident()
        init_ok = True
        try:
            if self._initializer is not None:
                self._initializer()
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            init_ok = False
            self._init_error = exc
        finally:
            self._init_done.set()
        if not init_ok:
            self._fail_pending(self._init_error)
            self._run_finalizer()
            return
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    break
                if not isinstance(item, _WorkItem):  # pragma: no cover - defensive
                    continue
                if not item.future.set_running_or_notify_cancel():
                    # The caller timed out before the worker picked this up.
                    continue
                started = time.perf_counter()
                error: BaseException | None = None
                try:
                    result = item.fn(*item.args, **item.kwargs)
                except BaseException as exc:  # noqa: BLE001 - propagated to the caller
                    error = exc
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
                finally:
                    self._record_call(time.perf_counter() - started, error)
        finally:
            self._run_finalizer()

    def _fail_pending(self, error: BaseException | None) -> None:
        """Fail every queued call after a failed initializer (callers never hang)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _WorkItem):
                item.future.set_exception(
                    BackendUnavailable(
                        "executor worker failed to initialise",
                        details={"name": self._name, "error": repr(error)},
                    )
                )

    def _run_finalizer(self) -> None:
        """Run the finalizer on the worker thread; record, never swallow, errors."""
        finalizer = self._finalizer
        if finalizer is None:
            return
        try:
            finalizer()
        except BaseException as exc:  # noqa: BLE001 - recorded in stats
            with self._state_lock:
                self._finalizer_error = exc
            with self._stats_lock:
                self._last_error = "finalizer " + type(exc).__name__ + ": " + str(exc)
