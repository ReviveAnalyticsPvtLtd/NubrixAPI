"""Compares two charts for the shadow run: does the SQL chart carry the same data as the Python one?

Only the data is compared, never titles or axis names, which the two models word differently.
Both charts are the `finalOutput` of a successful run.
"""

__all__ = ["compareCharts"]

import itertools
import math
import re
from typing import Any, Literal

_ABS_TOL = 1e-12
_MIDNIGHT = re.compile(r"[ T]00:00:00$")
_WHOLE = re.compile(r"[+-]?\d+$")
# Calls of `_same` that the greedy fallback may make in one comparison; past it the answer is "differ".
_GREEDY_BUDGET = 200_000
# Column matching for tables: columns sharing one signature are tried in permutations, within these bounds.
_MAX_TIED_COLUMNS = 8
_MAX_COLUMN_MAPPINGS = 720


class _BudgetExhausted(Exception):
    pass


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
        if _WHOLE.match(text):
            return int(text)
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
        if _isIntegral(value):
            return (1, int(value), "")
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


def _exactKey(value: Any) -> tuple:
    """An unrounded, type-tagged sort key: integral numbers as ints, other floats as floats, text as text, missing first."""
    value = _norm(value)
    if isinstance(value, dict):
        return (3, tuple(sorted((str(k), _exactKey(v)) for k, v in value.items())), "")
    if isinstance(value, (list, tuple)):
        return (4, tuple(_exactKey(v) for v in value), "")
    if value is None:
        return (0, 0, "")
    if _isNumber(value):
        return (1, int(value) if _isIntegral(value) else float(value), "")
    return (2, 0, str(value))


def _sameMultiset(a: list, b: list, relTol: float, budget: list) -> bool:
    """
    The same items in any order: both sides sorted by a normalised key, then the items that tie on the key are paired.

    Ties are paired in exact (unrounded) order first, which is correct for identical data in any arrival order and costs
    O(n log n). Only the items this exact-order pairing leaves unmatched go to a greedy tolerance matching, which has a fixed budget of `_same`
    calls (`_GREEDY_BUDGET`, shared by the whole comparison). A comparison that runs out of budget answers "differ", so
    pathological data (many tolerance classes inside one key bucket, not in identical order) can give a "differ" for
    charts that carry the same data.
    """
    if len(a) != len(b):
        return False
    groupsA, groupsB = _groups(a), _groups(b)
    if [k for k, _ in groupsA] != [k for k, _ in groupsB]:
        # Rounded keys can differ for values that agree within the tolerance: fall back to one pool.
        return _pairOff(a, b, relTol, budget)
    return all(_pairOff(x, y, relTol, budget) for (_, x), (_, y) in zip(groupsA, groupsB))


def _groups(items: list) -> list:
    groups: list = []
    for item in sorted(items, key=_key):
        key = _key(item)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(item)
        else:
            groups.append((key, [item]))
    return groups


def _pairOff(a: list, b: list, relTol: float, budget: list) -> bool:
    """Pairs the items in exact order, then pairs what is left greedily within the budget of `_same` calls."""
    if len(a) != len(b):
        return False
    a, b = sorted(a, key=_exactKey), sorted(b, key=_exactKey)
    leftA, leftB = [], []
    for x, y in zip(a, b):
        if not _same(x, y, relTol):
            leftA.append(x)
            leftB.append(y)
    for x in leftA:
        index = None
        for i, y in enumerate(leftB):
            budget[0] -= 1
            if budget[0] < 0:
                raise _BudgetExhausted
            if _same(x, y, relTol):
                index = i
                break
        if index is None:
            return False
        leftB.pop(index)
    return True


def _text(value: Any) -> str:
    return "" if value is None else str(_norm(value)) if isinstance(value, str) else str(value).strip()


def _labelKeys(chart: dict) -> list:
    labels = (chart.get("data") or {}).get("labels") or []
    return [_key(label) for label in labels]


def _datasets(chart: dict) -> list:
    return (chart.get("data") or {}).get("datasets") or []


def _pairDatasets(a: dict, b: dict, sameData) -> bool:
    """
    Datasets are matched by label, in any order; `sameData(datasetA, datasetB)` says whether two carry the same data.

    With exactly one dataset on each side the label is only the model's wording of the measure, so it is ignored.
    """
    datasetsA, datasetsB = _datasets(a), _datasets(b)
    if len(datasetsA) != len(datasetsB):
        return False
    if len(datasetsA) == 1:
        return sameData(datasetsA[0], datasetsB[0])
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


def _sameScatter(a: dict, b: dict, relTol: float, budget: list) -> bool:
    return _pairDatasets(a, b, lambda x, y: _sameMultiset(x.get("data") or [], y.get("data") or [], relTol, budget))


