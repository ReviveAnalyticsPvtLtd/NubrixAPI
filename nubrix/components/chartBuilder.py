"""
chartBuilder.py

Turns the Arrow result of a chart's SQL query plus its ChartRequest into the
chart dictionary the client draws. Pure Python: values are converted with
explicit rules (no pandas), so NaN, big integers, decimals and dates always end
up JSON safe. Errors are written for the repair prompt and name the fix.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["ChartBuildError", "buildChart", "emptyChart", "readableLabel"]

import logging
import math
import re
from decimal import Decimal

import pyarrow as pa

from nubrix.components.chartRequest import (
    CATEGORY_TYPES,
    CHART_TYPES,
    LABEL_CAP,
    ChartRequest,
    requiredColumns,
)

logger = logging.getLogger(__name__)

MAX_EXACT_INT = 2**53
EMPTY_LEVEL = "\u2014"
AXIS_TYPES = ("bar", "line", "area", "histogram")
POINT_TYPES = ("scatter", "bubble")
SINGLE_DATASET_TYPES = ("pie", "doughnut", "polarArea")
APOSTROPHE = "’"


class ChartBuildError(ValueError):
    """The query result cannot be drawn as the requested chart; the message names the fix.

    The message goes to the repair prompt and may quote labels or cells. `logMessage` says the same without
    any data (no labels, cells or column names); it is what the failure log records.
    """

    def __init__(self, message: str, *, logMessage: str):
        super().__init__(message)
        self.logMessage = logMessage


def readableLabel(column: str) -> str:
    """
    Makes a column name readable.

    Args:
        column (str): A result column name such as `total_payment_value` or `avgPrice`.

    Returns:
        str: "Total Payment Value" or "Avg Price".
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", column)
    words = re.split(r"[\s_\-]+", spaced)
    readable = " ".join(word[:1].upper() + word[1:] for word in words if word)
    return readable or column


def emptyChart(chartType: str, title: str, xLabel: str | None = None, yLabel: str | None = None) -> dict:
    """
    Returns a valid chart of the type with no data.

    Raises:
        ValueError: The chart type is not one of CHART_TYPES.
    """
    if chartType not in CHART_TYPES:
        raise ValueError(f"Unknown chart type '{chartType}'")
    chart: dict = {"chartType": chartType, "title": title}
    if chartType in AXIS_TYPES:
        chart.update(xLabels=xLabel or "", yLabels=yLabel or "", data={"labels": [], "datasets": []})
    elif chartType in POINT_TYPES:
        chart.update(xLabels=xLabel or "", yLabels=yLabel or "", data={"datasets": []})
    elif chartType in SINGLE_DATASET_TYPES:
        # The client contract wants exactly one dataset for these types.
        chart["data"] = {"labels": [], "datasets": [{"label": "", "data": []}]}
    elif chartType in CATEGORY_TYPES:
        chart["data"] = {"labels": [], "datasets": []}
    elif chartType == "card":
        chart.update(label="", data=0)
    elif chartType == "table":
        chart.update(columns=[], data=[])
    elif chartType == "pivot":
        chart["data"] = {}
    else:
        chart["map"] = {"mapType": "scatterMap", "data": {"points": []}}
    return chart


def _number(value: object) -> int | float | None:
    """A JSON number for a numeric value; None for null, NaN, infinity and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value if abs(value) <= MAX_EXACT_INT else float(value)


def _text(value: object) -> str:
    """Label text: null and NaN give "", dates give ISO text."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _levelText(value: object) -> str:
    """
    A pivot key level the client's parsers read back unchanged: its regexes
    cannot hold an apostrophe, a parenthesis or an empty level.
    """
    text = _text(value)
    text = text.replace("'", APOSTROPHE).replace("(", "\uff08").replace(")", "\uff09")
    return text or EMPTY_LEVEL


