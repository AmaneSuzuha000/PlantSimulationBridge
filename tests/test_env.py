"""Tests for the dispatch reinforcement-learning environment.

The tests use `ReferenceEngine`, a thin adapter that exposes the independent
scan-based oracle (psbridge.scheduling.reference.ReferenceStepper) through the
DispatchEngine protocol. No test here depends on psbridge.experiment.fake_engine,
so the environment is exercised against a second, independent implementation.
"""

from __future__ import annotations

import json
import math

import pytest

from psbridge.errors import ValidationError
from psbridge.experiment.engine import EngineState
from psbridge.scheduling.env import OBSERVATION_KEYS, READY_OPERATION_KEYS, DispatchEnv
from psbridge.scheduling.policies import get_policy
from psbridge.scheduling.reference import ReferenceStepper
from psbridge.schema import Instance, Job, Machine, Operation, ScheduledOperation


class ReferenceEngine:
    """DispatchEngine adapter over the reference stepper (test double, no semantics)."""

    kind = "reference"

    def __init__(self) -> None:
        self._stepper: ReferenceStepper | None = None
        self._truncated = False

    def reset(self, instance: Instance, seed: int, *, horizon: float | None = None) -> EngineState:
        self._stepper = ReferenceStepper(instance, horizon=horizon)
        return self.current_state()

    def _require(self) -> ReferenceStepper:
        if self._stepper is None:
            raise RuntimeError("engine not reset")
        return self._stepper

    def next_decision(self):
        return self._require().next_decision()

    def apply(self, op_id: str, machine_id: str | None = None) -> EngineState:
        stepper = self._require()
        scheduled = stepper.apply(op_id, machine_id)
        if scheduled is None:
            self._truncated = True
        return self.current_state()

    def is_done(self) -> bool:
        stepper = self._require()
        return stepper.truncated or stepper.completed_operations >= stepper.total_operations

    def current_state(self) -> EngineState:
        stepper = self._require()
        schedule = stepper.schedule()
        return EngineState(
            time=stepper.current_time,
            completed_operations=stepper.completed_operations,
            total_operations=stepper.total_operations,
            schedule=schedule,
            machine_free_at=stepper.machine_free_at(),
        )

    def schedule(self) -> tuple[ScheduledOperation, ...]:
        return self._require().schedule()

    def state_hash(self) -> str:
        return self._require().fingerprint()

    def event_log(self):
        return self._require().event_log()


def op(op_id: str, job_id: str, index: int, machine: str, duration: float) -> Operation:
    return Operation(op_id=op_id, job_id=job_id, index=index, candidates=(machine,), duration=duration)


def make_instance() -> Instance:
    """Two jobs on one machine: J1 (5) and J2 (2), both released at 0."""
    return Instance(
        instance_id="env_small",
        jobs=(
            Job("J1", (op("J1_O1", "J1", 0, "M1", 5.0),), release_time=0.0, due_date=20.0),
            Job("J2", (op("J2_O1", "J2", 0, "M1", 2.0),), release_time=0.0, due_date=1.0),
        ),
        machines=(Machine("M1"),),
    )


def make_two_machine_instance() -> Instance:
    return Instance(
        instance_id="env_two",
        jobs=(
            Job("J1", (op("J1_O1", "J1", 0, "M1", 1.0), op("J1_O2", "J1", 1, "M2", 2.0)), release_time=0.0),
            Job("J2", (op("J2_O1", "J2", 0, "M2", 3.0), op("J2_O2", "J2", 1, "M1", 1.0)), release_time=0.0),
        ),
        machines=(Machine("M1"), Machine("M2")),
    )


# -- reset / observation -----------------------------------------------------
def test_reset_returns_json_observable_dict() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    observation = env.reset()
    assert isinstance(observation, dict)
    assert set(observation) == set(OBSERVATION_KEYS)
    assert observation["time"] == 0.0
    assert observation["machine_id"] == "M1"
    assert observation["completed_operations"] == 0
    assert observation["total_operations"] == 2
    json.dumps(observation)  # must be fully JSON-serialisable


def test_observation_ready_operations_have_documented_keys() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    observation = env.reset()
    assert observation["ready_operations"]
    for item in observation["ready_operations"]:
        assert set(item) == set(READY_OPERATION_KEYS)
        json.dumps(item)
    assert {item["op_id"] for item in observation["ready_operations"]} == {"J1_O1", "J2_O1"}


def test_machine_free_at_is_reported_for_every_machine() -> None:
    env = DispatchEnv(ReferenceEngine(), make_two_machine_instance())
    observation = env.reset()
    assert observation["machine_free_at"] == {"M1": 0.0, "M2": 0.0}


