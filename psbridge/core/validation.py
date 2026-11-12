"""SimTalk-safe validation primitives: identifiers, paths, allow-lists, literals.

Every string that can end up inside a generated SimTalk statement is validated
here *before* it is interpolated. The module never "repairs" unsafe input: a
value is either returned in normalised form or a typed error is raised
(``ValidationError`` for malformed input, ``TypeNotAllowed`` for input that is
well-formed but outside the allow-list).

Two allow-lists define the safety boundary of the bridge:

* object classes - only ``.MaterialFlow.*`` devices and ``.Resources.*``
  resources may be created (plus the auxiliary ``Method``/``DataTable``/
  ``Frame`` classes the bridge needs for its own bookkeeping),
* writable attributes - a per-class allow-list with a declared value kind
  (``time``/``int``/``float``/``string``/``bool``), so a write can never hit an
  attribute that does not exist on that class, and a value can never be
  coerced into the wrong SimTalk type.

The class paths, attribute allow-lists, and SimTalk quoting rules encoded here
are verified against Siemens Tecnomatix Plant Simulation 13.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psbridge.errors import PropertyNotApplicable, TypeNotAllowed, ValidationError

#: Longest accepted identifier (SimTalk object/attribute names).
MAX_IDENTIFIER_LENGTH = 64
#: Longest accepted object path, counted in dot-separated components.
MAX_PATH_COMPONENTS = 16
#: Longest accepted string literal body (characters, before quoting).
MAX_STRING_LITERAL_LENGTH = 8192
#: Longest accepted raw SimTalk source block.
MAX_SIMTALK_SOURCE_LENGTH = 100_000
#: Largest accepted simulation time, in seconds (10 years).
MAX_SIMTIME_SECONDS = 315_360_000.0

#: \Z (not $) so a trailing newline can never sneak into an identifier.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
#: Characters that cannot be represented safely in a SimTalk string literal.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
#: Control characters that are legal inside a multi-line SimTalk source block.
_ALLOWED_SOURCE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

TIME = "time"
INT = "int"
FLOAT = "float"
STRING = "string"
BOOL = "bool"
ATTRIBUTE_KINDS = (TIME, INT, FLOAT, STRING, BOOL)


@dataclass(frozen=True)
class AttributeSpec:
    """Declared type of one writable attribute of one object class."""

    name: str
    kind: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


def _time(name: str, default: float = 0.0) -> AttributeSpec:
    return AttributeSpec(name, TIME, float(default), minimum=0.0)


def _count(name: str, default: int = 1, minimum: int = 1) -> AttributeSpec:
    return AttributeSpec(name, INT, int(default), minimum=float(minimum))


def _measure(name: str, default: float = 1.0, minimum: float = 0.0) -> AttributeSpec:
    return AttributeSpec(name, FLOAT, float(default), minimum=minimum)


def _text(name: str, default: str = "", choices: tuple[str, ...] = ()) -> AttributeSpec:
    return AttributeSpec(name, STRING, default, choices=choices)


#: Device/resource classes the bridge may create: short name -> SimTalk path.
#: Verified creatable against Plant Simulation 13 via scripts/verify_contract.py.
DEVICE_CLASS_ALLOWLIST: Mapping[str, str] = {
    "Source": ".MaterialFlow.Source",
    "SingleProc": ".MaterialFlow.SingleProc",
    "ParallelProc": ".MaterialFlow.ParallelProc",
    "Assembly": ".MaterialFlow.Assembly",
    "Buffer": ".MaterialFlow.Buffer",
    "PlaceBuffer": ".MaterialFlow.PlaceBuffer",
    "Sorter": ".MaterialFlow.Sorter",
    "Cycle": ".MaterialFlow.Cycle",
    "Track": ".MaterialFlow.Track",
    "TwoLaneTrack": ".MaterialFlow.TwoLaneTrack",
    "Turntable": ".MaterialFlow.Turntable",
    "Store": ".MaterialFlow.Store",
    "Line": ".MaterialFlow.Line",
    "DismantleStation": ".MaterialFlow.DismantleStation",
    "Interface": ".MaterialFlow.Interface",
    "Drain": ".MaterialFlow.Drain",
    "WorkerPool": ".Resources.WorkerPool",
    "Workplace": ".Resources.Workplace",
}

#: Old short names that pointed at SimTalk paths this build does not have.
#: They stay accepted so existing callers keep working, but resolve to the
#: object that actually fills that role. Measured, not guessed: Station,
#: ParallelStation, AssemblyStation, Conveyor and Worker all failed
#: createObject on 13.0.5 while the targets below all succeeded.
DEPRECATED_CLASS_ALIASES: Mapping[str, str] = {
    "Station": ".MaterialFlow.SingleProc",
    "ParallelStation": ".MaterialFlow.ParallelProc",
    "AssemblyStation": ".MaterialFlow.Assembly",
    "Conveyor": ".MaterialFlow.Track",
    "Worker": ".Resources.WorkerPool",
}

#: Non-device classes the bridge itself needs (methods, tables, variables).
#: Corrected from the .Models.* paths, which do not exist on this build:
#: information-flow objects live under .InformationFlow.& (with an ampersand).
AUXILIARY_CLASS_ALLOWLIST: Mapping[str, str] = {
    "Method": ".InformationFlow.&Method",
    "TableFile": ".InformationFlow.&TableFile",
    "DataTable": ".InformationFlow.&TableFile",
    "CardFile": ".InformationFlow.&CardFile",
    "Variable": ".InformationFlow.&Variable",
    "Comment": ".InformationFlow.&Comment",
    "FileLink": ".InformationFlow.&FileLink",
    "ShiftCalendar": ".InformationFlow.&ShiftCalendar",
    "AttributeExplorer": ".InformationFlow.&AttributeExplorer",
    "Generator": ".InformationFlow.&Generator",
    "Chart": ".UserInterface.Chart",
}

#: Per-class writable attributes. Anything absent here is not writable through
#: the bridge, even if SimTalk would accept the assignment.
CLASS_ATTRIBUTE_SPECS: Mapping[str, Mapping[str, AttributeSpec]] = {
    # Verified writable against Plant Simulation 13 with read-back after each
    # write (see scripts/verify_contract.py).
    ".MaterialFlow.Assembly": {
        "Capacity": _count("Capacity"),
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.Buffer": {
        "Capacity": _count("Capacity"),
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.Cycle": {},
    ".MaterialFlow.DismantleStation": {
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.Drain": {
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.Interface": {},
    ".MaterialFlow.Line": {
        "Capacity": _count("Capacity"),
        "Length": _measure("Length", 1.0, 0.0),
        "Speed": _measure("Speed", 1.0, 0.0),
    },
    ".MaterialFlow.ParallelProc": {
        "XDim": _count("XDim"),
        "YDim": _count("YDim"),
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.PlaceBuffer": {
        "Capacity": _count("Capacity"),
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.SingleProc": {
        "procTime": _time("procTime"),
    },
    ".MaterialFlow.Sorter": {
        "Capacity": _count("Capacity"),
    },
    ".MaterialFlow.Source": {
        "Interval": _time("Interval", 60.0),
        "Start": _time("Start", 0.0),
        "Stop": _time("Stop", 0.0),
        "TimeOfGeneration": _text(
            "TimeOfGeneration",
            "Interval adjustable",
            ("Interval adjustable", "Constant", "Interval", "Number adjustable", "Delivery table", "Delivery Table", "Trigger"),
        ),
    },
    ".MaterialFlow.Store": {
        "XDim": _count("XDim"),
        "YDim": _count("YDim"),
    },
    ".MaterialFlow.Track": {
        "Capacity": _count("Capacity"),
        "Length": _measure("Length", 1.0, 0.0),
    },
    ".MaterialFlow.Turntable": {
        "Length": _measure("Length", 1.0, 0.0),
        "Speed": _measure("Speed", 1.0, 0.0),
    },
    ".MaterialFlow.TwoLaneTrack": {
        "Capacity": _count("Capacity"),
    },
    ".Resources.WorkerPool": {
        "XDim": _count("XDim"),
        "YDim": _count("YDim"),
    },
    ".Resources.Workplace": {},
}


# ---------------------------------------------------------------------------
# identifiers and paths
# ---------------------------------------------------------------------------
def validate_identifier(value: Any, *, field_name: str = "identifier") -> str:
    """Return ``value`` if it is a SimTalk-safe identifier, else raise."""
    if not isinstance(value, str):
        raise ValidationError(field_name + " must be a string", details={"field": field_name, "value": repr(value)})
    if not value:
        raise ValidationError(field_name + " must not be empty", details={"field": field_name})
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValidationError(
            field_name + " is longer than " + str(MAX_IDENTIFIER_LENGTH) + " characters",
            details={"field": field_name, "length": len(value)},
        )
    if not IDENTIFIER_RE.match(value):
        raise ValidationError(
            field_name + " must match ^[A-Za-z_][A-Za-z0-9_]*\\Z (SimTalk-safe identifier)",
            details={"field": field_name, "value": value},
        )
    return value


def validate_object_path(value: Any, *, field_name: str = "path") -> str:
    """Return an absolute SimTalk object path (``.Models.Model.Station1``).

    A path must start with ``.``, contain at least one component, and every
    component must be a SimTalk-safe identifier. Whitespace, quotes, slashes,
    backslashes and control characters are rejected, so a path can never break
    out of a generated SimTalk statement.
    """
    if not isinstance(value, str):
        raise ValidationError(field_name + " must be a string", details={"field": field_name, "value": repr(value)})
    if not value:
        raise ValidationError(field_name + " must not be empty", details={"field": field_name})
    if not value.startswith("."):
        raise ValidationError(
            field_name + " must be an absolute SimTalk path starting with '.'",
            details={"field": field_name, "value": value},
        )
    if len(value) > MAX_IDENTIFIER_LENGTH * MAX_PATH_COMPONENTS:
        raise ValidationError(field_name + " is too long", details={"field": field_name, "length": len(value)})
    if CONTROL_CHAR_RE.search(value):
        raise ValidationError(field_name + " must not contain control characters", details={"field": field_name})
    components = value[1:].split(".")
    if len(components) > MAX_PATH_COMPONENTS:
        raise ValidationError(
            field_name + " has more than " + str(MAX_PATH_COMPONENTS) + " components",
            details={"field": field_name, "components": len(components)},
        )
    for position, component in enumerate(components):
        if not component:
            raise ValidationError(
                field_name + " contains an empty component",
                details={"field": field_name, "value": value, "component_index": position},
            )
        if not IDENTIFIER_RE.match(component):
            raise ValidationError(
                field_name + " component is not a SimTalk-safe identifier",
                details={"field": field_name, "value": value, "component": component},
            )
    return value


def join_object_path(frame_path: str, name: str) -> str:
    """Join a validated frame path and a validated object name into a path."""
    frame = validate_object_path(frame_path, field_name="frame_path")
    identifier = validate_identifier(name, field_name="name")
    return frame + "." + identifier


def validate_coordinate(value: Any, *, field_name: str = "coordinate") -> float:
    """Return a finite layout coordinate as float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field_name + " must be a number", details={"field": field_name, "value": repr(value)})
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(field_name + " must be finite", details={"field": field_name, "value": repr(value)})
    return number


