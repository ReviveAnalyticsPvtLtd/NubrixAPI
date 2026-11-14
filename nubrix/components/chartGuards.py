"""
chartGuards.py

Checks on a chart the SQL chart workflow has just built, for two habits the model keeps despite the prompt rules:

- a number label cast to text under its own name (`CAST("n" AS VARCHAR) AS n ... ORDER BY "n"`), so the labels can
  sort as text (1, 10, 2) and a whole-number DOUBLE shows as "1.0";
- a scatter or bubble chart that gives every point its own `series` value, so every point becomes its own dataset and
  legend entry.

A guard returns a message for the repair prompt, or None. It fires only when it can decide: anything it cannot read
(SQL that does not parse, a projection it cannot match to the label, a chart of an unexpected shape) declines.
Pure: no sandbox, no model. Only new chart generation calls it; saved widgets re-run without it.
"""

__all__ = [
    "CAST_LABEL_MESSAGE",
    "CAST_LOG_MESSAGE",
    "POINT_SERIES_LOG_MESSAGE",
    "POINT_SERIES_MESSAGE",
    "castLabelMessage",
    "guardLogMessage",
    "guardMessage",
    "pointSeriesMessage",
]

import re

import sqlglot
from sqlglot import exp

from nubrix.components.chartBuilder import EMPTY_LEVEL
from nubrix.components.chartRequest import ChartRequest

CAST_LABEL_MESSAGE = (
    "Column '{name}' casts a number to text, so its labels can sort as text (1, 10, 2) and a whole-number DOUBLE shows "
    "as '1.0'. Keep it a number: drop the CAST. The backend shows a number label as text, keeps the query's row order, "
    "and shows a whole-number DOUBLE as '3'. Order by the numeric value."
)
CAST_LOG_MESSAGE = "A label column casts a number to text."
POINT_SERIES_MESSAGE = (
    "Each point has its own series value, so every point becomes its own dataset and legend entry. Leave `series` out "
    "when each row is its own point; use it only to group many points into a few datasets, such as a category or a "
    "region."
)
POINT_SERIES_LOG_MESSAGE = "Every point of a scatter or bubble chart has its own series value."

_AXIS_LABEL_TYPES = ("bar", "line", "area", "histogram", "radar")
_SLICE_TYPES = ("pie", "doughnut", "polarArea")
_POINT_TYPES = ("scatter", "bubble")
_TEXT_TYPES = frozenset(
    getattr(exp.DataType.Type, name)
    for name in ("TEXT", "VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BPCHAR")
    if hasattr(exp.DataType.Type, name)
)
# Number text: no leading zero (that is a code, such as a postal code), no exponent, no padding.
_NUMBER_TEXT = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")


def guardMessage(request: ChartRequest, sql: str, chart: object) -> str | None:
    """The first guard that fires on this built chart, as its repair message; None when none fires or any is unsure."""
    for guard in (castLabelMessage, pointSeriesMessage):
        try:
            message = guard(request, sql, chart)
        except Exception:
            # A guard that cannot decide never fires.
            message = None
        if message is not None:
            return message
    return None


def guardLogMessage(message: str) -> str:
    """The data-free text of a guard message, for the failure log."""
    return POINT_SERIES_LOG_MESSAGE if message == POINT_SERIES_MESSAGE else CAST_LOG_MESSAGE


def castLabelMessage(request: ChartRequest, sql: str, chart: object) -> str | None:
    """A label of a category, slice or pivot chart that the outermost SELECT casts to text and that holds only numbers."""
    chartType = request.chartType
    encoding = request.encoding
    if chartType in _AXIS_LABEL_TYPES:
        roles = [(encoding.x, _categoryLabels(chart))]
    elif chartType in _SLICE_TYPES:
        roles = [(encoding.label, _categoryLabels(chart))]
    elif chartType == "pivot":
        roles = _pivotRoles(request, chart)
    else:
        return None
    projections = _outerProjections(sql)
    if projections is None:
        return None
    for name, labels in roles:
        if name and labels is not None and _isTextCast(projections, name) and _allNumberText(labels):
            return CAST_LABEL_MESSAGE.format(name=name)
    return None