def test_reset_is_repeatable_and_deterministic() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    first = env.reset(seed=7)
    second = env.reset(seed=7)
    assert first == second
    assert env.seed == 7
    assert env.steps == 0


def test_reset_rejects_bad_seed() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    with pytest.raises(ValidationError):
        env.reset(seed=-1)


# -- masks -------------------------------------------------------------------
def test_action_mask_matches_available_actions() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    actions = env.available_actions()
    assert actions == ("J1_O1", "J2_O1")
    assert env.action_mask() == (True, True)
    assert len(env.action_mask()) == len(actions)


def test_action_index_maps_actions() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    assert env.action_index("J2_O1") == 1
    with pytest.raises(ValidationError):
        env.action_index("J1_O1_unknown")


def test_available_actions_empty_before_reset_and_after_termination() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    assert env.available_actions() == ()
    assert env.action_mask() == ()
    env.reset()
    env.step("J1_O1")
    env.step("J2_O1")
    assert env.terminated is True
    assert env.available_actions() == ()
    assert env.action_mask() == ()


# -- stepping ----------------------------------------------------------------
def test_step_returns_five_tuple_with_documented_info() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    observation, reward, terminated, truncated, info = env.step("J1_O1")
    assert reward == 0.0
    assert terminated is False
    assert truncated is False
    assert info["time"] == 0.0
    assert info["decision_index"] == 0
    assert info["op_id"] == "J1_O1"
    assert info["machine_id"] == "M1"
    assert info["start"] == 0.0
    assert info["end"] == 5.0
    assert info["completed_operations"] == 1
    assert "metrics" not in info  # only on termination
    assert observation["completed_operations"] == 1
    assert observation["machine_id"] == "M1"


def test_default_reward_is_zero_every_step() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    rewards = []
    while True:
        actions = env.available_actions()
        _, reward, terminated, truncated, _ = env.step(actions[0])
        rewards.append(reward)
        if terminated or truncated:
            break
    assert rewards == [0.0, 0.0]


def test_termination_carries_final_time_and_metrics_slot() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    env.step("J1_O1")
    observation, _, terminated, truncated, info = env.step("J2_O1")
    assert terminated is True and truncated is False
    assert info["completed_operations"] == 2
    assert info["final_time"] == 7.0
    assert info["complete"] is True
    assert observation["progress"] == 1.0
    # metrics come from the experiment layer; either a dict or an explicit reason
    assert "metrics" in info
    if info["metrics"] is None:
        assert isinstance(info["metrics_error"], str) and info["metrics_error"]
    else:
        json.dumps(info["metrics"])


def test_episode_order_follows_the_agents_choice() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    env.step("J2_O1")  # short job first
    _, _, terminated, _, info = env.step("J1_O1")
    assert terminated is True
    assert info["final_time"] == 7.0
    schedule = env.schedule()
    assert [(item.op_id, item.start) for item in schedule] == [("J2_O1", 0.0), ("J1_O1", 2.0)]


def test_suggested_operation_uses_the_configured_policy() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance(), policy=get_policy("spt"))
    env.reset()
    assert env.suggested_operation() == "J2_O1"  # duration 2 < 5
    edd_env = DispatchEnv(ReferenceEngine(), make_instance(), policy=get_policy("edd"))
    edd_env.reset()
    assert edd_env.suggested_operation() == "J2_O1"  # due 1 < 20


def test_policy_never_forces_the_action() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance(), policy=get_policy("spt"))
    env.reset()
    observation, _, _, _, _ = env.step("J1_O1")  # deliberately not the SPT choice
    assert observation["completed_operations"] == 1


# -- illegal actions ---------------------------------------------------------
@pytest.mark.parametrize("bad", ["NOPE", "", "j1_o1", "J1_O1 ", "J2_O2"])
def test_illegal_action_raises_validation_error(bad: str) -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    before = env.steps
    with pytest.raises(ValidationError) as excinfo:
        env.step(bad)
    assert excinfo.value.code == "VALIDATION_ERROR"
    assert "available_actions" in excinfo.value.details
    assert env.steps == before  # nothing was applied


def test_step_without_reset_is_rejected() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    with pytest.raises(ValidationError):
        env.step("J1_O1")


def test_step_after_termination_is_rejected() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    env.step("J1_O1")
    env.step("J2_O1")
    with pytest.raises(ValidationError):
        env.step("J1_O1")


def test_non_string_action_rejected() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance())
    env.reset()
    with pytest.raises(ValidationError):
        env.step(42)  # type: ignore[arg-type]


def test_action_from_another_machine_is_rejected() -> None:
    """Actions are machine-scoped: the M2 decision cannot take the M1 op."""
    env = DispatchEnv(ReferenceEngine(), make_two_machine_instance())
    env.reset()
    assert env.observation["machine_id"] == "M1"
    legal = env.available_actions()
    assert legal == ("J1_O1",)  # J2_O1 runs on M2
    with pytest.raises(ValidationError):
        env.step("J2_O1")


