"""
periodProbe.py

Finds the places where a chart query starts its period at a date counted back from today, and builds one probe
query per place that asks whether the table's data ends before that date. The SQL chart workflow runs the probes
when a chart came back without data, and asks the user which period to chart when every probe says the data ended
before the period. Nothing here runs a query.

A place counts when a comparison puts a lower bound that depends on the current date on one column:
`col >= expr`, `col > expr`, `expr <= col`, `expr < col` or `col BETWEEN expr AND ...`. `col` may be wrapped in
functions (a CAST, a date_trunc) but holds exactly one column and no aggregate; `expr` holds a current-date function
and no column. The column must resolve to one project table: through its qualifier, through the single source of
its scope, or through a CTE or derived table whose matching output column resolves the same way.

The probe, `max(col) IS NULL OR max(col) < (expr)` over the whole table, is true exactly when no row of the table can
satisfy `col >= expr`, so a filtered CTE body or other conditions in the query never make it say "ended" wrongly.
"""

__all__ = ["PeriodProbe", "chartHasNoData", "findPeriodProbes"]

import string
from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

_DIALECT = "duckdb"
_MAX_DEPTH = 16

# The current date or time, in any spelling sqlglot parses. CURRENT_TIME is a time of day, not a date, so it is left out.
_CURRENT_NODES = tuple(
    getattr(exp, name)
    for name in ("CurrentDate", "CurrentTimestamp", "CurrentDatetime", "CurrentTimestampLTZ", "Localtimestamp")
    if hasattr(exp, name)
)
_CURRENT_NAMES = frozenset(
    {
        "now",
        "today",
        "get_current_timestamp",
        "transaction_timestamp",
        "current_date",
        "current_timestamp",
        "current_localtimestamp",
        "localtimestamp",
    }
)
# Nodes that stop an expression from being a row's own value, or a bound that is the same for every row.
_ROW_BREAKERS = (exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Star)


@dataclass(frozen=True)
class PeriodProbe:
    """One probe: the canonical project table, the column side and the bound as DuckDB SQL, and the probe query."""

    table: str
    column: str
    bound: str
    sql: str


_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _fold(name: str) -> str:
    """Fold a name the way DuckDB matches identifiers: ASCII letters only (as the SQL check does)."""
    return str(name).translate(_ASCII_FOLD)


def _isCurrentDate(node: exp.Expression) -> bool:
    if isinstance(node, _CURRENT_NODES):
        return True
    return isinstance(node, exp.Anonymous) and _fold(node.name) in _CURRENT_NAMES


def _hasCurrentDate(node: exp.Expression) -> bool:
    return any(_isCurrentDate(child) for child in node.walk())


def _isRowFree(node: exp.Expression) -> bool:
    """A bound the same for every row: no column, aggregate, window or subquery."""
    return not any(isinstance(child, (exp.Column, *_ROW_BREAKERS)) for child in node.walk())


def _singleColumn(node: exp.Expression) -> exp.Column | None:
    """The one column of a row-level expression with no current-date function, else None."""
    columns = []
    for child in node.walk():
        if isinstance(child, _ROW_BREAKERS) or _isCurrentDate(child):
            return None
        if isinstance(child, exp.Column):
            if isinstance(child.this, exp.Star):
                return None
            columns.append(child)
    return columns[0] if len(columns) == 1 else None


def _isNegated(node: exp.Expression) -> bool:
    """Whether a NOT stands between the comparison and the query it belongs to (NOT turns a lower bound around)."""
    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Select, exp.SetOperation, exp.Subquery)):
        if isinstance(parent, exp.Not):
            return True
        parent = parent.parent
    return False


def _lowerBounds(root: exp.Expression) -> list[tuple[exp.Expression, exp.Expression]]:
    """(column side, bound) for every current-date lower bound in the query, in query order."""
    found = []
    for node in root.walk():
        if isinstance(node, (exp.GTE, exp.GT)):
            side, bound = node.this, node.expression
        elif isinstance(node, (exp.LTE, exp.LT)):
            side, bound = node.expression, node.this
        elif isinstance(node, exp.Between) and not node.args.get("symmetric"):
            side, bound = node.this, node.args.get("low")
        else:
            continue
        if side is None or bound is None or _isNegated(node):
            continue
        if _hasCurrentDate(bound) and _isRowFree(bound) and _singleColumn(side) is not None:
            found.append((side, bound))
    return found


def _sourceOf(scope: Scope, column: exp.Column):
    """The FROM or JOIN source a column reads: by its qualifier, or the scope's only source."""
    sources = scope.selected_sources
    qualifier = column.table
    if qualifier:
        if column.args.get("db") or column.args.get("catalog"):
            return None
        matches = [source for name, (_, source) in sources.items() if _fold(name) == _fold(qualifier)]
        return matches[0] if len(matches) == 1 else None
    if len(sources) != 1:
        return None
    (_, source), = sources.values()
    return source


def _replaced(expression: exp.Expression, column: exp.Column, replacement: exp.Expression) -> exp.Expression:
    """A copy of `expression` with its one column `column` replaced by a copy of `replacement`."""
    if expression is column:
        return replacement.copy()
    path = []
    node = column
    while node is not expression:
        path.append((node.arg_key, node.index))
        node = node.parent
    copy = expression.copy()
    target = copy
    for key, index in reversed(path):
        value = target.args[key]
        target = value[index] if index is not None else value
    target.replace(replacement.copy())
    return copy