def _columns(chart: dict) -> list:
    """Column names in first-seen order: the declared `columns`, then any key a row adds."""
    names: dict = {}
    for name in chart.get("columns") or []:
        names.setdefault(name, None)
    for row in chart.get("data") or []:
        if isinstance(row, dict):
            for name in row:
                names.setdefault(name, None)
    return list(names)


def _signature(rows: list, column: Any) -> tuple:
    return tuple(sorted(_key(row.get(column)) for row in rows))


def _columnMappings(colsA: list, colsB: list, rowsA: list, rowsB: list):
    """
    Yields mappings (a list of B columns, one per A column) that pair columns with equal value signatures.

    Columns with a unique signature pair directly; a group of columns sharing a signature is permuted. A group of more
    than `_MAX_TIED_COLUMNS` or more than `_MAX_COLUMN_MAPPINGS` mappings in all yields nothing further.
    """
    groupsA: dict = {}
    groupsB: dict = {}
    for col in colsA:
        groupsA.setdefault(_signature(rowsA, col), []).append(col)
    for col in colsB:
        groupsB.setdefault(_signature(rowsB, col), []).append(col)
    if groupsA.keys() != groupsB.keys() or any(len(groupsA[s]) != len(groupsB[s]) for s in groupsA):
        return
    signatures = list(groupsA)
    if any(len(groupsA[s]) > _MAX_TIED_COLUMNS for s in signatures):
        return
    choices = [list(itertools.permutations(groupsB[s])) for s in signatures]
    for picked in itertools.islice(itertools.product(*choices), _MAX_COLUMN_MAPPINGS):
        target = {}
        for s, perm in zip(signatures, picked):
            target.update(zip(groupsA[s], perm))
        yield [target[col] for col in colsA]


def _sameTable(a: dict, b: dict, relTol: float, budget: list) -> bool:
    """
    Rows as a multiset. Columns are matched by their values, not their names.

    Same column names: the rows compare as dicts, as before. Otherwise the column count must agree and columns pair by
    the multiset of their values; each candidate pairing is checked with the multiset of whole rows. Past the bounds on
    tied columns and mappings the comparison falls back to names, which cannot match differently named columns.
    """
    rowsA, rowsB = a.get("data") or [], b.get("data") or []
    colsA, colsB = _columns(a), _columns(b)
    if len(colsA) != len(colsB):
        return False
    if set(colsA) == set(colsB):
        return _sameMultiset(rowsA, rowsB, relTol, budget)
    if len(rowsA) != len(rowsB):
        return False
    tuplesA = [tuple(row.get(c) for c in colsA) for row in rowsA]
    for mapping in _columnMappings(colsA, colsB, rowsA, rowsB):
        tuplesB = [tuple(row.get(c) for c in mapping) for row in rowsB]
        if _sameMultiset(tuplesA, tuplesB, relTol, budget):
            return True
    return False


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
              datasets pair by label in any order (one dataset per side ignores its label); a duplicate label falls back to ordered lists;
            - scatter and bubble: datasets agree by label (a single dataset on each side ignores its label), points as a multiset;
            - card: the value agrees;
            - table: rows agree as a multiset, in any order, with columns matched by their values, not their names;
            - pivot: every key and value agrees;
            - geoMap: the set of (lat, long, label) agrees to 6 decimals.
        Missing values (None, NaN) agree with each other. Two integral numbers agree only when equal, other numbers
        within `relTol`. Numeric text equals its number, and a label ending in a midnight time equals the bare date.
        Table and scatter rows that tie on a rounded key are paired in exact order first, then greedily within a fixed
        budget of `_same` calls; pathological data can exhaust it, and that gives "differ".
    """
    try:
        return "match" if _compare(a, b, relTol) else "differ"
    except Exception:  # includes _BudgetExhausted: a budget-exhausted "differ" is possible for pathological data
        return "differ"


def _compare(a: dict, b: dict, relTol: float) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    chartType = a.get("chartType")
    if chartType != b.get("chartType"):
        return False
    if chartType == "card":
        return _same(a.get("data"), b.get("data"), relTol)
    budget = [_GREEDY_BUDGET]
    if chartType == "table":
        return _sameTable(a, b, relTol, budget)
    if chartType == "pivot":
        return _same(a.get("data"), b.get("data"), relTol)
    if chartType == "geoMap":
        pointsA, pointsB = _points(a), _points(b)
        return pointsA is not None and pointsA == pointsB
    if chartType in ("scatter", "bubble"):
        return _sameScatter(a, b, relTol, budget)
    return _sameCategory(a, b, relTol)