# ---------------------------------------------------------------------------
# class and attribute allow-lists
# ---------------------------------------------------------------------------
def class_allowlist(*, include_auxiliary: bool = False) -> dict[str, str]:
    """Return a copy of the class allow-list (short name -> SimTalk path)."""
    allowlist = dict(DEVICE_CLASS_ALLOWLIST)
    if include_auxiliary:
        allowlist.update(AUXILIARY_CLASS_ALLOWLIST)
    return allowlist


def _resolve_class(value: Any, *, allow_auxiliary: bool) -> str:
    allowlist = class_allowlist(include_auxiliary=allow_auxiliary)
    canonical = {path: path for path in allowlist.values()}
    if isinstance(value, str):
        if value in allowlist:
            return allowlist[value]
        if value in DEPRECATED_CLASS_ALIASES:
            return DEPRECATED_CLASS_ALIASES[value]
        if value in canonical:
            return canonical[value]
    raise TypeNotAllowed(
        "object class is not in the allow-list",
        details={
            "class_name": repr(value),
            "allowed": sorted(allowlist),
            "allowed_paths": sorted(allowlist.values()),
            "auxiliary_allowed": allow_auxiliary,
        },
    )


def validate_class_name(value: Any, *, allow_auxiliary: bool = False) -> str:
    """Return the canonical SimTalk class path for an allowed class."""
    return _resolve_class(value, allow_auxiliary=allow_auxiliary)


