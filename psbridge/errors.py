"""Exception taxonomy for the bridge.

Design rule: an operation must not report success unless its effect was
verified by a read-back or an equivalent postcondition check. Every error
carries a stable machine-readable code and JSON-serialisable details so MCP
wrappers can surface structured failures instead of opaque strings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class BridgeError(Exception):
    """Base class for every bridge failure."""

    code = "BRIDGE_ERROR"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class ValidationError(BridgeError):
    """User input is malformed (identifiers, ranges, schema violations)."""

    code = "VALIDATION_ERROR"


class ConfigError(BridgeError):
    code = "CONFIG_ERROR"


class PathNotAllowed(ValidationError):
    """A filesystem path escaped the configured model/work root."""

    code = "PATH_NOT_ALLOWED"


class BackendUnavailable(BridgeError):
    """Plant Simulation / COM could not be reached (not installed, not running)."""

    code = "BACKEND_UNAVAILABLE"


class BackendError(BridgeError):
    """The backend rejected an operation or failed unexpectedly."""

    code = "BACKEND_ERROR"


class OperationTimeout(BridgeError):
    """A COM call or a whole operation exceeded its timeout budget."""

    code = "OPERATION_TIMEOUT"


class ExecutorClosed(BridgeError):
    code = "EXECUTOR_CLOSED"


class ObjectNotFound(BridgeError):
    code = "OBJECT_NOT_FOUND"


class ObjectExists(BridgeError):
    code = "OBJECT_EXISTS"


class TypeNotAllowed(BridgeError):
    """Object/attribute not in the allow-list (guards against arbitrary SimTalk)."""

    code = "TYPE_NOT_ALLOWED"


class PropertyNotApplicable(TypeNotAllowed):
    """Attribute exists in principle but is not writable on this object type.

    Derived from TypeNotAllowed so a caller that only distinguishes
    "refused" from "succeeded" keeps working, while a caller that cares can
    tell "this attribute does not exist anywhere" (TypeNotAllowed) from
    "this attribute exists, just not here" (this class).
    """

    code = "PROPERTY_NOT_APPLICABLE"


class VerificationFailed(BridgeError):
    """A write was applied but read-back did not match the intended value."""

    code = "VERIFICATION_FAILED"


class SimTalkError(BridgeError):
    """SimTalk compilation/runtime failure, including errors swallowed by COM."""

    code = "SIMTALK_ERROR"


class SimulationError(BridgeError):
    code = "SIMULATION_ERROR"


class ModelMutationDenied(BridgeError):
    """A mutation targeted an original research model instead of a clone."""

    code = "MODEL_MUTATION_DENIED"


class InstanceInvalid(ValidationError):
    code = "INSTANCE_INVALID"


class ExperimentInvalid(ValidationError):
    code = "EXPERIMENT_INVALID"


class RunInvalid(BridgeError):
    """A run finished but failed the validity gates; it must not enter results."""

    code = "RUN_INVALID"


class CrossCheckMismatch(RunInvalid):
    """Two independent implementations disagree on the same run."""

    code = "CROSS_CHECK_MISMATCH"


class EngineUnsupported(BridgeError):
    """The engine does not implement a requested capability (e.g. routing)."""

    code = "ENGINE_UNSUPPORTED"
