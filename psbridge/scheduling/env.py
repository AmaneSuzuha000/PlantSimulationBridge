"""Gym-style dispatch environment over a :class:`DispatchEngine`.

The environment is the glue an RL agent needs and nothing more:

* `reset(seed=None)` returns an observation of the first decision point,
* `step(op_id)` applies one ready operation and advances the engine,
* `action_mask()` / `available_actions()` expose the legal choices,
* an illegal action raises :class:`~psbridge.errors.ValidationError`.

The environment holds *no* scheduling semantics of its own: every time,
decision and precedence rule comes from the injected engine (the
`DispatchEngine` protocol). That is what keeps the RL track and the
experiment track comparable.

Reward
------
The default reward is `0.0` on every step. A study that wants a shaped
signal passes `reward_fn`: a callable that receives one transition dict and
returns a finite float. The transition dict is documented in
:meth:`DispatchEnv._reward` and is deliberately plain data (no objects
that would leak engine internals into a reward function).

Action masking note: decision points are machine-major (S4/S5), so a legal
action is an operation that is ready *for the machine of the current decision
point*, not every globally ready operation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from psbridge.errors import ValidationError
from psbridge.experiment.engine import DecisionPoint, DispatchEngine, EngineState
from psbridge.scheduling.policy import DispatchPolicy, select_operation
from psbridge.schema import Instance, validate_number

__all__ = ["DispatchEnv"]

_ENGINE_METHODS = ("reset", "next_decision", "apply", "is_done", "current_state", "schedule", "event_log")

#: Keys every observation carries (checked by the test suite so that an agent
#: cannot silently depend on a key that later disappears).
OBSERVATION_KEYS = (
    "time",
    "machine_id",
    "decision_index",
    "ready_operations",
    "machine_free_at",
    "completed_operations",
    "total_operations",
    "progress",
    "terminated",
    "truncated",
)

READY_OPERATION_KEYS = (
    "op_id",
    "job_id",
    "index",
    "duration",
    "candidates",
    "ready_since",
    "job_release",
    "due_date",
    "effective_due",
    "weight",
)


class DispatchEnv:
    """Deterministic dispatch environment.

    Parameters
    ----------
    engine:
        Any object satisfying :class:`psbridge.experiment.engine.DispatchEngine`.
    instance:
        The instance the engine will be reset with. It is stored, not copied.
    policy:
        Optional :class:`~psbridge.scheduling.policy.DispatchPolicy` used only by
        :meth:`suggested_operation` (a baseline/teacher action). It never
        constrains or auto-applies anything.
    max_steps:
        Optional cap on the number of applied operations. Hitting it ends the
        episode with `truncated=True` (never `terminated=True`).
    reward_fn:
        Optional callable `transition -> float`. See :meth:`_reward`.
    horizon:
        Optional pass-through horizon for `engine.reset(...)` (rule S7).
    """

    def __init__(
        self,
        engine: DispatchEngine,
        instance: Instance,
        policy: DispatchPolicy | None = None,
        max_steps: int | None = None,
        reward_fn: Callable[[Mapping[str, Any]], float] | None = None,
        *,
        horizon: float | None = None,
    ) -> None:
        missing = [name for name in _ENGINE_METHODS if not callable(getattr(engine, name, None))]
        if missing:
            raise ValidationError(
                "engine does not satisfy the DispatchEngine protocol",
                details={"missing_methods": missing, "type": type(engine).__name__},
            )
        if not isinstance(instance, Instance):
            raise ValidationError(
                "instance must be a psbridge.schema.Instance",
                details={"type": type(instance).__name__},
            )
        if policy is not None and (
            not isinstance(getattr(policy, "name", None), str) or not callable(getattr(policy, "preference", None))
        ):
            raise ValidationError("policy must expose 'name' and 'preference'", details={"type": type(policy).__name__})
        if max_steps is not None and (isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1):
            raise ValidationError("max_steps must be a positive integer or None", details={"max_steps": repr(max_steps)})
        if reward_fn is not None and not callable(reward_fn):
            raise ValidationError("reward_fn must be callable or None", details={"type": type(reward_fn).__name__})
        if horizon is not None:
            horizon = validate_number(horizon, field_name="horizon", strictly_positive=True)

        self._engine = engine
        self._instance = instance
        self._policy = policy
        self._max_steps = max_steps
        self._reward_fn = reward_fn
        self._horizon = horizon
        self._started = False
        self._steps = 0
        self._terminated = False
        self._truncated = False
        self._seed = 0
        self._decision: DecisionPoint | None = None
        self._observation: dict[str, Any] = self._empty_observation()
        self._last_transition: dict[str, Any] | None = None

    # -- introspection ---------------------------------------------------
    @property
    def engine(self) -> DispatchEngine:
        return self._engine

    @property
    def instance(self) -> Instance:
        return self._instance

    @property
    def policy(self) -> DispatchPolicy | None:
        return self._policy

    @property
    def max_steps(self) -> int | None:
        return self._max_steps

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def terminated(self) -> bool:
        return self._terminated

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def observation(self) -> dict[str, Any]:
        return self._observation

    @property
    def decision(self) -> DecisionPoint | None:
        return self._decision

    def current_state(self) -> EngineState:
        return self._engine.current_state()

    def schedule(self) -> tuple[Any, ...]:
        return tuple(self._engine.schedule())

    def state_hash(self) -> str:
        return str(self._engine.state_hash())

    # -- episode control -------------------------------------------------
    def reset(self, seed: int | None = None) -> dict[str, Any]:
        """Reset the engine and return the first observation."""
        if seed is None:
            seed_used = 0
        else:
            if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                raise ValidationError("seed must be a non-negative integer or None", details={"seed": repr(seed)})
            seed_used = seed
        self._seed = seed_used
        self._engine.reset(self._instance, seed_used, horizon=self._horizon)
        self._started = True
        self._steps = 0
        self._terminated = False
        self._truncated = False
        self._last_transition = None
        self._decision = self._engine.next_decision()
        if self._decision is None:
            state = self._engine.current_state()
            if state.completed_operations >= state.total_operations:
                self._terminated = True
            else:
                self._truncated = True
        self._observation = self._build_observation()
        return self._observation

    def available_actions(self) -> tuple[str, ...]:
        """Legal op ids at the current decision point (empty once the episode ends)."""
        if not self._started or self._decision is None:
            return ()
        return self._decision.action_ids

    def action_mask(self) -> tuple[bool, ...]:
        """Boolean mask aligned with :meth:`available_actions`."""
        return tuple(True for _ in self.available_actions())

    def action_index(self, op_id: str) -> int:
        """Position of `op_id` in :meth:`available_actions`; ValidationError if illegal."""
        actions = self.available_actions()
        try:
            return actions.index(op_id)
        except ValueError:
            raise ValidationError(
                "operation is not legal at the current decision point",
                details={"op_id": op_id, "available_actions": list(actions)},
            ) from None

    def suggested_operation(self) -> str | None:
        """The configured policy's choice at the current decision (None if unavailable)."""
        if self._policy is None or self._decision is None:
            return None
        return select_operation(self._decision, self._policy).op_id

    # -- stepping --------------------------------------------------------
    def step(self, op_id: str) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        """Apply one operation and return `(observation, reward, terminated, truncated, info)`."""
        if not self._started:
            raise ValidationError("call reset() before step()")
        if self._terminated or self._truncated:
            raise ValidationError(
                "episode is already over; call reset()",
                details={"steps": self._steps, "terminated": self._terminated, "truncated": self._truncated},
            )
        decision = self._decision
        if decision is None:
            raise ValidationError("no decision point available; call reset()")
        if not isinstance(op_id, str):
            raise ValidationError("op_id must be a string", details={"op_id": repr(op_id)})
        if op_id not in decision.action_ids:
            raise ValidationError(
                "operation is not legal at the current decision point",
                details={"op_id": op_id, "available_actions": list(decision.action_ids), "machine_id": decision.machine_id},
            )
        before = self._engine.current_state()
        after = self._engine.apply(op_id, decision.machine_id)
        self._steps += 1
        scheduled = self._find_scheduled(after.schedule, op_id)
        transition = {
            "step": self._steps,
            "time_before": before.time,
            "time": after.time,
            "machine_id": decision.machine_id,
            "op_id": op_id,
            "index": decision.decision_index,
            "start": None if scheduled is None else scheduled.start,
            "end": None if scheduled is None else scheduled.end,
            "duration": None if scheduled is None else scheduled.duration,
            "completed_before": before.completed_operations,
            "completed_after": after.completed_operations,
            "total_operations": decision.total_operations,
        }
        reward = self._reward(transition)

        engine_done = self._engine.is_done()
        complete = after.completed_operations >= decision.total_operations
        if engine_done and complete:
            self._terminated, self._truncated = True, False
            self._decision = None
        elif engine_done:
            # The engine stopped without scheduling everything: horizon (S7).
            self._terminated, self._truncated = False, True
            self._decision = None
        elif self._max_steps is not None and self._steps >= self._max_steps:
            self._terminated, self._truncated = False, True
            self._decision = None
        else:
            self._decision = self._engine.next_decision()
            if self._decision is None:
                # The engine stopped without finishing: horizon truncation (S7).
                self._truncated = True
                self._decision = None
        self._last_transition = transition
        self._observation = self._build_observation()

        info: dict[str, Any] = {
            "time": after.time,
            "decision_index": decision.decision_index,
            "next_decision_index": None if self._decision is None else self._decision.decision_index,
            "step": self._steps,
            "op_id": op_id,
            "machine_id": decision.machine_id,
            "start": transition["start"],
            "end": transition["end"],
            "completed_operations": after.completed_operations,
            "total_operations": decision.total_operations,
            "terminated": self._terminated,
            "truncated": self._truncated,
            "schedule_length": len(after.schedule),
            "state_hash": self._engine.state_hash(),
        }
        if self._terminated:
            info["final_time"] = self._final_time()
            metrics, error = self._final_metrics()
            info["metrics"] = metrics
            info["metrics_error"] = error
            info["complete"] = after.completed_operations == decision.total_operations
        return self._observation, reward, self._terminated, self._truncated, info

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _find_scheduled(schedule: Sequence[Any], op_id: str) -> Any | None:
        for item in reversed(schedule):
            if getattr(item, "op_id", None) == op_id:
                return item
        return None

    def _final_time(self) -> float:
        schedule = self._engine.schedule()
        if not schedule:
            return float(self._engine.current_state().time)
        return float(max(item.end for item in schedule))

    def _final_metrics(self) -> tuple[dict[str, Any] | None, str | None]:
        """Compute the standard metrics for a finished episode.

        The metric formulas live in `psbridge.experiment.metrics` (owned by the
        experiment layer). Imported lazily so that the environment works even
        before/without it; a metrics failure is reported, never swallowed.
        """
        try:
            from psbridge.experiment.metrics import compute_metrics
        except ImportError as exc:  # pragma: no cover - depends on sibling module
            return None, "metrics unavailable: " + str(exc)
        try:
            metrics = compute_metrics(self._instance, tuple(self._engine.schedule()), horizon=self._horizon)
        except Exception as exc:  # noqa: BLE001 - reported via info, not hidden
            return None, "metrics failed: " + type(exc).__name__ + ": " + str(exc)
        to_dict = getattr(metrics, "to_dict", None)
        return (to_dict() if callable(to_dict) else metrics), None

    def _reward(self, transition: Mapping[str, Any]) -> float:
        """Default 0.0; a custom `reward_fn` receives exactly `transition`.

        The transition dict keys are: step, time_before, time, machine_id, op_id,
        index (decision index), start, end, duration, completed_before,
        completed_after, total_operations. Values are plain numbers/strings/None;
        start/end/duration are None if the engine refused to start the operation
        (horizon truncation).
        """
        if self._reward_fn is None:
            return 0.0
        value = self._reward_fn(dict(transition))
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValidationError(
                "reward_fn must return a finite number",
                details={"reward": repr(value), "step": transition.get("step")},
            )
        return float(value)

    def _empty_observation(self) -> dict[str, Any]:
        return {
            "time": 0.0,
            "machine_id": None,
            "decision_index": None,
            "ready_operations": [],
            "machine_free_at": {},
            "completed_operations": 0,
            "total_operations": self._instance.total_operations,
            "progress": 0.0,
            "terminated": False,
            "truncated": False,
        }

    def _build_observation(self) -> dict[str, Any]:
        state = self._engine.current_state()
        decision = self._decision
        ready: list[dict[str, Any]] = []
        if decision is not None:
            for operation in decision.ready_operations:
                ready.append(
                    {
                        "op_id": operation.op_id,
                        "job_id": operation.job_id,
                        "index": operation.index,
                        "duration": float(operation.duration),
                        "candidates": list(operation.candidates),
                        "ready_since": float(operation.ready_since),
                        "job_release": float(operation.job_release),
                        "due_date": None if operation.due_date is None else float(operation.due_date),
                        "effective_due": None if operation.due_date is None else float(operation.due_date),
                        "weight": float(operation.weight),
                    }
                )
        machine_free_at = {
            machine_id: float(value) for machine_id, value in sorted(dict(state.machine_free_at).items())
        }
        total = max(int(state.total_operations), 1)
        return {
            "time": float(state.time),
            "machine_id": None if decision is None else decision.machine_id,
            "decision_index": None if decision is None else decision.decision_index,
            "ready_operations": ready,
            "machine_free_at": machine_free_at,
            "completed_operations": int(state.completed_operations),
            "total_operations": int(state.total_operations),
            "progress": float(state.completed_operations) / total,
            "terminated": self._terminated,
            "truncated": self._truncated,
        }
