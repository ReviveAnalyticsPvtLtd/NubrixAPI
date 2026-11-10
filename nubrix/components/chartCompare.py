"""Compares two charts for the shadow run: does the SQL chart carry the same data as the Python one?

Only the data is compared, never titles or axis names, which the two models word differently.
Both charts are the `finalOutput` of a successful run.
"""

__all__ = ["compareCharts"]

import math
import re
from typing import Any, Literal

_ABS_TOL = 1e-12
_MIDNIGHT = re.compile(r"[ T]00:00:00$")


def _isNumber(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _isIntegral(value: Any) -> bool:
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def _norm(value: Any) -> Any:
    """A scalar in comparable form: missing values (None, NaN) are None, numeric text is its number, midnight dates are dates."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str):
        text = _MIDNIGHT.sub("", value.strip())
        try:
            number = float(text)
        except ValueError:
            return text
        return number if math.isfinite(number) else text
    return value


def _key(value: Any) -> tuple:
    """A sort key that puts equal data next to each other, whatever the order it arrived in."""
    value = _norm(value)
    if isinstance(value, dict):
        return (3, tuple(sorted((str(k), _key(v)) for k, v in value.items())), "")
    if isinstance(value, (list, tuple)):
        return (4, tuple(_key(v) for v in value), "")
    if value is None:
        return (0, 0, "")
    if _isNumber(value):
        return (1, float(f"{value:.6g}"), "")
    return (2, 0, str(value))


def _same(a: Any, b: Any, relTol: float) -> bool:
    """Equal values: missing with missing, integral numbers exactly, other numbers within `relTol`, containers item by item."""
    if isinstance(a, dict) and isinstance(b, dict):
        keysA = {str(k): v for k, v in a.items()}
        keysB = {str(k): v for k, v in b.items()}
        return keysA.keys() == keysB.keys() and all(_same(keysA[k], keysB[k], relTol) for k in keysA)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y, relTol) for x, y in zip(a, b))
    a, b = _norm(a), _norm(b)
    if a is None or b is None:
        return a is None and b is None
    if _isNumber(a) and _isNumber(b):
        if _isIntegral(a) and _isIntegral(b):
            return a == b
        return math.isclose(a, b, rel_tol=relTol, abs_tol=_ABS_TOL)
    return a == b


def _sameMultiset(a: list, b: list, relTol: float) -> bool:
    """The same items in any order: both sides sorted by a normalised key, then compared pairwise."""
    return len(a) == len(b) and all(_same(x, y, relTol) for x, y in zip(sorted(a, key=_key), sorted(b, key=_key)))


def _text(value: Any) -> str:
    return "" if value is None else str(_norm(value)) if isinstance(value, str) else str(value).strip()


def _labelKeys(chart: dict) -> list:
    labels = (chart.get("data") or {}).get("labels") or []
    return [_key(label) for label in labels]


def _datasets(chart: dict) -> list:
    return (chart.get("data") or {}).get("datasets") or []


def _pairDatasets(a: dict, b: dict, sameData) -> bool:
    """Datasets are matched by label, in any order; `sameData(datasetA, datasetB)` says whether two carry the same data."""
    datasetsA, datasetsB = _datasets(a), _datasets(b)
    if len(datasetsA) != len(datasetsB):
        return False
    byLabel: dict[str, list[dict]] = {}
    for dataset in datasetsB:
        byLabel.setdefault(_text(dataset.get("label")), []).append(dataset)
    for dataset in datasetsA:
        candidates = byLabel.get(_text(dataset.get("label")))
        if not candidates:
            return False
        match = next((i for i, other in enumerate(candidates) if sameData(dataset, other)), None)
        if match is None:
            return False
        candidates.pop(match)
    return True


def _byLabel(keys: list, dataset: dict) -> dict | None:
    values = dataset.get("data") or []
    return dict(zip(keys, values)) if len(values) == len(keys) else None


def _sameCategory(a: dict, b: dict, relTol: float) -> bool:
    """Labels and values as a mapping label -> value, so order does not count; duplicate labels compare as ordered lists."""
    keysA, keysB = _labelKeys(a), _labelKeys(b)
    if len(keysA) != len(keysB):
        return False
    if len(set(keysA)) != len(keysA) or len(set(keysB)) != len(keysB):
        return keysA == keysB and _pairDatasets(a, b, lambda x, y: _same(x.get("data"), y.get("data"), relTol))
    if set(keysA) != set(keysB):
        return False

    def sameData(x: dict, y: dict) -> bool:
        mapA, mapB = _byLabel(keysA, x), _byLabel(keysB, y)
        return mapA is not None and mapB is not None and all(_same(mapA[k], mapB[k], relTol) for k in mapA)

    return _pairDatasets(a, b, sameData)


def _sameScatter(a: dict, b: dict, relTol: float) -> bool:
    return _pairDatasets(a, b, lambda x, y: _sameMultiset(x.get("data") or [], y.get("data") or [], relTol))


def _sameTable(a: dict, b: dict, relTol: float) -> bool:
    return _sameMultiset(a.get("data") or [], b.get("data") or [], relTol)


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
            - category charts: each dataset agrees as a mapping label -> value, so the order of labels does not count;
              datasets pair by label in any order; a duplicate label falls back to ordered lists;
            - scatter and bubble: datasets agree by label, points as a multiset;
            - card: the value agrees;
            - table: rows agree as a multiset, in any order;
            - pivot: every key and value agrees;
            - geoMap: the set of (lat, long, label) agrees to 6 decimals.
        Missing values (None, NaN) agree with each other. Two integral numbers agree only when equal, other numbers
        within `relTol`. Numeric text equals its number, and a label ending in a midnight time equals the bare date.
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
        return _sameScatter(a, b, relTol)
    return _sameCategory(a, b, relTol)
