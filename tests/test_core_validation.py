"""Unit tests for the SimTalk-safe validation primitives."""

from __future__ import annotations

import pytest

from psbridge.core import validation
from psbridge.errors import TypeNotAllowed, ValidationError


# ---------------------------------------------------------------------------
# identifiers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    ["Station1", "S_1", "_private", "A" * validation.MAX_IDENTIFIER_LENGTH, "abcDEF_123"],
)
def test_valid_identifiers_are_returned_unchanged(value: str) -> None:
    assert validation.validate_identifier(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "1Station", "Sta tion", "Sta-tion", "Sta.tion", "Stä", 'Sta"tion', "Station;", "Station\n"],
)
def test_malformed_identifiers_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        validation.validate_identifier(value)


def test_identifier_rejects_non_strings_and_overlong_names() -> None:
    with pytest.raises(ValidationError):
        validation.validate_identifier(123)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validation.validate_identifier(None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validation.validate_identifier("A" * (validation.MAX_IDENTIFIER_LENGTH + 1))


# ---------------------------------------------------------------------------
# object paths
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [".Models.Model.S1", ".MaterialFlow.SingleProc", ".Models.Model.EventController"],
)
def test_valid_object_paths(value: str) -> None:
    assert validation.validate_object_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Models.Model",
        ".",
        ".Models..S1",
        ".Models.Model.S1.",
        ".Models.Model.S1; deleteObject",
        '.Models."S1"',
        ".Models.Model.S1\n",
        ".Models.Model.1S1",
        ".Models/Model/S1",
    ],
)
def test_malformed_object_paths_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        validation.validate_object_path(value)


def test_object_path_depth_is_bounded() -> None:
    too_deep = "." + ".".join(["N"] * (validation.MAX_PATH_COMPONENTS + 1))
    with pytest.raises(ValidationError):
        validation.validate_object_path(too_deep)


def test_join_object_path_uses_both_validators() -> None:
    assert validation.join_object_path(".Models.Model", "S1") == ".Models.Model.S1"
    with pytest.raises(ValidationError):
        validation.join_object_path(".Models.Model", "bad name")


def test_coordinates_must_be_finite_numbers() -> None:
    assert validation.validate_coordinate(2, field_name="x") == 2.0
    for bad in (True, "2", None, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            validation.validate_coordinate(bad, field_name="x")


# ---------------------------------------------------------------------------
# class allow-list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "short_name,class_path",
    [
        ("SingleProc", ".MaterialFlow.SingleProc"),
        ("Buffer", ".MaterialFlow.Buffer"),
        ("WorkerPool", ".Resources.WorkerPool"),
        ("Workplace", ".Resources.Workplace"),
    ],
)
def test_device_classes_resolve_from_short_name_and_path(short_name: str, class_path: str) -> None:
    assert validation.validate_class_name(short_name) == class_path
    assert validation.validate_class_name(class_path) == class_path
    assert validation.class_short_name(class_path) == short_name


@pytest.mark.parametrize("value", ["Evil", ".Models.Frame", ".ApplicationObjects.HBW3D.WMS", "", 42, None])
def test_classes_outside_the_allowlist_raise_type_not_allowed(value: object) -> None:
    with pytest.raises(TypeNotAllowed):
        validation.validate_class_name(value)


def test_auxiliary_classes_need_the_explicit_flag() -> None:
    with pytest.raises(TypeNotAllowed):
        validation.validate_class_name("Method")
    assert validation.validate_class_name("Method", allow_auxiliary=True) == ".InformationFlow.&Method"
    assert validation.validate_class_name("DataTable", allow_auxiliary=True) == ".InformationFlow.&TableFile"


def test_allowlist_is_a_defensive_copy() -> None:
    allowlist = validation.class_allowlist()
    allowlist["SingleProc"] = ".MaterialFlow.Evil"
    assert validation.validate_class_name("SingleProc") == ".MaterialFlow.SingleProc"


# ---------------------------------------------------------------------------
# attribute allow-list
# ---------------------------------------------------------------------------
def test_writable_attributes_are_per_class() -> None:
    assert "procTime" in validation.writable_attributes("SingleProc")
    assert "Capacity" in validation.writable_attributes("Buffer")
    assert "Capacity" not in validation.writable_attributes("SingleProc")
    assert "Interval" in validation.writable_attributes(".MaterialFlow.Source")
    # Measured: a Source has no processing time, so procTime is not offered.
    assert "procTime" not in validation.writable_attributes(".MaterialFlow.Source")


def test_attribute_applicability() -> None:
    assert validation.validate_attribute("Buffer", "Capacity") == "Capacity"
    assert validation.validate_attribute(".MaterialFlow.SingleProc", "procTime") == "procTime"
    assert validation.is_attribute_applicable("SingleProc", "procTime") is True
    assert validation.is_attribute_applicable("SingleProc", "Capacity") is False
    assert validation.is_attribute_applicable("NotAClass", "procTime") is False
    with pytest.raises(TypeNotAllowed):
        validation.validate_attribute("SingleProc", "Capacity")

    # Aliases keep old callers working but resolve to the object that exists.
    assert validation.validate_class_name("Station") == ".MaterialFlow.SingleProc"
    assert validation.validate_class_name("AssemblyStation") == ".MaterialFlow.Assembly"
    with pytest.raises(TypeNotAllowed):
        validation.validate_attribute("Station", "noSuchAttribute")
    with pytest.raises(ValidationError):
        validation.validate_attribute("Station", "bad-name")