def pointSeriesMessage(request: ChartRequest, sql: str, chart: object) -> str | None:
    """A scatter or bubble chart with a series where every dataset holds exactly one point."""
    if request.chartType not in _POINT_TYPES or not request.encoding.series:
        return None
    data = chart.get("data") if isinstance(chart, dict) else None
    datasets = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(datasets, list) or len(datasets) < 2:
        return None
    for dataset in datasets:
        points = dataset.get("data") if isinstance(dataset, dict) else None
        if not isinstance(points, list) or len(points) != 1:
            return None
    return POINT_SERIES_MESSAGE


def _outerProjections(sql: str) -> list[exp.Expression] | None:
    """The projections of the single outermost SELECT; None for anything else (a set operation, a parse failure)."""
    text = sql.strip() if isinstance(sql, str) else ""
    while text.endswith(";"):
        text = text[:-1].rstrip()
    try:
        statements = [statement for statement in sqlglot.parse(text, read="duckdb") if statement is not None]
    except Exception:
        return None
    if len(statements) != 1:
        return None
    body = statements[0]
    while isinstance(body, exp.Subquery) and not body.args.get("alias") and not body.args.get("pivots"):
        body = body.this
    if not isinstance(body, exp.Select):
        return None
    projections = list(body.expressions)
    if any(projection.is_star for projection in projections):
        # A star can supply the label's column too.
        return None
    return projections


def _outputName(projection: exp.Expression) -> str | None:
    """The result column name DuckDB gives a projection, when it can be read off the SQL: an alias or a bare column."""
    if isinstance(projection, exp.Alias):
        return projection.alias
    if isinstance(projection, exp.Column):
        return projection.name
    return None


def _isTextCast(projections: list[exp.Expression], name: str) -> bool:
    """Whether exactly one projection gives `name`, and it is an aliased bare cast to a text type."""
    matches = [projection for projection in projections if _outputName(projection) == name]
    if len(matches) != 1 or not isinstance(matches[0], exp.Alias):
        return False
    value = matches[0].this
    while isinstance(value, exp.Paren):
        value = value.this
    # TRY_CAST and `::` parse as a Cast too.
    if not isinstance(value, exp.Cast):
        return False
    target = value.args.get("to")
    return isinstance(target, exp.DataType) and target.this in _TEXT_TYPES


def _allNumberText(labels: list) -> bool:
    """At least one non-empty label, and every non-empty one is number text."""
    filled = [label for label in labels if label != ""]
    return bool(filled) and all(isinstance(label, str) and _NUMBER_TEXT.fullmatch(label) for label in filled)


def _categoryLabels(chart: object) -> list | None:
    data = chart.get("data") if isinstance(chart, dict) else None
    labels = data.get("labels") if isinstance(data, dict) else None
    return labels if isinstance(labels, list) else None


def _pivotRoles(request: ChartRequest, chart: object) -> list[tuple[str, list | None]]:
    """(column, its labels) for each pivot rows and columns column, read back from the cross-table's keys."""
    data = chart.get("data") if isinstance(chart, dict) else None
    if not isinstance(data, dict) or not all(isinstance(rows, dict) for rows in data.values()):
        return []
    columnKeys = list(data)
    rowKeys = list(dict.fromkeys(rowKey for rows in data.values() for rowKey in rows))
    roles = []
    for names, keys in ((request.encoding.rows or [], rowKeys), (request.encoding.columns or [], columnKeys)):
        levels = [_pivotLevels(key, len(names)) for key in keys]
        for index, name in enumerate(names):
            labels = None if any(level is None for level in levels) else [level[index] for level in levels]
            roles.append((name, labels))
    return roles


def _pivotLevels(key: object, count: int) -> list[str] | None:
    """The levels of a pivot key: plain text for one level, the builder's ('a', 'b') text for several.

    The builder writes an empty level as an em dash and never leaves an apostrophe or a parenthesis in a level.
    """
    if not isinstance(key, str):
        return None
    if count == 1:
        levels = [key]
    elif key.startswith("('") and key.endswith("')"):
        levels = key[2:-2].split("', '")
    else:
        return None
    if len(levels) != count:
        return None
    return ["" if level == EMPTY_LEVEL else level for level in levels]
