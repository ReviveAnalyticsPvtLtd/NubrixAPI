"""
periodProbe.py

Finds the date bounds counted back from today that filter the rows a chart query is built from, and builds the probe
query that asks whether each bound's table ends before that date. The SQL chart workflow runs the probe when a chart
came back without data, and asks the user which period to chart when every bound's data ended before its period.
Nothing here runs a query.

A bound counts only when it provably filters the chart's own rows:
- it is `col >= expr`, `col > expr`, `expr <= col`, `expr < col` or `col BETWEEN expr AND ...`, where `expr` holds a
  current-date function and nothing that differs by row;
- it is a top-level AND conjunct of a WHERE clause, or of the ON of an inner join (never under OR, NOT, CASE, another
  comparison or any other expression);
- its SELECT is on the chart's data path: the outermost SELECT, or a CTE or derived table reached from it only
  through FROM and inner or cross joins, recursively. One outer join counts too: a LEFT join that fills a list of
  periods, where every other source of that SELECT reads no table (expressions, VALUES, unnest(generate_series(...))
  in a select list), so every measure comes from the joined side; its ON conjuncts then filter that side's rows. Any
  other optional side of an outer join, IN, EXISTS and scalar subqueries, a set operation other than UNION ALL, an
  unused CTE and a projection are never on the path. In a UNION ALL every branch must hold a bound;
- `col` is one column, bare or wrapped in date_trunc, CAST or TRY_CAST, resolved in the comparison's own scope to one
  active project table: by its qualifier (an alias of that scope), by the scope's only source, or through a CTE or
  derived table (without a column list) whose matching output column resolves the same way.
Anything that cannot be shown declines the whole query, so the chart is drawn as it is.

Per bound, `max(side) IS NULL OR max(side) < (expr)` over the whole table is true exactly when no row of the table can
pass the bound, so other filters can only remove rows and never make it say "ended" wrongly. Its `last_value` is the
latest value of the bare column, which the question quotes. All bounds run as one query, one row of one-row
aggregates cross-joined.
"""

__all__ = ["MAX_BOUNDS", "PeriodProbe", "chartHasNoData", "combinedProbeSql", "findPeriodProbes"]

import string
from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

_DIALECT = "duckdb"
_MAX_DEPTH = 16
# More bounds than this make the combined probe too large to be worth a run; the query declines.
MAX_BOUNDS = 8

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
# Nodes that stop an expression from being a bound the same for every row.
_ROW_BREAKERS = (exp.Column, exp.AggFunc, exp.Window, exp.Subquery, exp.Select, exp.Star)
# The wrappers a compared column may have: date_trunc, CAST and TRY_CAST (a Cast subclass).
_WRAPPERS = tuple(getattr(exp, name) for name in ("Cast", "TimestampTrunc", "DateTrunc") if hasattr(exp, name))
_INNER_KINDS = ("", "INNER", "CROSS")
# Join kinds that keep the FROM side's rows (or a subset of them).
_LEFT_KEEPING_KINDS = ("", "INNER", "CROSS", "OUTER", "SEMI", "ANTI")

_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@dataclass(frozen=True)
class PeriodProbe:
    """One bound: the canonical table, the bare column, the compared side and the bound as DuckDB SQL, and its probe."""

    table: str
    column: str
    side: str
    bound: str
    sql: str


class _Decline(Exception):
    """Something on the chart's data path cannot be shown; the query gets no probe."""


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
    return not any(isinstance(child, _ROW_BREAKERS) for child in node.walk())


def _innerColumn(side: exp.Expression) -> exp.Column | None:
    """The one column of a bare or wrapped (date_trunc, CAST, TRY_CAST) side, else None."""
    node = side
    while not isinstance(node, exp.Column):
        if not isinstance(node, _WRAPPERS):
            return None
        for key, value in node.args.items():
            if key != "this" and isinstance(value, exp.Expression):
                if any(isinstance(child, _ROW_BREAKERS) or _isCurrentDate(child) for child in value.walk()):
                    return None
        node = node.this
        if node is None:
            return None
    return None if isinstance(node.this, exp.Star) else node