def test_attribute_values_are_coerced_to_the_declared_kind() -> None:
    assert validation.validate_attribute_value("Station", "procTime", 2) == 2.0
    assert validation.validate_attribute_value("ParallelProc", "XDim", 3) == 3
    assert validation.validate_attribute_value("Source", "TimeOfGeneration", "Constant") == "Constant"
    with pytest.raises(ValidationError):
        validation.validate_attribute_value("Station", "procTime", -1.0)
    with pytest.raises(ValidationError):
        validation.validate_attribute_value("Station", "procTime", True)
    with pytest.raises(ValidationError):
        validation.validate_attribute_value("ParallelProc", "XDim", 2.0)
    with pytest.raises(ValidationError):
        validation.validate_attribute_value("ParallelProc", "XDim", 0)
    with pytest.raises(ValidationError):
        validation.validate_attribute_value("Source", "TimeOfGeneration", "Bogus")


def test_default_values_are_fresh_copies() -> None:
    first = validation.default_values("Source")
    first["Interval"] = 99.0
    assert validation.default_values("Source")["Interval"] == 60.0
    assert validation.default_values(".InformationFlow.&Method") == {}


def test_values_equal_is_tolerant_for_floats_but_strict_for_bools() -> None:
    assert validation.values_equal(1.0, 1.0 + 1e-12) is True
    assert validation.values_equal(1.0, 1.1) is False
    assert validation.values_equal(True, True) is True
    assert validation.values_equal(True, 1) is False
    assert validation.values_equal("a", "a") is True
    assert validation.values_equal(float("nan"), float("nan")) is False


# ---------------------------------------------------------------------------
# SimTalk literals
# ---------------------------------------------------------------------------
def test_simtalk_string_quoting_escapes_backslashes_and_quotes() -> None:
    assert validation.quote_simtalk_string("plain") == '"plain"'
    assert validation.quote_simtalk_string('a"b') == '"a\\"b"'
    assert validation.escape_simtalk_string("back\\slash") == "back\\\\slash"
    assert validation.quote_simtalk_string('C:\\tmp\\"x"') == '"C:\\\\tmp\\\\\\"x\\""'


@pytest.mark.parametrize("value", ["a\nb", "a\tb", "a\x00b", "a\x7fb", "\r"])
def test_simtalk_string_quoting_rejects_control_characters(value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        validation.quote_simtalk_string(value)
    assert "control characters" in str(excinfo.value)


def test_simtalk_string_quoting_rejects_non_strings_and_overlong_values() -> None:
    with pytest.raises(ValidationError):
        validation.quote_simtalk_string(1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validation.quote_simtalk_string("a" * (validation.MAX_STRING_LITERAL_LENGTH + 1))


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0:00:00"),
        (0.001, "0:00:00.001"),
        (1.5, "0:00:01.500"),
        (59.9996, "0:01:00"),
        (60, "0:01:00"),
        (3661.25, "1:01:01.250"),
        (3600, "1:00:00"),
        (90061.007, "25:01:01.007"),
    ],
)
def test_seconds_to_simtalk_time(seconds: float, expected: str) -> None:
    assert validation.seconds_to_simtalk_time(seconds) == expected


@pytest.mark.parametrize("seconds", [-1, -0.001, float("nan"), float("inf"), True, "1", None])
def test_seconds_to_simtalk_time_rejects_invalid_input(seconds: object) -> None:
    with pytest.raises(ValidationError):
        validation.seconds_to_simtalk_time(seconds)


def test_seconds_to_simtalk_time_is_bounded() -> None:
    with pytest.raises(ValidationError):
        validation.seconds_to_simtalk_time(validation.MAX_SIMTIME_SECONDS + 1.0)


def test_simtalk_source_validation() -> None:
    source = "var x : integer;\nx := 1;\n"
    assert validation.validate_simtalk_source(source) == source
    for bad in ("", "   ", "a\x00b", "a\x01b"):
        with pytest.raises(ValidationError):
            validation.validate_simtalk_source(bad)
    with pytest.raises(ValidationError):
        validation.validate_simtalk_source(5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DataTable bounds
# ---------------------------------------------------------------------------
def test_table_index_and_size_validation() -> None:
    # Plant Simulation DataTables are 1-based, so index 0 is always invalid.
    assert validation.validate_table_index(1, 3) == (1, 3)
    assert validation.validate_table_size(2, 3) == (2, 3)
    assert validation.table_cell_count(2, 3) == 6
    for bad in ((True, 1), (0, 1), (1, 0), (1, 1.5), (None, 1)):
        with pytest.raises(ValidationError):
            validation.validate_table_index(*bad)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        validation.validate_table_size(0, 1)
    with pytest.raises(ValidationError):
        validation.validate_table_size(2, 2, max_cells=3)


def test_table_cell_bounds_and_ranges() -> None:
    assert validation.validate_table_cell_bounds(1, 1, rows=2, columns=2) == (1, 1)
    assert validation.validate_table_cell_bounds(2, 2, rows=2, columns=2) == (2, 2)
    with pytest.raises(ValidationError):
        validation.validate_table_cell_bounds(3, 1, rows=2, columns=2)
    with pytest.raises(ValidationError):
        validation.validate_table_cell_bounds(0, 1, rows=2, columns=2)
    assert validation.validate_table_range(1, 1, 2, 2, rows=2, columns=2) == (1, 1, 2, 2)
    with pytest.raises(ValidationError):
        validation.validate_table_range(2, 2, 1, 1, rows=2, columns=2)
    with pytest.raises(ValidationError):
        validation.validate_table_range(1, 1, 6, 1, rows=2, columns=2)
    with pytest.raises(ValidationError):
        validation.validate_table_range(1, 1, 2, 2, rows=2, columns=2, max_cells=3)