def class_short_name(class_name: Any, *, allow_auxiliary: bool = True) -> str:
    """Return the short name (``Station``) for an allowed class path."""
    canonical = _resolve_class(class_name, allow_auxiliary=allow_auxiliary)
    for short, path in class_allowlist(include_auxiliary=allow_auxiliary).items():
        if path == canonical:
            return short
    raise TypeNotAllowed("object class is not in the allow-list", details={"class_name": repr(class_name)})


def writable_attributes(class_name: Any) -> tuple[str, ...]:
    """Return the sorted writable attribute names of an allowed class."""
    canonical = _resolve_class(class_name, allow_auxiliary=True)
    specs = CLASS_ATTRIBUTE_SPECS.get(canonical, {})
    return tuple(sorted(specs))


def known_attributes() -> set[str]:
    """Every attribute name the bridge knows, across all classes."""
    names: set[str] = set()
    for specs in CLASS_ATTRIBUTE_SPECS.values():
        names.update(specs)
    return names


def attribute_spec(class_name: Any, attribute: Any) -> AttributeSpec:
    """Return the declared spec for a writable attribute, else raise.

    A name that exists nowhere and a name that exists but not on this class
    are different mistakes, and saying so saves the caller a search: the
    former gets the full attribute vocabulary, the latter only what this
    class accepts.
    """
    canonical = _resolve_class(class_name, allow_auxiliary=True)
    specs = CLASS_ATTRIBUTE_SPECS.get(canonical, {})
    name = validate_identifier(attribute, field_name="attribute")
    spec = specs.get(name)
    if spec is None:
        if name not in known_attributes():
            raise TypeNotAllowed(
                "attribute is not in the allow-list; it is not one of the attributes this bridge models",
                details={"attribute": name, "known": sorted(known_attributes())},
            )
        raise PropertyNotApplicable(
            "attribute exists but is not writable on this object class",
            details={"class": canonical, "attribute": name, "allowed": sorted(specs)},
        )
    return spec