def _conjuncts(condition: exp.Expression) -> list[exp.Expression]:
    """The top-level AND conjuncts of a condition, parentheses removed."""
    node = condition.unnest()
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _lowerBound(conjunct: exp.Expression) -> tuple[exp.Expression, exp.Expression] | None:
    """(side, bound) when the conjunct puts a current-date lower bound on its side, else None."""
    if isinstance(conjunct, (exp.GTE, exp.GT)):
        side, bound = conjunct.this, conjunct.expression
    elif isinstance(conjunct, (exp.LTE, exp.LT)):
        side, bound = conjunct.expression, conjunct.this
    elif isinstance(conjunct, exp.Between) and not conjunct.args.get("symmetric"):
        side, bound = conjunct.this, conjunct.args.get("low")
    else:
        return None
    if side is None or bound is None or not _hasCurrentDate(bound) or not _isRowFree(bound):
        return None
    return side, bound


def _hasColumnList(node: exp.Expression, source) -> bool:
    """Whether the source is renamed by a column list, on its reference or on its CTE definition."""
    aliases = [node.args.get("alias")]
    if isinstance(node.parent, exp.Subquery):
        aliases.append(node.parent.args.get("alias"))
    if isinstance(source, Scope) and isinstance(source.expression.parent, (exp.CTE, exp.Subquery)):
        aliases.append(source.expression.parent.args.get("alias"))
    return any(isinstance(alias, exp.TableAlias) and alias.columns for alias in aliases)


def _sourceOf(scope: Scope, column: exp.Column):
    """(reference node, source) a column reads in its own scope: an alias of that scope, or its only source."""
    sources = scope.selected_sources
    qualifier = column.table
    if qualifier:
        if column.args.get("db") or column.args.get("catalog"):
            return None
        matches = [pair for name, pair in sources.items() if _fold(name) == _fold(qualifier)]
        return matches[0] if len(matches) == 1 else None
    if len(sources) != 1:
        return None
    return next(iter(sources.values()))


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


def _isStar(item: exp.Expression) -> bool:
    return isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))


def _resolve(scope: Scope, column: exp.Column, activeByKey: dict[str, str], depth: int = 0):
    """(canonical table, side over that table's own columns) for a column of `scope`, or None."""
    if depth > _MAX_DEPTH:
        return None
    pair = _sourceOf(scope, column)
    if pair is None:
        return None
    node, source = pair
    if _hasColumnList(node, source):
        return None
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
        innerColumn = _innerColumn(projection)
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


def _isPreserving(join: exp.Join) -> bool:
    """Whether the rows before this join survive it as a subset (no RIGHT, FULL or POSITIONAL padding)."""
    return join.side not in ("RIGHT", "FULL") and join.kind in _LEFT_KEEPING_KINDS and join.args.get("method") in (None, "")


def _isInner(join: exp.Join) -> bool:
    """An inner or cross join of a table or derived table: its rows are on the data path and its ON filters them."""
    return (
        join.side == ""
        and join.kind in _INNER_KINDS
        and join.args.get("method") in (None, "")
        and isinstance(join.this, (exp.Table, exp.Subquery))
    )


def _readsATable(scope: Scope, depth: int = 0) -> bool:
    """Whether a CTE or derived table reads any table, in any of its sources, subqueries or the CTEs it reads."""
    if depth > _MAX_DEPTH:
        return True
    for inner in scope.traverse():
        for _, source in inner.selected_sources.values():
            if isinstance(source, exp.Table):
                return True
            if isinstance(source, Scope) and source is not inner and _readsATable(source, depth + 1):
                return True
    return False


def _isFilledIn(scope: Scope, select: exp.Select, join: exp.Join) -> bool:
    """A LEFT join that fills a list of periods with data: every other source of the SELECT reads no table (only
    expressions, VALUES or unnest(generate_series(...)) in a select list), so every measure comes from the joined side
    and its rows are the chart's data."""
    if join.side != "LEFT" or join.kind not in ("", "OUTER") or join.args.get("method") not in (None, ""):
        return False
    if not isinstance(join.this, (exp.Table, exp.Subquery)):
        return False
    fromNode = select.args.get("from_") or select.args.get("from")
    others = [fromNode.this] if fromNode is not None else []
    others += [other.this for other in select.args.get("joins") or [] if other is not join]
    if not others:
        return False
    for node in others:
        pair = scope.selected_sources.get(node.alias_or_name)
        source = pair[1] if pair else None
        if not isinstance(source, Scope) or _readsATable(source):
            return False
    return True


