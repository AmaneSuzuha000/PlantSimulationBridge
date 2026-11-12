"""Regression tests for structured backend and validation failure semantics."""
from __future__ import annotations

from pathlib import Path

import pytest

from psbridge.com.fake import FakeBackend
from psbridge.core import validation
from psbridge.core.ops import SafeOps
from psbridge.errors import PropertyNotApplicable, TypeNotAllowed, ValidationError


def test_unknown_attribute_and_inapplicable_attribute_are_distinct() -> None:
    """A name that exists nowhere is a different mistake from a name that
    exists but not on this class. The old code called both "not writable",
    which sent the caller looking for a spelling error that was not there."""
    with pytest.raises(TypeNotAllowed) as nowhere:
        validation.validate_attribute("SingleProc", "NotAnAttribute")
    assert not isinstance(nowhere.value, PropertyNotApplicable)
    assert "allow-list" in str(nowhere.value)

    with pytest.raises(PropertyNotApplicable) as elsewhere:
        validation.validate_attribute("SingleProc", "Capacity")
    assert "not writable on this object class" in str(elsewhere.value)


def test_inapplicable_attribute_still_caught_as_TypeNotAllowed() -> None:
    """Old callers catch TypeNotAllowed; the split must not break them."""
    assert issubclass(PropertyNotApplicable, TypeNotAllowed)


def test_the_default_frame_comes_from_the_loaded_model(tmp_path: Path) -> None:
    """The constant .Models.Model only exists for a model literally called
    Model, so every bare name failed as "unknown object" on a real model."""
    backend = FakeBackend()
    backend.connect()
    ops = SafeOps(backend)
    assert ops._frame(None) == ".Models.Model"
    assert backend.default_frame_path == ".Models.Model"


def test_explicit_frame_still_wins(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.connect()
    ops = SafeOps(backend)
    assert ops._frame(".Models.Other.Frame") == ".Models.Other.Frame"


def test_source_configuration_rejects_a_non_positive_interval() -> None:
    """Measured: zero interval reached SimTalk and surfaced as a bare
    "execution failed". It is a caller mistake and should read as one."""
    backend = FakeBackend()
    backend.connect()
    ops = SafeOps(backend)
    for interval in (0, 0.0, -1.0):
        with pytest.raises(ValidationError) as exc:
            ops.configure_source(".Models.Model.Src", units=1, interval=interval)
        assert "interval" in str(exc.value)


def test_source_configuration_rejects_non_positive_units() -> None:
    backend = FakeBackend()
    backend.connect()
    ops = SafeOps(backend)
    for units in (0, -3):
        with pytest.raises(ValidationError) as exc:
            ops.configure_source(".Models.Model.Src", units=units, interval=1.0)
        assert "units" in str(exc.value)
    with pytest.raises(ValidationError):
        ops.configure_source(".Models.Model.Src", units=True, interval=1.0)


def test_source_configuration_rejects_a_negative_start() -> None:
    backend = FakeBackend()
    backend.connect()
    ops = SafeOps(backend)
    with pytest.raises(ValidationError) as exc:
        ops.configure_source(".Models.Model.Src", units=1, interval=1.0, start=-0.5)
    assert "start" in str(exc.value)