# -- max_steps / reward_fn / horizon ----------------------------------------
def test_max_steps_truncates_instead_of_terminating() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance(), max_steps=1)
    env.reset()
    _, _, terminated, truncated, info = env.step("J1_O1")
    assert terminated is False
    assert truncated is True
    assert info["truncated"] is True
    assert info["completed_operations"] == 1
    with pytest.raises(ValidationError):
        env.step("J2_O1")


def test_max_steps_equal_to_total_still_terminates() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance(), max_steps=2)
    env.reset()
    env.step("J1_O1")
    _, _, terminated, truncated, _ = env.step("J2_O1")
    assert terminated is True
    assert truncated is False


def test_horizon_stop_is_reported_as_truncated() -> None:
    instance = Instance(
        instance_id="env_horizon",
        jobs=(
            Job("J1", (op("J1_O1", "J1", 0, "M1", 5.0), op("J1_O2", "J1", 1, "M1", 5.0)), release_time=0.0),
            Job("J2", (op("J2_O1", "J2", 0, "M1", 9.0),), release_time=6.0),
        ),
        machines=(Machine("M1"),),
    )
    env = DispatchEnv(ReferenceEngine(), instance, horizon=8.0)
    env.reset()
    env.step("J1_O1")
    # at t=5 J1_O2 (needs 5 more -> 10 > 8) and J2_O1 is ready from 6? no: 5 < 6
    # so the engine picks J1_O2 and refuses to start it -> truncated
    observation, _, terminated, truncated, info = env.step("J1_O2")
    assert terminated is False
    assert truncated is True
    assert info["start"] is None and info["end"] is None
    assert observation["truncated"] is True


def test_reward_fn_receives_transition_and_can_shape() -> None:
    seen = []

    def reward_fn(transition):
        seen.append(transition)
        return -float(transition["duration"])

    env = DispatchEnv(ReferenceEngine(), make_instance(), reward_fn=reward_fn)
    env.reset()
    _, reward, _, _, _ = env.step("J2_O1")
    assert reward == -2.0
    assert seen[0]["op_id"] == "J2_O1"
    assert seen[0]["start"] == 0.0 and seen[0]["end"] == 2.0
    assert {"step", "time_before", "time", "machine_id", "index", "completed_before", "completed_after"} <= set(seen[0])


def test_reward_fn_returning_non_finite_value_is_rejected() -> None:
    env = DispatchEnv(ReferenceEngine(), make_instance(), reward_fn=lambda transition: math.inf)
    env.reset()
    with pytest.raises(ValidationError):
        env.step("J1_O1")


def test_reward_fn_can_use_makespan_proxy() -> None:
    def reward_fn(transition):
        return 1.0 if transition["completed_after"] == transition["total_operations"] else 0.0

    env = DispatchEnv(ReferenceEngine(), make_instance(), reward_fn=reward_fn)
    env.reset()
    assert env.step("J1_O1")[1] == 0.0
    assert env.step("J2_O1")[1] == 1.0


# -- constructor validation --------------------------------------------------
def test_object_without_engine_methods_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DispatchEnv(object(), make_instance())  # type: ignore[arg-type]
    assert "missing_methods" in excinfo.value.details


def test_non_instance_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DispatchEnv(ReferenceEngine(), {"instance_id": "x"})  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -3, 1.5, True])
def test_bad_max_steps_rejected(bad) -> None:
    with pytest.raises(ValidationError):
        DispatchEnv(ReferenceEngine(), make_instance(), max_steps=bad)


def test_bad_reward_fn_rejected() -> None:
    with pytest.raises(ValidationError):
        DispatchEnv(ReferenceEngine(), make_instance(), reward_fn="nope")  # type: ignore[arg-type]


def test_engine_that_returns_no_decision_marks_truncated() -> None:
    class DeadEngine(ReferenceEngine):
        def reset(self, instance, seed, *, horizon=None):
            super().reset(instance, seed, horizon=horizon)
            self._stepper = ReferenceStepper(instance, horizon=0.5)
            return self.current_state()

    instance = Instance(
        instance_id="env_dead",
        jobs=(Job("J1", (op("J1_O1", "J1", 0, "M1", 5.0),), release_time=0.0),),
        machines=(Machine("M1"),),
    )
    env = DispatchEnv(DeadEngine(), instance)
    observation = env.reset()
    assert observation["machine_id"] == "M1"  # decision exists before the horizon bites
    _, _, terminated, truncated, _ = env.step("J1_O1")
    assert truncated is True
    assert terminated is False
