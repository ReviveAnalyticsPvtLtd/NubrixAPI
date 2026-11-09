"""
chartRequest.py

The chart request a model returns for a prompt chart: one DuckDB SQL query plus
a mapping from result columns to chart roles. The backend builds the chart JSON
from the query result. This module holds the model, the per-chart-type role
rules, the launcher row caps and the compact `generatedCode` text form.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = [
    "CHART_TYPES",
    "CATEGORY_TYPES",
    "LABEL_CAP",
    "Encoding",
    "ChartRequest",
    "requiredColumns",
    "rowLimit",
    "encodeGeneratedCode",
    "decodeGeneratedCode",
]

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CHART_TYPES = (
    "bar",
    "line",
    "area",
    "histogram",
    "radar",
    "pie",
    "doughnut",
    "polarArea",
    "scatter",
    "bubble",
    "card",
    "table",
    "pivot",
    "geoMap",
)

CATEGORY_TYPES = ("bar", "line", "area", "histogram", "radar", "pie", "doughnut", "polarArea")

# Distinct labels a category chart may carry. The builder enforces it; the
# launcher returns up to CATEGORY_ROW_LIMIT rows because long-form `series`
# rows are labels multiplied by series.
LABEL_CAP = 500
CATEGORY_ROW_LIMIT = 5000

GENERATED_CODE_MARKER = "nubrixChart"
GENERATED_CODE_VERSION = 1

# Order in which an encoding's columns are listed.
ROLE_ORDER = ("x", "y", "series", "label", "value", "r", "lat", "long", "id", "rows", "columns")
LIST_ROLES = ("y", "rows", "columns")

# chartType -> (required roles, optional roles). A role names a result column,
# except a card's `label`, which is literal text.
_AXIS_ROLES = (("x", "y"), ("series",))
ROLE_TABLE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "bar": _AXIS_ROLES,
    "line": _AXIS_ROLES,
    "area": _AXIS_ROLES,
    "radar": _AXIS_ROLES,
    "histogram": (("x", "y"), ()),
    "pie": (("label", "value"), ()),
    "doughnut": (("label", "value"), ()),
    "polarArea": (("label", "value"), ()),
    "scatter": (("x", "y"), ("series",)),
    "bubble": (("x", "y", "r"), ("series",)),
    "card": (("value",), ("label",)),
    "table": ((), ("columns",)),
    "pivot": (("rows", "columns", "value"), ()),
    "geoMap": (("lat", "long"), ("label", "id")),
}

# Chart types that take exactly one `y` column.
_SINGLE_Y = ("histogram", "scatter", "bubble")

ROW_LIMITS = {
    "table": 1000,
    "geoMap": 2000,
    "scatter": 5000,
    "bubble": 5000,
    "pivot": 10000,
    "card": 2,
}


class Encoding(BaseModel):
    """Maps chart roles to column names of the query result."""

    model_config = ConfigDict(extra="forbid")

    x: str | None = None
    y: list[str] | None = None
    series: str | None = None
    label: str | None = None
    value: str | None = None
    r: str | None = None
    lat: str | None = None
    long: str | None = None
    id: str | None = None
    rows: list[str] | None = None
    columns: list[str] | None = None

    @field_validator("y", mode="before")
    @classmethod
    def _singleYBecomesList(cls, value):
        """Accepts one column name for `y` and wraps it in a list."""
        return [value] if isinstance(value, str) else value


class ChartRequest(BaseModel):
    """A prompt chart as the model describes it."""

    model_config = ConfigDict(extra="forbid")

    chartType: Literal[CHART_TYPES]
    title: str = Field(min_length=1, max_length=200)
    xLabel: str | None = None
    yLabel: str | None = None
    sql: str = Field(min_length=1, max_length=20000)
    encoding: Encoding
    datasetLabels: dict[str, str] | None = None

    @model_validator(mode="after")
    def _checkRoles(self):
        """Enforces the role table for the chart type."""
        required, optional = ROLE_TABLE[self.chartType]
        encoding = self.encoding
        for role in required:
            if not getattr(encoding, role):
                raise ValueError(f"{self.chartType} needs the encoding role '{role}'")
        for role in ROLE_ORDER:
            value = getattr(encoding, role)
            if value is None:
                continue
            if role not in required and role not in optional:
                raise ValueError(f"{self.chartType} does not use the encoding role '{role}'")
            names = value if role in LIST_ROLES else [value]
            if not names or any(not name for name in names):
                raise ValueError(f"{self.chartType} encoding role '{role}' has an empty column name")
        if encoding.y is not None:
            if self.chartType in _SINGLE_Y and len(encoding.y) != 1:
                raise ValueError(f"{self.chartType} takes exactly one column for the encoding role 'y'")
            if len(encoding.y) > 1 and encoding.series:
                raise ValueError(
                    f"{self.chartType} cannot combine several 'y' columns with the encoding role 'series'"
                )
        return self


def requiredColumns(request: ChartRequest) -> list[str]:
    """
    Lists every result column the encoding names, in role order, without repeats.

    A card's `label` is literal text and is left out. A table without `columns`
    gives an empty list (it uses every result column).
    """
    columns: list[str] = []
    for role in ROLE_ORDER:
        if role == "label" and request.chartType == "card":
            continue
        value = getattr(request.encoding, role)
        if value is None:
            continue
        for name in value if role in LIST_ROLES else [value]:
            if name not in columns:
                columns.append(name)
    return columns


def rowLimit(chartType: str) -> int:
    """Returns the number of rows the launcher may return for a chart type."""
    if chartType in CATEGORY_TYPES:
        return CATEGORY_ROW_LIMIT
    if chartType in ROW_LIMITS:
        return ROW_LIMITS[chartType]
    raise ValueError(f"Unknown chart type '{chartType}'")


def encodeGeneratedCode(request: ChartRequest) -> str:
    """Returns the compact JSON stored in `generatedCode`, marker key first."""
    body = request.model_dump(exclude_none=True)
    return json.dumps(
        {GENERATED_CODE_MARKER: GENERATED_CODE_VERSION, **body},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def decodeGeneratedCode(text: str | None) -> ChartRequest | None:
    """
    Reads a chart request back from `generatedCode`.

    Returns:
        ChartRequest | None: None when the text is not a JSON object carrying the
            marker key (the old Python form).

    Raises:
        ValueError: The marker is present but the version is not supported or
            the request fails validation.
    """
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or GENERATED_CODE_MARKER not in parsed:
        return None
    version = parsed.pop(GENERATED_CODE_MARKER)
    if isinstance(version, bool) or version != GENERATED_CODE_VERSION:
        raise ValueError(f"Unsupported chart request version: {version!r}")
    return ChartRequest.model_validate(parsed)