def validate_attribute(class_name: Any, attribute: Any) -> str:
    """Return the attribute name if it is writable on the class, else raise."""
    return attribute_spec(class_name, attribute).name


def is_attribute_applicable(class_name: Any, attribute: Any) -> bool:
    """Return True if the attribute is writable on the class (never raises)."""
    try:
        attribute_spec(class_name, attribute)
    except (TypeNotAllowed, ValidationError):
        return False
    return True


def default_values(class_name: Any) -> dict[str, Any]:
    """Return a fresh dict of default attribute values for an allowed class."""
    canonical = _resolve_class(class_name, allow_auxiliary=True)
    return {name: spec.default for name, spec in CLASS_ATTRIBUTE_SPECS.get(canonical, {}).items()}


def validate_attribute_value(class_name: Any, attribute: Any, value: Any) -> Any:
    """Coerce ``value`` to the declared kind of the attribute, or raise.

    ``time`` attributes are handled as plain seconds (float) at this layer; the
    COM backend is responsible for rendering the SimTalk time literal.
    """
    spec = attribute_spec(class_name, attribute)
    if spec.kind == TIME:
        number = validate_coordinate(value, field_name=spec.name)
        if number < 0.0:
            raise ValidationError(spec.name + " must be >= 0", details={"attribute": spec.name, "value": number})
        if spec.minimum is not None and number < spec.minimum:
            raise ValidationError(spec.name + " is below its minimum", details={"attribute": spec.name, "value": number})
        if spec.maximum is not None and number > spec.maximum:
            raise ValidationError(spec.name + " is above its maximum", details={"attribute": spec.name, "value": number})
        return number
    if spec.kind == INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(spec.name + " must be an integer", details={"attribute": spec.name, "value": repr(value)})
        if spec.minimum is not None and value < spec.minimum:
            raise ValidationError(spec.name + " is below its minimum", details={"attribute": spec.name, "value": value})
        if spec.maximum is not None and value > spec.maximum:
            raise ValidationError(spec.name + " is above its maximum", details={"attribute": spec.name, "value": value})
        return value
    if spec.kind == FLOAT:
        number = validate_coordinate(value, field_name=spec.name)
        if spec.minimum is not None and number < spec.minimum:
            raise ValidationError(spec.name + " is below its minimum", details={"attribute": spec.name, "value": number})
        if spec.maximum is not None and number > spec.maximum:
            raise ValidationError(spec.name + " is above its maximum", details={"attribute": spec.name, "value": number})
        return number
    if spec.kind == BOOL:
        if not isinstance(value, bool):
            raise ValidationError(spec.name + " must be a boolean", details={"attribute": spec.name, "value": repr(value)})
        return value
    if spec.kind == STRING:
        if not isinstance(value, str):
            raise ValidationError(spec.name + " must be a string", details={"attribute": spec.name, "value": repr(value)})
        if len(value) > MAX_STRING_LITERAL_LENGTH:
            raise ValidationError(spec.name + " is too long", details={"attribute": spec.name, "length": len(value)})
        if CONTROL_CHAR_RE.search(value):
            raise ValidationError(spec.name + " must not contain control characters", details={"attribute": spec.name})
        if spec.choices and value not in spec.choices:
            raise ValidationError(
                spec.name + " must be one of the declared choices",
                details={"attribute": spec.name, "value": value, "choices": list(spec.choices)},
            )
        return value
    raise ValidationError("unsupported attribute kind", details={"attribute": spec.name, "kind": spec.kind})


