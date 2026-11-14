"""
chartContract.py

Checks a chart dictionary against the shapes the client reads for each chart
type. An empty result means the chart matches. It never raises on malformed
input.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["contractErrors"]

import math
import re

CATEGORY_AXIS_TYPES = ("bar", "line", "area", "histogram")
SINGLE_DATASET_TYPES = ("pie", "doughnut", "polarArea")
POINT_KEYS = {"scatter": ("x", "y"), "bubble": ("x", "y", "r")}


def _isNumber(value: object) -> bool:
    """True for a finite int or float (not bool, NaN or infinity)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _isNumberOrNull(value: object) -> bool:
    return value is None or _isNumber(value)


def _isJsonSafe(value: object) -> bool:
    """True for str, bool, None, int, finite float, or lists and dicts of those."""
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_isJsonSafe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _isJsonSafe(item) for key, item in value.items())
    return False


def _isStrList(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _checkAxisLabels(chart: dict, errors: list[str]) -> None:
    for key in ("xLabels", "yLabels"):
        if not isinstance(chart.get(key), str):
            errors.append(f"{key} must be a string")


def _checkCategoryData(chart: dict, errors: list[str]) -> None:
    data = chart.get("data")
    if not isinstance(data, dict):
        errors.append("data must be an object")
        return
    if not _isStrList(data.get("labels")):
        errors.append("data.labels must be a list of strings")
    datasets = data.get("datasets")
    if not isinstance(datasets, list):
        errors.append("data.datasets must be a list")
        return
    if chart["chartType"] in SINGLE_DATASET_TYPES and len(datasets) != 1:
        errors.append(f"{chart['chartType']} needs exactly one dataset, got {len(datasets)}")
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, dict):
            errors.append(f"datasets[{index}] must be an object")
            continue
        if not isinstance(dataset.get("label"), str):
            errors.append(f"datasets[{index}].label must be a string")
        values = dataset.get("data")
        if not isinstance(values, list) or not all(_isNumberOrNull(value) for value in values):
            errors.append(f"datasets[{index}].data must be a list of numbers or null")


def _checkPointData(chart: dict, errors: list[str]) -> None:
    keys = POINT_KEYS[chart["chartType"]]
    data = chart.get("data")
    datasets = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(datasets, list):
        errors.append("data.datasets must be a list")
        return
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, dict):
            errors.append(f"datasets[{index}] must be an object")
            continue
        if not isinstance(dataset.get("label"), str):
            errors.append(f"datasets[{index}].label must be a string")
        points = dataset.get("data")
        if not isinstance(points, list):
            errors.append(f"datasets[{index}].data must be a list of points")
            continue
        for pointIndex, point in enumerate(points):
            where = f"datasets[{index}].data[{pointIndex}]"
            if not isinstance(point, dict) or set(point) != set(keys):
                errors.append(f"{where} must have exactly the keys {list(keys)}")
            elif not all(_isNumber(point[key]) for key in keys):
                errors.append(f"{where} values must be finite numbers")


def _checkCard(chart: dict, errors: list[str]) -> None:
    if not isinstance(chart.get("label"), str):
        errors.append("label must be a string")
    data = chart.get("data")
    if not (_isNumber(data) or isinstance(data, str)):
        errors.append("data must be a number or a string")


def _checkTable(chart: dict, errors: list[str]) -> None:
    columns = chart.get("columns")
    if not _isStrList(columns):
        errors.append("columns must be a list of strings")
        return
    rows = chart.get("data")
    if not isinstance(rows, list):
        errors.append("data must be a list of rows")
        return
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"data[{index}] must be an object")
        elif list(row) != columns:
            errors.append(f"data[{index}] keys must equal columns, in the same order")
        elif not _isJsonSafe(row):
            errors.append(f"data[{index}] must hold only JSON-safe values")


def _pivotLevels(key: str) -> list[str] | None:
    """The levels the client reads from a tuple-text column key; None when it misreads the key."""
    tupleMatch = re.search(r"\(([^)]+)\)", key)
    if not tupleMatch or tupleMatch.group(0) != key:
        return None
    levels = re.findall(r"'([^']+)'", tupleMatch.group(1))
    if not levels or tupleMatch.group(1) != ", ".join(f"'{level}'" for level in levels):
        return None
    return levels


def _checkPivot(chart: dict, errors: list[str]) -> None:
    data = chart.get("data")
    if not isinstance(data, dict):
        errors.append("data must be an object")
        return
    depths = set()
    for columnKey in data:
        if columnKey.startswith("("):
            levels = _pivotLevels(columnKey)
            if levels is None:
                errors.append(f"data['{columnKey}'] is a column key the client would misread")
            else:
                depths.add(len(levels))
    if len(depths) > 1:
        errors.append("multi-level pivot column keys must all have the same depth")
    for columnKey, column in data.items():
        if not isinstance(column, dict):
            errors.append(f"data['{columnKey}'] must be an object keyed by row")
        elif not all(_isNumberOrNull(value) for value in column.values()):
            errors.append(f"data['{columnKey}'] values must be numbers or null")


def _checkGeoMap(chart: dict, errors: list[str]) -> None:
    if "data" in chart:
        errors.append("geoMap must not have a data key")
    mapBlock = chart.get("map")
    if not isinstance(mapBlock, dict):
        errors.append("map must be an object")
        return
    if mapBlock.get("mapType") != "scatterMap":
        errors.append("map.mapType must be 'scatterMap'")
    mapData = mapBlock.get("data")
    points = mapData.get("points") if isinstance(mapData, dict) else None
    if not isinstance(points, list):
        errors.append("map.data.points must be a list")
        return
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            errors.append(f"points[{index}] must be an object")
            continue
        if not isinstance(point.get("id"), str):
            errors.append(f"points[{index}].id must be a string")
        if not _isNumber(point.get("lat")) or not _isNumber(point.get("long")):
            errors.append(f"points[{index}] lat and long must be numbers")
        label = point.get("label")
        if not isinstance(label, str) or not label:
            errors.append(f"points[{index}].label must be a non-empty string")


def contractErrors(chart: dict) -> list[str]:
    """
    Lists the ways a chart differs from the shape the client reads.

    Args:
        chart (dict): A chart as returned in `finalOutput`.

    Returns:
        list[str]: One message per violation; empty when the chart matches.
    """
    if not isinstance(chart, dict):
        return ["chart must be an object"]
    chartType = chart.get("chartType")
    if not isinstance(chartType, str):
        return ["chartType must be a string"]
    errors: list[str] = []
    # The geoMap panel template has never carried a title.
    titleOptional = chartType == "geoMap" and "title" not in chart
    if not titleOptional and not isinstance(chart.get("title"), str):
        errors.append("title must be a string")

    if chartType in CATEGORY_AXIS_TYPES:
        _checkAxisLabels(chart, errors)
        _checkCategoryData(chart, errors)
    elif chartType == "radar" or chartType in SINGLE_DATASET_TYPES:
        _checkCategoryData(chart, errors)
    elif chartType in POINT_KEYS:
        _checkAxisLabels(chart, errors)
        _checkPointData(chart, errors)
    elif chartType == "card":
        _checkCard(chart, errors)
    elif chartType == "table":
        _checkTable(chart, errors)
    elif chartType == "pivot":
        _checkPivot(chart, errors)
    elif chartType == "geoMap":
        _checkGeoMap(chart, errors)
    else:
        errors.append(f"unknown chartType '{chartType}'")
    return errors