def _alwaysOneRow(select: exp.Select) -> bool:
    """An aggregate with no GROUP BY gives one row even from no rows, so its emptiness does not reach its reader."""
    if select.args.get("group"):
        return False
    return any(
        isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Select) is select
        for item in select.selects
        for node in item.walk()
    )


def _scopeBounds(scope: Scope, activeByKey: dict[str, str], depth: int) -> list[tuple[str, exp.Expression, exp.Expression]]:
    """(table, side, bound) for every bound on the data path from this scope; raises _Decline when one can't be shown."""
    if depth > _MAX_DEPTH:
        raise _Decline()
    expression = scope.expression
    if isinstance(expression, exp.SetOperation):
        if not isinstance(expression, exp.Union) or expression.args.get("distinct") is not False:
            return []
        bounds = []
        for branch in scope.set_operation_scopes:
            found = _scopeBounds(branch, activeByKey, depth + 1)
            if not found:
                raise _Decline()
            bounds.extend(found)
        return bounds
    if not isinstance(expression, exp.Select):
        return []

    where = expression.args.get("where")
    conditions = [where.this] if where is not None else []
    joins = expression.args.get("joins") or []
    pathNodes = []
    if all(_isPreserving(join) for join in joins):
        fromNode = expression.args.get("from_") or expression.args.get("from")
        if fromNode is not None:
            pathNodes.append(fromNode.this)
        for join in joins:
            if _isInner(join) or _isFilledIn(scope, expression, join):
                pathNodes.append(join.this)
                if join.args.get("on") is not None:
                    conditions.append(join.args["on"])

    bounds = []
    for condition in conditions:
        for conjunct in _conjuncts(condition):
            found = _lowerBound(conjunct)
            if found is None:
                continue
            side, bound = found
            column = _innerColumn(side)
            resolved = _resolve(scope, column, activeByKey) if column is not None else None
            if resolved is None:
                raise _Decline()
            table, replacement = resolved
            bounds.append((table, _replaced(side, column, replacement), bound))

    for node in pathNodes:
        pair = scope.selected_sources.get(node.alias_or_name)
        source = pair[1] if pair else None
        if isinstance(source, Scope):
            inner = source.expression
            if isinstance(inner, exp.Select) and _alwaysOneRow(inner):
                continue
            bounds.extend(_scopeBounds(source, activeByKey, depth + 1))
    return bounds


def _tableSql(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=_DIALECT)


def _probeSql(table: str, column: str, side: str, bound: str) -> str:
    return (
        f"SELECT CAST(max({column}) AS VARCHAR) AS last_value, "
        f"(max({side}) IS NULL OR max({side}) < ({bound})) AS ended_before FROM {_tableSql(table)}"
    )


def findPeriodProbes(sql: str, activeTables: Iterable[str]) -> list[PeriodProbe]:
    """One probe per distinct current-date bound that filters the chart's own rows.

    Empty when there is none, when any such bound cannot be shown to filter them or resolved to an active table,
    when there are more than MAX_BOUNDS, or when the SQL does not parse.
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
        if rootScope is None:
            return []
        bounds = _scopeBounds(rootScope, activeByKey, 0)
    except (_Decline, SqlglotError, RecursionError, ValueError):
        return []

    probes: list[PeriodProbe] = []
    seen: set[str] = set()
    for table, side, bound in bounds:
        column = _innerColumn(side)
        columnSql, sideSql, boundSql = column.sql(dialect=_DIALECT), side.sql(dialect=_DIALECT), bound.sql(dialect=_DIALECT)
        probeSql = _probeSql(table, columnSql, sideSql, boundSql)
        if probeSql not in seen:
            seen.add(probeSql)
            probes.append(PeriodProbe(table=table, column=columnSql, side=sideSql, bound=boundSql, sql=probeSql))
    return probes if len(probes) <= MAX_BOUNDS else []


def combinedProbeSql(probes: list[PeriodProbe]) -> str:
    """One query for every probe: a row with `last_value_<n>` and `ended_before_<n>` per probe, numbered from 1."""
    columns = ", ".join(
        f"p{number}.last_value AS last_value_{number}, p{number}.ended_before AS ended_before_{number}"
        for number in range(1, len(probes) + 1)
    )
    sources = " CROSS JOIN ".join(f"({probe.sql}) AS p{number}" for number, probe in enumerate(probes, start=1))
    return f"SELECT {columns} FROM {sources}"


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