def values_equal(expected: Any, actual: Any) -> bool:
    """Compare a requested value with a read-back value (tolerant for floats)."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if not (math.isfinite(float(expected)) and math.isfinite(float(actual))):
            return False
        return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(expected, str) and isinstance(actual, str):
        # Measured: the product re-cases some enumerations -- writing
        # "Interval adjustable" reads back as "Interval Adjustable" -- so a
        # case-sensitive compare reports a mismatch on a write that worked.
        return expected.strip().casefold() == actual.strip().casefold()
    return expected == actual


# ---------------------------------------------------------------------------
# SimTalk literals
# ---------------------------------------------------------------------------
def escape_simtalk_string(value: Any, *, field_name: str = "value") -> str:
    """Return the body of a SimTalk string literal (without the delimiters).

    Verified on Plant Simulation 13: SimTalk delimits string literals with
    double quotes and interprets backslash escapes before backslashes (``\\\\``)
    and double quotes (``\\"``), while rejecting doubled quotes (``""``).
    Control characters are rejected before quoting.
    """
    if not isinstance(value, str):
        raise ValidationError(field_name + " must be a string", details={"field": field_name, "value": repr(value)})
    if len(value) > MAX_STRING_LITERAL_LENGTH:
        raise ValidationError(
            field_name + " is longer than " + str(MAX_STRING_LITERAL_LENGTH) + " characters",
            details={"field": field_name, "length": len(value)},
        )
    match = CONTROL_CHAR_RE.search(value)
    if match:
        raise ValidationError(
            field_name + " must not contain control characters",
            details={"field": field_name, "position": match.start(), "codepoint": "U+" + format(ord(match.group()), "04X")},
        )
    return value.replace("\\", "\\\\").replace('"', '\\"')


def quote_simtalk_string(value: Any, *, field_name: str = "value") -> str:
    """Return a complete, quoted SimTalk string literal."""
    return '"' + escape_simtalk_string(value, field_name=field_name) + '"'


def seconds_to_simtalk_time(seconds: Any, *, field_name: str = "seconds") -> str:
    """Render seconds as a SimTalk ``h:mm:ss[.sss]`` time literal.

    The value is rounded to the millisecond first, so carries (59.9996 s ->
    ``0:01:00``) are handled without special cases.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValidationError(field_name + " must be a number", details={"field": field_name, "value": repr(seconds)})
    number = float(seconds)
    if not math.isfinite(number):
        raise ValidationError(field_name + " must be finite", details={"field": field_name, "value": repr(seconds)})
    if number < 0.0:
        raise ValidationError(field_name + " must be >= 0", details={"field": field_name, "value": number})
    if number > MAX_SIMTIME_SECONDS:
        raise ValidationError(
            field_name + " exceeds the maximum simulation time",
            details={"field": field_name, "value": number, "maximum": MAX_SIMTIME_SECONDS},
        )
    total_ms = round(number * 1000.0)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    literal = f"{hours}:{minutes:02d}:{whole_seconds:02d}"
    if milliseconds:
        literal += "." + format(milliseconds, "03d")
    return literal


def validate_simtalk_source(value: Any, *, field_name: str = "source") -> str:
    """Validate a raw SimTalk source block (newlines/tabs allowed)."""
    if not isinstance(value, str):
        raise ValidationError(field_name + " must be a string", details={"field": field_name, "value": repr(value)})
    if not value.strip():
        raise ValidationError(field_name + " must not be empty", details={"field": field_name})
    if len(value) > MAX_SIMTALK_SOURCE_LENGTH:
        raise ValidationError(
            field_name + " is longer than " + str(MAX_SIMTALK_SOURCE_LENGTH) + " characters",
            details={"field": field_name, "length": len(value)},
        )
    match = _ALLOWED_SOURCE_CONTROL_RE.search(value)
    if match:
        raise ValidationError(
            field_name + " must not contain control characters other than tab/newline/carriage return",
            details={"field": field_name, "position": match.start(), "codepoint": "U+" + format(ord(match.group()), "04X")},
        )
    return value


# ---------------------------------------------------------------------------
# DataTable bounds
# ---------------------------------------------------------------------------
def _validate_index(value: Any, *, field_name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(field_name + " must be an integer", details={"field": field_name, "value": repr(value)})
    if value < minimum:
        raise ValidationError(
            field_name + " must be >= " + str(minimum), details={"field": field_name, "value": value}
        )
    return value


def validate_table_index(row: Any, column: Any) -> tuple[int, int]:
    """Validate a 1-based table cell index (SimTalk DataTables start at 1)."""
    return (
        _validate_index(row, field_name="row", minimum=1),
        _validate_index(column, field_name="column", minimum=1),
    )


def table_cell_count(rows: Any, columns: Any) -> int:
    """Return rows * columns after validating both as integers."""
    return _validate_index(rows, field_name="rows") * _validate_index(columns, field_name="columns")


def validate_table_size(rows: Any, columns: Any, *, max_cells: int | None = None) -> tuple[int, int]:
    """Validate a table size (at least 1x1) and an optional cell budget."""
    row_count = _validate_index(rows, field_name="rows", minimum=1)
    column_count = _validate_index(columns, field_name="columns", minimum=1)
    if max_cells is not None:
        _validate_index(max_cells, field_name="max_cells", minimum=1)
        cells = row_count * column_count
        if cells > max_cells:
            raise ValidationError(
                "table size exceeds the configured cell budget",
                details={"rows": row_count, "columns": column_count, "cells": cells, "max_cells": max_cells},
            )
    return row_count, column_count


def validate_table_cell_bounds(row: Any, column: Any, *, rows: int, columns: int) -> tuple[int, int]:
    """Validate a 1-based cell index against the table size."""
    row_index, column_index = validate_table_index(row, column)
    row_count, column_count = validate_table_size(rows, columns)
    if row_index > row_count or column_index > column_count:
        raise ValidationError(
            "table cell is out of bounds",
            details={"row": row_index, "column": column_index, "rows": row_count, "columns": column_count},
        )
    return row_index, column_index


def validate_table_range(
    row1: Any,
    column1: Any,
    row2: Any,
    column2: Any,
    *,
    rows: int,
    columns: int,
    max_cells: int | None = None,
) -> tuple[int, int, int, int]:
    """Validate an inclusive 1-based table range against the table size."""
    first_row, first_column = validate_table_cell_bounds(row1, column1, rows=rows, columns=columns)
    last_row, last_column = validate_table_cell_bounds(row2, column2, rows=rows, columns=columns)
    if last_row < first_row or last_column < first_column:
        raise ValidationError(
            "table range is reversed (row2/column2 must not precede row1/column1)",
            details={
                "row1": first_row,
                "column1": first_column,
                "row2": last_row,
                "column2": last_column,
            },
        )
    if max_cells is not None:
        cells = (last_row - first_row + 1) * (last_column - first_column + 1)
        if cells > max_cells:
            raise ValidationError(
                "table range exceeds the configured cell budget",
                details={"cells": cells, "max_cells": max_cells},
            )
    return first_row, first_column, last_row, last_column

#: Control hooks measured to exist on this build. EntranceCtrl and ExitCtrl are
#: the two that actually fire on a Source or a processing object; InitCtrl is
#: accepted by the product for initialisation.
CONTROL_NAMES = ("EntranceCtrl", "ExitCtrl", "InitCtrl")

#: Attribute kinds .MUs.Entity.createAttr accepts.
MU_ATTRIBUTE_KINDS = frozenset({"integer", "string", "real", "boolean"})


#: A frame node handle: <frame path>.node(<expression>). Measured: a newly
#: created information-flow object cannot be renamed and is not reachable by
#: dotted name, so this is the only handle the product offers for one.
_NODE_HANDLE_RE = re.compile(r"^(?P<frame>\.[A-Za-z0-9_.]+)\.node\((?P<index>[^()]+)\)\Z")
_NODE_INDEX_RE = re.compile(r"^[A-Za-z0-9_. ]+\Z")


def validate_object_handle(value: Any, *, field_name: str = "path") -> str:
    """Accept an object path, or a frame node handle.

    The handle is a SimTalk EXPRESSION, not a path, so it gets its own
    validator instead of widening validate_object_path -- every caller that
    wants a path should still be told when it passed an expression.
    """
    if not isinstance(value, str):
        raise ValidationError(
            field_name + " must be a string", details={"field": field_name, "value": repr(value)}
        )
    if CONTROL_CHAR_RE.search(value):
        raise ValidationError(
            field_name + " must not contain control characters", details={"field": field_name}
        )
    text = value.strip()
    match = _NODE_HANDLE_RE.match(text)
    if match is None:
        return validate_object_path(text, field_name=field_name)
    validate_object_path(match.group("frame"), field_name=field_name)
    index = match.group("index").strip()
    if not index or not _NODE_INDEX_RE.match(index):
        raise ValidationError(
            field_name + " node index is not a SimTalk-safe expression",
            details={"field": field_name, "index": index},
        )
    return text


def validate_control_name(value: Any) -> str:
    """Return the canonical control hook name, or raise."""
    if not isinstance(value, str):
        raise ValidationError("control must be a string", details={"control": repr(value)})
    for name in CONTROL_NAMES:
        if value.strip().casefold() == name.casefold():
            return name
    raise ValidationError(
        "unknown control hook",
        details={"control": repr(value), "allowed": list(CONTROL_NAMES)},
    )


def validate_mu_attribute_kind(value: Any) -> str:
    """Return the canonical MU attribute type accepted by Entity.createAttr."""
    if not isinstance(value, str):
        raise ValidationError("MU attribute kind must be a string", details={"kind": repr(value)})
    normalized = value.strip().casefold()
    if normalized not in MU_ATTRIBUTE_KINDS:
        raise ValidationError(
            "unsupported MU attribute kind",
            details={"kind": repr(value), "allowed": sorted(MU_ATTRIBUTE_KINDS)},
        )
    return normalized
