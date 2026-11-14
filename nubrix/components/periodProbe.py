"""
periodProbe.py

Decides whether a chart without data may be answered with a question about its period. It finds the date bounds counted
back from today in a chart query, builds the one probe query that asks, per bound, whether the bound's table ends
before that date, and, given the bounds that ended, proves or fails to prove that the chart holds no project data.
Nothing here runs a query.

A bound is a top-level AND conjunct of a WHERE, or of the ON of an inner, semi, anti or LEFT join, of the form
`col >= expr`, `col > expr`, `expr <= col`, `expr < col` or `col BETWEEN expr AND ...`. `expr` holds a current-date
function and nothing that differs by row. `col` is one column, bare or wrapped in date_trunc, CAST or TRY_CAST. It is
resolved in the comparison's own scope to one active project table: by its qualifier (an alias of that scope), by the
scope's only source, or through a CTE or derived table (without a column list) whose matching output column resolves
the same way. A conjunct that does not resolve is not a bound. Per bound, `max(side) IS NULL OR max(side) < (expr)`
over the whole table is true exactly when no row of the table can pass it ("ended"). Every value the column takes in
the scope is the side of some table row, or NULL, so an ended bound is false for every row of that scope. Its
`last_value` is the latest value of the bare column, which the question quotes. All bounds run as one query, one row
of one-row aggregates cross-joined.

Every query (each SELECT, set operation, CTE and derived table) gets one status:
- EMPTY: it gives no rows;
- DATAFREE: it may give rows, but no value in them depends on any project-table row;
- UNKNOWN: anything else.
The rules, in order:
1. A subquery expression anywhere outside a SELECT's sources (select list, WHERE, ON, HAVING, ORDER BY, VALUES,
   unnest((SELECT ...)), IN, EXISTS, scalar), or a source other than a table, CTE or derived table, that reads a table
   (directly or through CTEs, recursively) makes it UNKNOWN. One that reads no table is fine.
2. Its rows are EMPTY when a WHERE conjunct is an ended bound, whatever the sources.
3. Otherwise its rows fold its FROM and joins from left to right. A table is UNKNOWN; a CTE or derived table has its
   own status. An inner, cross or semi join is EMPTY when either side is EMPTY or an ON conjunct is an ended bound.
   A LEFT or anti join gives the left rows when the right side is EMPTY or an ON conjunct is an ended bound, and EMPTY
   when the left rows are. Otherwise (RIGHT, FULL and every other join too) the result is EMPTY only when both sides
   are, DATAFREE only when both are EMPTY or DATAFREE, and UNKNOWN otherwise.
4. EMPTY rows give an EMPTY result when the SELECT gives no row from no rows (no aggregate at all, or a plain
   GROUP BY), and DATAFREE otherwise (GROUP BY (), ROLLUP, CUBE, GROUPING SETS, HAVING, an aggregate). DATAFREE rows
   give DATAFREE, UNKNOWN rows UNKNOWN.
5. A set operation is EMPTY only when every branch is, DATAFREE only when every branch is EMPTY or DATAFREE. A CTE's
   status is computed once and every use takes it; a CTE that reads itself (recursive) is UNKNOWN.
The chart may ask only when at least one bound ended and the outermost query is EMPTY or DATAFREE. Only bounds in
queries the analysis reaches are probed (never one in a rule-1 query or an unused CTE), and only when the query could
ask if all of them ended; the analysis only gains from more ended bounds, so otherwise no answer could make it ask.
"""

__all__ = ["MAX_BOUNDS", "PeriodProbe", "chartHasNoData", "combinedProbeSql", "findPeriodProbes", "isDataFree"]

import string
from dataclasses import dataclass
from typing import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

_DIALECT = "duckdb"
_MAX_DEPTH = 16
# More bounds than this make the combined probe too large to be worth a run; the query gets no probe.
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

_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@dataclass(frozen=True)
class PeriodProbe:
    """One bound: the canonical table, the bare column, the compared side and the bound as DuckDB SQL, and its probe."""

    table: str
    column: str
    side: str
    bound: str
    sql: str


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



def _tableSql(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=_DIALECT)


def _probeSql(table: str, column: str, side: str, bound: str) -> str:
    return (
        f"SELECT CAST(max({column}) AS VARCHAR) AS last_value, "
        f"(max({side}) IS NULL OR max({side}) < ({bound})) AS ended_before FROM {_tableSql(table)}"
    )


# The statuses, ordered by how much they prove, so a combination that needs every part is their min().
_UNKNOWN, _DATAFREE, _EMPTY = 0, 1, 2
# GROUP BY items that give a row from no rows (the grand total), and the empty grouping set `()`.
_GRAND_TOTALS = (exp.Rollup, exp.Cube, exp.GroupingSets, exp.Tuple)