def _resolve(scope: Scope, column: exp.Column, activeByKey: dict[str, str], depth: int = 0):
    """(canonical table, expression over that table's own columns) for a column of `scope`, or None."""
    if depth > _MAX_DEPTH:
        return None
    source = _sourceOf(scope, column)
    if isinstance(source, exp.Table):
        if source.args.get("db") or source.args.get("catalog") or not isinstance(source.this, exp.Identifier):
            return None
        canonical = activeByKey.get(_fold(source.name))
        if canonical is None:
            return None
        bare = column.copy()
        bare.set("table", None)
        return canonical, bare
    if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
        return None
    inner = source
    name = _fold(column.name)
    matches = [item for item in inner.expression.selects if not _isStar(item) and _fold(item.alias_or_name) == name]
    if len(matches) == 1:
        projection = matches[0].unalias() if isinstance(matches[0], exp.Alias) else matches[0]
        innerColumn = _singleColumn(projection)
        if innerColumn is None:
            return None
        resolved = _resolve(inner, innerColumn, activeByKey, depth + 1)
        if resolved is None:
            return None
        table, replacement = resolved
        return table, _replaced(projection, innerColumn, replacement)
    if matches:
        return None
    stars = [item for item in inner.expression.selects if _isStar(item)]
    if len(stars) != 1:
        return None
    star = stars[0]
    qualifier = star.table if isinstance(star, exp.Column) else None
    return _resolve(inner, exp.column(column.args["this"].copy(), table=qualifier), activeByKey, depth + 1)


def _isStar(item: exp.Expression) -> bool:
    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _tableSql(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=_DIALECT)


def _probeSql(column: str, bound: str, table: str) -> str:
    return (
        f"SELECT CAST(max({column}) AS VARCHAR) AS last_value, "
        f"(max({column}) IS NULL OR max({column}) < ({bound})) AS ended_before FROM {_tableSql(table)}"
    )


def findPeriodProbes(sql: str, activeTables: Iterable[str]) -> list[PeriodProbe]:
    """One probe per current-date lower bound whose column resolves to an active table, each probe once.

    A bound whose column cannot be resolved is skipped. SQL that does not parse gives no probe.
    """
    activeByKey: dict[str, str] = {}
    for name in activeTables:
        activeByKey.setdefault(_fold(name), str(name))
    text = sql.strip() if isinstance(sql, str) else ""
    while text.endswith(";"):
        text = text[:-1].rstrip()
    try:
        root = sqlglot.parse_one(text, read=_DIALECT)
        rootScope = build_scope(root)
    except (SqlglotError, RecursionError, ValueError):
        return []
    if rootScope is None:
        return []
    scopeOf: dict[int, Scope] = {}
    for scope in rootScope.traverse():
        for column in scope.columns:
            scopeOf[id(column)] = scope

    probes: list[PeriodProbe] = []
    seen: set[str] = set()
    for side, bound in _lowerBounds(root):
        column = _singleColumn(side)
        scope = scopeOf.get(id(column))
        if scope is None:
            continue
        resolved = _resolve(scope, column, activeByKey)
        if resolved is None:
            continue
        table, replacement = resolved
        columnSql = _replaced(side, column, replacement).sql(dialect=_DIALECT)
        boundSql = bound.sql(dialect=_DIALECT)
        probeSql = _probeSql(columnSql, boundSql, table)
        if probeSql not in seen:
            seen.add(probeSql)
            probes.append(PeriodProbe(table=table, column=columnSql, bound=boundSql, sql=probeSql))
    return probes


# The chart shapes the builder emits (chartBuilder.py).
_CATEGORY_TYPES = frozenset({"bar", "line", "area", "histogram", "radar", "pie", "doughnut", "polarArea"})
_POINT_TYPES = frozenset({"scatter", "bubble"})


def _isNothing(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0)


def chartHasNoData(chart: object) -> bool:
    """Whether a built chart shows no data: no labels, points or rows, or every value null or 0.

    A chart whose shape is not one the builder emits counts as having data, so it is never questioned.
    """
    if not isinstance(chart, dict):
        return False
    chartType = chart.get("chartType")
    data = chart.get("data")
    if chartType in _CATEGORY_TYPES:
        if not isinstance(data, dict) or not isinstance(data.get("labels"), list) or not isinstance(data.get("datasets"), list):
            return False
        if not data["labels"]:
            return True
        values = []
        for dataset in data["datasets"]:
            if not isinstance(dataset, dict) or not isinstance(dataset.get("data"), list):
                return False
            values.extend(dataset["data"])
        return all(_isNothing(value) for value in values)
    if chartType in _POINT_TYPES:
        if not isinstance(data, dict) or not isinstance(data.get("datasets"), list):
            return False
        return all(isinstance(dataset, dict) and dataset.get("data") == [] for dataset in data["datasets"])
    if chartType == "card":
        return "data" in chart and _isNothing(data)
    if chartType == "table":
        return data == []
    if chartType == "pivot":
        if not isinstance(data, dict) or not all(isinstance(column, dict) for column in data.values()):
            return False
        return all(_isNothing(value) for column in data.values() for value in column.values())
    if chartType == "geoMap":
        mapping = chart.get("map")
        mapData = mapping.get("data") if isinstance(mapping, dict) else None
        return isinstance(mapData, dict) and mapData.get("points") == []
    return False