def _cell(value: object):
    """A JSON-safe table or card value; integers above 2**53 become text."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, (float, Decimal)):
        return _number(value)
    if isinstance(value, int):
        return value if abs(value) <= MAX_EXACT_INT else str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_cell(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _cell(item) for key, item in value.items()}
    return str(value)


def _checkColumn(table: pa.Table, name: str) -> None:
    if table.column_names.count(name) != 1:
        raise ChartBuildError(
            f"The query result has no single column '{name}'; its columns are {table.column_names}. "
            "Use the exact output column names in the encoding.",
            logMessage="The query result has no single column for a name in the encoding.",
        )


def _column(table: pa.Table, name: str) -> list:
    _checkColumn(table, name)
    return table.column(name).to_pylist()


def _requireNumeric(table: pa.Table, names: list[str], fix: str) -> None:
    """Strict mode: every named column must have a numeric Arrow type."""
    for name in names:
        kind = table.schema.field(name).type
        numeric = pa.types.is_integer(kind) or pa.types.is_floating(kind) or pa.types.is_decimal(kind)
        if not (numeric or pa.types.is_null(kind)):
            raise ChartBuildError(
                f"Column '{name}' has type {kind} but must be numeric. {fix}",
                logMessage=f"A column has type {kind} but must be numeric. {fix}",
            )


def _checkLabelCount(request: ChartRequest, count: int, truncated: bool, lenient: bool) -> None:
    """Enforces the label cap; lenient keeps the first LABEL_CAP labels and warns."""
    if count <= LABEL_CAP and not truncated:
        return
    if count > LABEL_CAP:
        problem = f"{request.chartType} chart has {count} labels"
    else:
        problem = f"query returned more rows than a {request.chartType} chart can use"
    if lenient:
        if count > LABEL_CAP:
            logger.warning("The %s; keeping the first %d labels", problem, LABEL_CAP)
        else:
            logger.warning("The %s; the data is incomplete", problem)
        return
    raise ChartBuildError(
        f"The {problem} (limit {LABEL_CAP}). Aggregate further, and add ORDER BY and LIMIT {LABEL_CAP}.",
        logMessage=f"The {problem} (limit {LABEL_CAP}).",
    )


def _datasetName(request: ChartRequest, column: str) -> str:
    return (request.datasetLabels or {}).get(column) or readableLabel(column)


def _axisLabels(request: ChartRequest) -> tuple[str, str]:
    encoding = request.encoding
    return (
        request.xLabel or readableLabel(encoding.x),
        request.yLabel or readableLabel(encoding.y[0]),
    )


def _buildCategory(request: ChartRequest, table: pa.Table, truncated: bool, lenient: bool) -> dict:
    encoding = request.encoding
    labelColumn = encoding.x or encoding.label
    valueColumns = encoding.y or [encoding.value]
    _requireNumeric(table, valueColumns, "Return a numeric value column, aggregated with SUM, AVG or COUNT.")
    rawLabels = [_text(value) for value in _column(table, labelColumn)]
    valueLists = [[_number(value) for value in _column(table, name)] for name in valueColumns]

    if not encoding.series:
        _checkLabelCount(request, len(rawLabels), truncated, lenient)
        labels = rawLabels[:LABEL_CAP]
        datasets = [
            {"label": _datasetName(request, name), "data": values[:LABEL_CAP]}
            for name, values in zip(valueColumns, valueLists)
        ]
    else:
        labelIndex: dict[str, int] = {}
        for label in rawLabels:
            labelIndex.setdefault(label, len(labelIndex))
        _checkLabelCount(request, len(labelIndex), truncated, lenient)
        labels = list(labelIndex)[:LABEL_CAP]
        seriesNames = [_text(value) for value in _column(table, encoding.series)]
        cells: dict[str, dict[int, int | float | None]] = {}
        for label, name, value in zip(rawLabels, seriesNames, valueLists[0]):
            index = labelIndex[label]
            if index >= LABEL_CAP:
                continue
            column = cells.setdefault(name, {})
            if index in column:
                raise ChartBuildError(
                    f"The query has more than one row for x '{label}' and series '{name}'. "
                    "Aggregate with GROUP BY so each pair appears once.",
                    logMessage="The query has more than one row for an x and series pair.",
                )
            column[index] = value
        datasets = [
            {"label": name, "data": [column.get(index) for index in range(len(labels))]}
            for name, column in cells.items()
        ]

    chart: dict = {"chartType": request.chartType, "title": request.title}
    if request.chartType in AXIS_TYPES:
        chart["xLabels"], chart["yLabels"] = _axisLabels(request)
    chart["data"] = {"labels": labels, "datasets": datasets}
    return chart


def _buildPoints(request: ChartRequest, table: pa.Table) -> dict:
    encoding = request.encoding
    roles = [("x", encoding.x), ("y", encoding.y[0])]
    if request.chartType == "bubble":
        roles.append(("r", encoding.r))
    _requireNumeric(
        table,
        [name for _, name in roles],
        f"A {request.chartType} chart needs numeric columns for x, y and r; "
        "for a text or date x use a line or bar chart instead.",
    )
    values = [[_number(value) for value in _column(table, name)] for _, name in roles]
    seriesNames = [_text(v) for v in _column(table, encoding.series)] if encoding.series else None
    datasets: dict[str, list[dict]] = {}
    for index, numbers in enumerate(zip(*values)):
        if any(number is None for number in numbers):
            continue
        name = seriesNames[index] if seriesNames else _datasetName(request, encoding.y[0])
        datasets.setdefault(name, []).append({role: number for (role, _), number in zip(roles, numbers)})
    xLabels, yLabels = _axisLabels(request)
    return {
        "chartType": request.chartType,
        "title": request.title,
        "xLabels": xLabels,
        "yLabels": yLabels,
        "data": {"datasets": [{"label": name, "data": points} for name, points in datasets.items()]},
    }


def _cardLabel(request: ChartRequest) -> str:
    return request.encoding.label or readableLabel(request.encoding.value)


def _buildCard(request: ChartRequest, table: pa.Table) -> dict:
    if table.num_rows > 1:
        raise ChartBuildError(
            f"A card shows one value but the query returned {table.num_rows} rows. "
            "Aggregate to a single row, or add LIMIT 1.",
            logMessage=f"A card shows one value but the query returned {table.num_rows} rows.",
        )
    column = request.encoding.value
    raw = _column(table, column)[0]
    if isinstance(raw, (list, tuple, dict)):
        raise ChartBuildError(
            f"Column '{column}' holds a list or struct, which a card cannot show. Return a single number or text.",
            logMessage="The card column holds a list or struct, which a card cannot show.",
        )
    value = "true" if raw is True else "false" if raw is False else _cell(raw)
    return {
        "chartType": "card",
        "title": request.title,
        "label": _cardLabel(request),
        "data": 0 if value is None else value,
    }


def _buildTable(request: ChartRequest, table: pa.Table) -> dict:
    columns = list(request.encoding.columns or table.column_names)
    data = [_column(table, name) for name in columns]
    rows = [{name: _cell(value) for name, value in zip(columns, row)} for row in zip(*data)]
    return {"chartType": "table", "title": request.title, "columns": columns, "data": rows}


def _pivotKey(levels: tuple[str, ...]) -> str:
    """One level is plain text; several are the tuple text the client parses."""
    if len(levels) == 1:
        return levels[0]
    quoted = ", ".join(f"'{level}'" for level in levels)
    return f"({quoted})"


def _buildPivot(request: ChartRequest, table: pa.Table, truncated: bool) -> dict:
    encoding = request.encoding
    if truncated:
        raise ChartBuildError(
            "The pivot query returned more rows than a pivot can use, so its totals would be wrong. "
            "Aggregate further so fewer (row, column) pairs are returned.",
            logMessage="The pivot query returned more rows than a pivot can use.",
        )
    _requireNumeric(table, [encoding.value], "Return a numeric value column, aggregated with SUM, AVG or COUNT.")
    rowLevels = zip(*[[_levelText(v) for v in _column(table, name)] for name in encoding.rows])
    columnLevels = zip(*[[_levelText(v) for v in _column(table, name)] for name in encoding.columns])
    rowKeys: dict[str, None] = {}
    cells: dict[str, dict[str, int | float | None]] = {}
    for rowLevel, columnLevel, value in zip(rowLevels, columnLevels, _column(table, encoding.value)):
        rowKey, columnKey = _pivotKey(rowLevel), _pivotKey(columnLevel)
        rowKeys[rowKey] = None
        column = cells.setdefault(columnKey, {})
        if rowKey in column:
            raise ChartBuildError(
                f"The query has more than one row for pivot row {rowKey} and column {columnKey}. "
                "Aggregate with GROUP BY so each pair appears once.",
                logMessage="The query has more than one row for a pivot row and column pair.",
            )
        column[rowKey] = _number(value)
    return {
        "chartType": "pivot",
        "title": request.title,
        "data": {key: {rowKey: column.get(rowKey) for rowKey in rowKeys} for key, column in cells.items()},
    }


def _buildGeoMap(request: ChartRequest, table: pa.Table) -> dict:
    encoding = request.encoding
    _requireNumeric(table, [encoding.lat, encoding.long], "Use CAST(... AS DOUBLE) for the coordinates.")
    lats, longs = _column(table, encoding.lat), _column(table, encoding.long)
    labels = _column(table, encoding.label) if encoding.label else [None] * table.num_rows
    ids = _column(table, encoding.id) if encoding.id else [None] * table.num_rows
    points = []
    for index, (lat, long, label, pointId) in enumerate(zip(lats, longs, labels, ids)):
        lat, long = _number(lat), _number(long)
        if lat is None or long is None or not -90 <= lat <= 90 or not -180 <= long <= 180:
            continue
        points.append(
            {
                "id": _text(pointId) or str(index),
                "lat": lat,
                "long": long,
                "label": _text(label) or f"{lat}, {long}",
            }
        )
    return {
        "chartType": "geoMap",
        "title": request.title,
        "map": {"mapType": "scatterMap", "data": {"points": points}},
    }


def buildChart(request: ChartRequest, table: pa.Table, truncated: bool = False, lenient: bool = False) -> dict:
    """
    Builds the client chart dictionary from the query result.

    Args:
        request (ChartRequest): The chart the model asked for.
        table (pa.Table): The query result.
        truncated (bool): The launcher cut the result at its row cap.
        lenient (bool): For re-runs of saved chart requests, where no model is
            left to repair them: a category chart keeps its first LABEL_CAP
            labels instead of raising when it has too many labels or the rows
            were truncated. Nothing else is relaxed.

    Returns:
        dict: One of the chart shapes the client reads.

    Raises:
        ChartBuildError: The result cannot be drawn; the message names the fix.
    """
    chartType = request.chartType
    for name in [*requiredColumns(request), *(request.encoding.columns or [])]:
        _checkColumn(table, name)
    if table.num_rows == 0:
        if chartType in AXIS_TYPES or chartType in POINT_TYPES:
            return emptyChart(chartType, request.title, *_axisLabels(request))
        chart = emptyChart(chartType, request.title)
        if chartType == "card":
            chart["label"] = _cardLabel(request)
        return chart
    if chartType in CATEGORY_TYPES:
        return _buildCategory(request, table, truncated, lenient)
    if chartType in POINT_TYPES:
        return _buildPoints(request, table)
    if chartType == "card":
        return _buildCard(request, table)
    if chartType == "table":
        return _buildTable(request, table)
    if chartType == "pivot":
        return _buildPivot(request, table, truncated)
    return _buildGeoMap(request, table)