def _cteFor(table: exp.Table) -> exp.CTE | None:
    """The CTE a table reference reads: the nearest WITH above it that defines the name."""
    if table.args.get("db") or table.args.get("catalog") or not isinstance(table.this, exp.Identifier):
        return None
    name = _fold(table.name)
    node = table.parent
    while node is not None:
        with_ = node.args.get("with_")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                if _fold(cte.alias_or_name) == name:
                    return cte
        node = node.parent
    return None


def _readsTable(node: exp.Expression, reading: tuple = ()) -> bool:
    """Whether anything in `node` reads a table, directly or through the CTEs it reads (a CTE reading itself counts)."""
    if len(reading) > _MAX_DEPTH:
        return True
    for table in node.find_all(exp.Table):
        if isinstance(table.this, exp.GenerateSeries):
            # range() or generate_series(): its arguments are walked as part of `node`.
            continue
        cte = _cteFor(table)
        if cte is None or any(cte is outer for outer in reading) or _readsTable(cte.this, reading + (cte,)):
            return True
    return False


def _readsOutsideSources(query: exp.Expression, sources: tuple[str, ...]) -> bool:
    """Rule 1: whether a part of the query other than its sources and its WITH reads a table."""
    for key, value in query.args.items():
        if key in sources or key == "with_":
            continue
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, exp.Expression):
                continue
            if isinstance(item, exp.Join):
                parts = [part for name, part in item.args.items() if name != "this" and isinstance(part, exp.Expression)]
                if any(_readsTable(part) for part in parts):
                    return True
            elif _readsTable(item):
                return True
    return False


def _givesNoRowFromNoRows(select: exp.Select) -> bool:
    """Rule 4: a SELECT with no aggregate at all, or with a plain GROUP BY, gives no row from no rows."""
    group = select.args.get("group")
    if group is not None:
        return (
            bool(group.expressions)
            and not any(value for key, value in group.args.items() if key != "expressions")
            and not any(isinstance(item, _GRAND_TOTALS) for item in group.expressions)
        )
    if select.args.get("having") is not None:
        return False
    for key, value in select.args.items():
        if key in ("from_", "joins", "with_"):
            continue
        for item in value if isinstance(value, list) else [value]:
            # An unknown function may be an aggregate (fsum, product, ...).
            if isinstance(item, exp.Expression) and any(isinstance(node, (exp.AggFunc, exp.Anonymous)) for node in item.walk()):
                return False
    return True


