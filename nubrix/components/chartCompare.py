"""Compares two charts for the shadow run: does the SQL chart carry the same data as the Python one?

Only the data is compared, never titles or axis names, which the two models word differently.
Both charts are the `finalOutput` of a successful run.
"""

__all__ = ["compareCharts"]

import math
from typing import Any, Literal

_ABS_TOL = 1e-12


def _isNumber(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(a: Any, b: Any, relTol: float) -> bool:
    """Equal values: None with None, numbers within `relTol`, text after trimming, containers item by item."""
    if a is None or b is None:
        return a is None and b is None
    if _isNumber(a) and _isNumber(b):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=relTol, abs_tol=_ABS_TOL)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key], relTol) for key in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, relTol) for x, y in zip(a, b))
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    return a == b


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sameLabels(a: dict, b: dict) -> bool:
    labelsA = (a.get("data") or {}).get("labels") or []
    labelsB = (b.get("data") or {}).get("labels") or []
    return [_text(x) for x in labelsA] == [_text(x) for x in labelsB]


def _sameDatasets(a: dict, b: dict, relTol: float) -> bool:
    """Datasets are matched by label, in any order; the data inside each is compared in order."""
    datasetsA = (a.get("data") or {}).get("datasets") or []
    datasetsB = (b.get("data") or {}).get("datasets") or []
    if len(datasetsA) != len(datasetsB):
        return False
    byLabel: dict[str, list[dict]] = {}
    for dataset in datasetsB:
        byLabel.setdefault(_text(dataset.get("label")), []).append(dataset)
    for dataset in datasetsA:
        candidates = byLabel.get(_text(dataset.get("label")))
        if not candidates:
            return False
        match = next((i for i, other in enumerate(candidates) if _same(dataset.get("data"), other.get("data"), relTol)), None)
        if match is None:
            return False
        candidates.pop(match)
    return True


def _normalisedRow(row: Any, relTol: float) -> Any:
    """A table row as text: numbers print with 6 significant digits, so an int and an equal float agree."""
    if not isinstance(row, dict):
        return _text(row)
    return {_text(key): (None if value is None else f"{value:.6g}" if _isNumber(value) else _text(value)) for key, value in row.items()}


def _sameTable(a: dict, b: dict, relTol: float) -> bool:
    rowsA = a.get("data") or []
    rowsB = b.get("data") or []
    return len(rowsA) == len(rowsB) and all(_normalisedRow(x, relTol) == _normalisedRow(y, relTol) for x, y in zip(rowsA, rowsB))


def _points(chart: dict) -> set | None:
    points = (((chart.get("map") or {}).get("data") or {}).get("points"))
    if not isinstance(points, list):
        return None
    try:
        return {(round(float(p["lat"]), 6), round(float(p["long"]), 6), _text(p.get("label"))) for p in points}
    except (KeyError, TypeError, ValueError):
        return None


def compareCharts(a: dict, b: dict, relTol: float = 1e-6) -> Literal["match", "differ"]:
    """
    Says whether two charts hold the same data.

    Args:
        a (dict): A chart as returned in `finalOutput`.
        b (dict): Another chart of the same shape.
        relTol (float): The relative tolerance for numbers.

    Returns:
        Literal["match", "differ"]: "match" when the chart type is the same and:
            - category charts: labels agree in order, datasets agree by label in any order;
            - scatter and bubble: datasets agree by label, points in order;
            - card: the value agrees;
            - table: rows agree as text, in order;
            - pivot: every key and value agrees;
            - geoMap: the set of (lat, long, label) agrees to 6 decimals.
        Numbers agree within `relTol`, and null only equals null.
    """
    try:
        return "match" if _compare(a, b, relTol) else "differ"
    except Exception:
        return "differ"


def _compare(a: dict, b: dict, relTol: float) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    chartType = a.get("chartType")
    if chartType != b.get("chartType"):
        return False
    if chartType == "card":
        return _same(a.get("data"), b.get("data"), relTol)
    if chartType == "table":
        return _sameTable(a, b, relTol)
    if chartType == "pivot":
        return _same(a.get("data"), b.get("data"), relTol)
    if chartType == "geoMap":
        pointsA, pointsB = _points(a), _points(b)
        return pointsA is not None and pointsA == pointsB
    if chartType in ("scatter", "bubble"):
        return _sameDatasets(a, b, relTol)
    return _sameLabels(a, b) and _sameDatasets(a, b, relTol)