class _Analysis:
    """The status of every query reached from the root, for one set of ended probes; records each bound it meets."""

    def __init__(self, rootScope: Scope, activeByKey: dict[str, str], ended: frozenset[str]):
        self.scopes = {id(scope.expression): scope for scope in rootScope.traverse()}
        self.activeByKey = activeByKey
        self.ended = ended
        self.probes: dict[str, PeriodProbe] = {}
        self.statuses: dict[int, int] = {}

    def status(self, scope: Scope, depth: int = 0) -> int:
        key = id(scope.expression)
        if key not in self.statuses:
            # Marked first, so a query that reads itself (a recursive CTE) reads UNKNOWN.
            self.statuses[key] = _UNKNOWN
            self.statuses[key] = _UNKNOWN if depth > _MAX_DEPTH else self._status(scope, depth)
        return self.statuses[key]

    def _status(self, scope: Scope, depth: int) -> int:
        query = scope.expression
        if isinstance(query, exp.SetOperation):
            if _readsOutsideSources(query, ("this", "expression")):
                return _UNKNOWN
            return min(self._query(branch, depth) for branch in (query.this, query.expression))
        if not isinstance(query, exp.Select):
            return _UNKNOWN if _readsTable(query) else _DATAFREE
        if _readsOutsideSources(query, ("from_",)):
            return _UNKNOWN
        where = query.args.get("where")
        emptied = where is not None and self._anyEnded(scope, where.this)
        fromNode = query.args.get("from_")
        rows = self._source(fromNode.this, depth) if fromNode is not None else _DATAFREE
        for join in query.args.get("joins") or []:
            rows = self._join(scope, rows, join, depth)
        if emptied or rows == _EMPTY:
            return _EMPTY if _givesNoRowFromNoRows(query) else _DATAFREE
        return rows

    def _query(self, node: exp.Expression, depth: int) -> int:
        """A set operation branch, which may sit in parentheses."""
        while isinstance(node, exp.Subquery) and not node.alias:
            node = node.this
        scope = self.scopes.get(id(node))
        if scope is not None:
            return self.status(scope, depth + 1)
        return _UNKNOWN if _readsTable(node) else _DATAFREE

    def _source(self, node: exp.Expression, depth: int) -> int:
        """Rule 3 for one source: a table is UNKNOWN, a CTE or derived table has its status, anything else reading a
        table is UNKNOWN (rule 1) and anything else is DATAFREE (unnest, range, VALUES of constants)."""
        plain = not node.args.get("joins") and not node.args.get("pivots")
        if isinstance(node, exp.Subquery) and plain and id(node.this) in self.scopes:
            return self.status(self.scopes[id(node.this)], depth + 1)
        if isinstance(node, exp.Table) and plain and isinstance(node.this, exp.Identifier):
            cte = _cteFor(node)
            if cte is None or id(cte.this) not in self.scopes:
                return _UNKNOWN
            return self.status(self.scopes[id(cte.this)], depth + 1)
        return _UNKNOWN if _readsTable(node) else _DATAFREE

    def _join(self, scope: Scope, rows: int, join: exp.Join, depth: int) -> int:
        right = self._source(join.this, depth)
        plain = join.args.get("method") in (None, "")
        inner = plain and join.side == "" and join.kind in ("", "INNER", "CROSS", "SEMI")
        keepsLeft = plain and ((join.side == "LEFT" and join.kind in ("", "OUTER")) or (join.side == "" and join.kind == "ANTI"))
        onEnded = (inner or keepsLeft) and self._anyEnded(scope, join.args.get("on"))
        if inner and (onEnded or _EMPTY in (rows, right)):
            return _EMPTY
        if keepsLeft and rows == _EMPTY:
            return _EMPTY
        if keepsLeft and (onEnded or right == _EMPTY):
            return rows
        return min(rows, right)

    def _anyEnded(self, scope: Scope, condition: exp.Expression | None) -> bool:
        """Whether a top-level AND conjunct of the condition is an ended bound; records every bound it holds."""
        if condition is None:
            return False
        ended = False
        for conjunct in _conjuncts(condition):
            probe = self._bound(scope, conjunct)
            if probe is not None:
                self.probes.setdefault(probe.sql, probe)
                ended = ended or probe.sql in self.ended
        return ended

    def _bound(self, scope: Scope, conjunct: exp.Expression) -> PeriodProbe | None:
        found = _lowerBound(conjunct)
        column = _innerColumn(found[0]) if found is not None else None
        resolved = _resolve(scope, column, self.activeByKey) if column is not None else None
        if resolved is None:
            return None
        side, bound = found
        table, replacement = resolved
        resolvedSide = _replaced(side, column, replacement)
        columnSql = _innerColumn(resolvedSide).sql(dialect=_DIALECT)
        sideSql, boundSql = resolvedSide.sql(dialect=_DIALECT), bound.sql(dialect=_DIALECT)
        probeSql = _probeSql(table, columnSql, sideSql, boundSql)
        return PeriodProbe(table=table, column=columnSql, side=sideSql, bound=boundSql, sql=probeSql)


def _analyse(sql: str, activeTables: Iterable[str], ended: Iterable[str]) -> tuple[int, list[PeriodProbe]]:
    """(the root's status, the bounds met) for one set of ended probe SQLs; (UNKNOWN, []) when the SQL does not parse."""
    activeByKey: dict[str, str] = {}
    for name in activeTables:
        activeByKey.setdefault(_fold(name), str(name))
    text = sql.strip() if isinstance(sql, str) else ""
    while text.endswith(";"):
        text = text[:-1].rstrip()
    try:
        rootScope = build_scope(sqlglot.parse_one(text, read=_DIALECT))
        if rootScope is None:
            return _UNKNOWN, []
        analysis = _Analysis(rootScope, activeByKey, frozenset(ended))
        return analysis.status(rootScope), list(analysis.probes.values())
    except (SqlglotError, RecursionError, ValueError):
        return _UNKNOWN, []


def findPeriodProbes(sql: str, activeTables: Iterable[str]) -> list[PeriodProbe]:
    """One probe per distinct bound the analysis reaches, in the order met.

    Empty when there is none, when the chart could not ask even if every one of them ended, when there are more than
    MAX_BOUNDS, or when the SQL does not parse.
    """
    _, probes = _analyse(sql, activeTables, ())
    if not probes or len(probes) > MAX_BOUNDS:
        return []
    status, _ = _analyse(sql, activeTables, [probe.sql for probe in probes])
    return probes if status != _UNKNOWN else []


def isDataFree(sql: str, activeTables: Iterable[str], endedProbes: Iterable[str]) -> bool:
    """Whether the chart provably holds no project data (EMPTY or DATAFREE) when the probes with these SQLs ended."""
    status, _ = _analyse(sql, activeTables, endedProbes)
    return status != _UNKNOWN


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
