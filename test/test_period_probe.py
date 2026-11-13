"""The period probe helper: the current-date bounds that filter a chart query's own rows, their probe query, and the
no-data predicate."""

from datetime import date, timedelta

import duckdb
import pyarrow as pa
import pytest

from nubrix.components.periodProbe import MAX_BOUNDS, chartHasNoData, combinedProbeSql, findPeriodProbes
from nubrix.components.sqlCheck import checkSql

TABLES = ["orders", "items", "refunds", "customers", "Order Lines"]


def _probe(sql, tables=TABLES):
    probes = findPeriodProbes(sql, tables)
    assert len(probes) == 1, probes
    return probes[0]


def _expectedSql(column, bound, table, side=None):
    side = side or column
    return (
        f"SELECT CAST(max({column}) AS VARCHAR) AS last_value, "
        f"(max({side}) IS NULL OR max({side}) < ({bound})) AS ended_before FROM {table}"
    )


def _found(sql, tables=TABLES):
    return [(probe.table, probe.column) for probe in findPeriodProbes(sql, tables)]


# -- finding the bound -------------------------------------------------------


@pytest.mark.parametrize(
    "where, bound",
    [
        ("d >= CURRENT_DATE - 29", "CURRENT_DATE - 29"),
        ("d > CURRENT_DATE - INTERVAL 12 MONTH", "CURRENT_DATE - INTERVAL '12' MONTH"),
        ("d BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE", "CURRENT_DATE - 7"),
        ("d BETWEEN CURRENT_DATE - 7 AND DATE '2999-01-01'", "CURRENT_DATE - 7"),
        ("CURRENT_DATE - 7 <= d", "CURRENT_DATE - 7"),
        ("d >= now() - INTERVAL '1 year'", "NOW() - INTERVAL '1' YEAR"),
        ("d >= today()", "CURRENT_DATE"),
        ("d >= get_current_timestamp() - INTERVAL 30 DAY", "GET_CURRENT_TIMESTAMP() - INTERVAL '30' DAY"),
        ("d >= current_timestamp - INTERVAL 30 DAY", "CURRENT_TIMESTAMP - INTERVAL '30' DAY"),
        ("d >= date_trunc('month', current_date) - INTERVAL 11 MONTH", "DATE_TRUNC('MONTH', CURRENT_DATE) - INTERVAL '11' MONTH"),
        ("status = 'x' AND d >= CURRENT_DATE - 29", "CURRENT_DATE - 29"),
        ("(status = 'x' AND (d >= CURRENT_DATE - 29))", "CURRENT_DATE - 29"),
    ],
)
def test_findsATopLevelWhereConjunct(where, bound):
    probe = _probe(f"SELECT count(*) AS n FROM orders WHERE {where}")
    assert probe.table == "orders"
    assert probe.column == "d" and probe.side == "d"
    assert probe.bound == bound
    assert probe.sql == _expectedSql("d", bound, '"orders"')


@pytest.mark.parametrize(
    "where",
    [
        "d <= CURRENT_DATE",
        "d < CURRENT_DATE - 30",
        "CURRENT_DATE >= d",
        "d BETWEEN DATE '2020-01-01' AND CURRENT_DATE",
        "NOT (d >= CURRENT_DATE - 30)",
        "d NOT BETWEEN CURRENT_DATE - 7 AND CURRENT_DATE",
    ],
)
def test_ignoresAnUpperBoundAlone(where):
    assert findPeriodProbes(f"SELECT count(*) AS n FROM orders WHERE {where}", TABLES) == []


@pytest.mark.parametrize(
    "where",
    [
        "d >= DATE '2024-01-01'",
        "d >= other_date",
        "amount > 10",
        "d >= CURRENT_DATE - lag_days",
        "d >= (SELECT max(d) FROM items) - 30",
        "max_d >= CURRENT_TIME",
    ],
)
def test_ignoresAComparisonWithNoCurrentDateFunctionOrARowDependentBound(where):
    assert findPeriodProbes(f"SELECT count(*) AS n FROM orders WHERE {where}", TABLES) == []


# -- only bounds that filter the chart's own rows (review C1) ----------------


@pytest.mark.parametrize(
    "sql",
    [
        # OR branch: the other branch adds rows.
        "SELECT status, sum(refund) AS total FROM orders WHERE d >= CURRENT_DATE - 30 OR status = 'open' GROUP BY 1",
        "SELECT count(*) AS n FROM orders WHERE status = 'x' AND (d >= CURRENT_DATE - 30 OR amount > 1)",
        # CASE label in the projection, and a CASE in WHERE.
        "SELECT CASE WHEN d >= CURRENT_DATE - 30 THEN 'recent' ELSE 'older' END AS bucket, sum(refund) AS total FROM orders GROUP BY 1",
        "SELECT count(*) AS n FROM orders WHERE CASE WHEN d >= CURRENT_DATE - 30 THEN false ELSE true END",
        "SELECT sum(CASE WHEN d >= CURRENT_DATE - 30 THEN amount ELSE 0 END) AS total FROM orders",
        # Anti-joins: NOT IN, NOT EXISTS, the right side of EXCEPT.
        "SELECT sum(discount) AS total FROM orders WHERE id NOT IN (SELECT order_id FROM refunds WHERE r_at >= CURRENT_DATE - 30)",
        "SELECT sum(discount) AS total FROM orders o WHERE NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id AND r.r_at >= CURRENT_DATE - 30)",
        "SELECT id FROM orders EXCEPT SELECT order_id FROM refunds WHERE r_at >= CURRENT_DATE - 30",
        # The optional side of an outer join.
        "SELECT c.region, sum(c.credit) AS total FROM customers c LEFT JOIN orders o ON o.cid = c.id AND o.d >= CURRENT_DATE - 30 GROUP BY 1",
        "SELECT count(*) AS n FROM orders o RIGHT JOIN items i ON i.oid = o.id AND o.d >= CURRENT_DATE - 30",
        "SELECT count(*) AS n FROM orders o FULL JOIN items i ON i.oid = o.id AND i.shipped >= CURRENT_DATE - 30",
        "SELECT count(*) AS n FROM (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30) AS r RIGHT JOIN items i ON i.oid = r.id",
        "SELECT count(*) AS n FROM customers c LEFT JOIN (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30) AS r ON r.cid = c.id",
        # An unused CTE.
        "WITH recent AS (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30) SELECT sum(refund) AS total FROM orders",
        # The comparison inside another expression.
        "SELECT count(*) AS n FROM orders WHERE (d >= CURRENT_DATE - 30) = false",
        "SELECT count(*) AS n FROM orders WHERE (d >= CURRENT_DATE - 30) IS NOT TRUE",
        "SELECT count(*) AS n FROM orders WHERE coalesce(d >= CURRENT_DATE - 30, true)",
        # FILTER and HAVING.
        "SELECT region, sum(refund) FILTER (WHERE d >= CURRENT_DATE - 30) AS recent FROM orders GROUP BY 1",
        "SELECT region, count(*) AS n FROM orders GROUP BY 1 HAVING max(d) >= CURRENT_DATE - 30",
        # A set operation other than UNION ALL, and a UNION ALL with an unbounded branch.
        "SELECT d FROM orders WHERE d >= CURRENT_DATE - 30 UNION SELECT d FROM items WHERE d >= CURRENT_DATE - 30",
        "SELECT d FROM orders WHERE d >= CURRENT_DATE - 30 EXCEPT SELECT d FROM items",
        "SELECT d FROM orders WHERE d >= CURRENT_DATE - 30 UNION ALL SELECT d FROM items",
        # An aggregate without GROUP BY gives a row even from no rows.
        "SELECT t.n FROM (SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 30) AS t",
    ],
)
def test_aBoundThatDoesNotFilterTheChartRowsMakesNoProbe(sql):
    assert findPeriodProbes(sql, TABLES) == []


@pytest.mark.parametrize(
    "sql",
    [
        # IN, EXISTS and scalar subqueries, with unqualified and qualified columns (review C2).
        "SELECT count(*) AS n FROM customers WHERE region = 'X' AND id IN (SELECT customer_id FROM orders WHERE created_at >= CURRENT_DATE - 30)",
        "SELECT count(*) AS n FROM customers c WHERE c.region = 'X' AND c.id IN (SELECT o.customer_id FROM orders o WHERE o.created_at >= CURRENT_DATE - 30)",
        "SELECT count(*) AS n FROM orders o WHERE EXISTS (SELECT 1 FROM items i WHERE i.oid = o.id AND d >= CURRENT_DATE - 30)",
        "SELECT count(*) AS n FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.cid = c.id AND o.created_at >= CURRENT_DATE - 30)",
        "SELECT region, (SELECT count(*) FROM refunds WHERE r_at >= CURRENT_DATE - 30) AS recent FROM customers",
        "SELECT count(*) AS n FROM customers WHERE (SELECT count(*) FROM orders WHERE created_at >= CURRENT_DATE - 30) > 0",
    ],
)
def test_aBoundInsideASubqueryExpressionMakesNoProbe(sql):
    assert findPeriodProbes(sql, TABLES) == []


def test_aCteBodyColumnResolvesInTheCteScope():
    sql = (
        "WITH r AS (SELECT * FROM orders WHERE created_at >= CURRENT_DATE - 30) "
        "SELECT count(*) AS n FROM customers c JOIN r ON r.cid = c.id"
    )
    assert _found(sql) == [("orders", "created_at")]


# -- the data path -----------------------------------------------------------


def test_resolvesASingleTableScope():
    probe = _probe("SELECT date_trunc('month', d) AS m, count(*) AS n FROM Orders WHERE d >= CURRENT_DATE - 365 GROUP BY 1")
    assert probe.table == "orders"
    assert probe.sql.endswith('FROM "orders"')


def test_resolvesAnAliasedJoin():
    sql = (
        "SELECT i.sku, count(*) AS n FROM orders AS o JOIN items AS i ON i.order_id = o.id "
        "WHERE o.created_at >= CURRENT_DATE - 30 GROUP BY 1"
    )
    probe = _probe(sql)
    assert (probe.table, probe.column) == ("orders", "created_at")
    assert probe.sql == _expectedSql("created_at", "CURRENT_DATE - 30", '"orders"')


def test_resolvesBothSidesOfAJoinSeparately():
    sql = (
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id "
        "WHERE o.created_at >= CURRENT_DATE - 30 AND i.shipped_at >= CURRENT_DATE - 30"
    )
    assert _found(sql) == [("orders", "created_at"), ("items", "shipped_at")]


def test_findsAConjunctOfAnInnerJoinOn():
    sql = "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id AND i.shipped_at >= CURRENT_DATE - 30"
    assert _found(sql) == [("items", "shipped_at")]
    cross = "SELECT count(*) AS n FROM orders o CROSS JOIN (SELECT * FROM items WHERE shipped_at >= CURRENT_DATE - 30) AS i"
    assert _found(cross) == [("items", "shipped_at")]


def test_aWhereBoundOnAnOuterJoinedColumnStillFiltersEveryRow():
    sql = "SELECT count(*) AS n FROM customers c LEFT JOIN orders o ON o.cid = c.id WHERE o.d >= CURRENT_DATE - 30"
    assert _found(sql) == [("orders", "d")]


def test_resolvesACteOverOneTable():
    sql = (
        "WITH m AS (SELECT CAST(o.d AS DATE) AS day, amount FROM orders AS o) "
        "SELECT day, sum(amount) AS total FROM m WHERE day >= CURRENT_DATE - 30 GROUP BY 1"
    )
    probe = _probe(sql)
    assert (probe.table, probe.column, probe.side) == ("orders", "d", "CAST(d AS DATE)")
    assert probe.sql == _expectedSql("d", "CURRENT_DATE - 30", '"orders"', side="CAST(d AS DATE)")


def test_resolvesAStarCteAndADerivedTable():
    star = _probe("WITH m AS (SELECT * FROM orders) SELECT count(*) AS n FROM m WHERE d >= CURRENT_DATE - 30")
    assert (star.table, star.column) == ("orders", "d")
    derived = _probe("SELECT count(*) AS n FROM (SELECT d AS dd FROM orders) AS s WHERE s.dd >= CURRENT_DATE - 30")
    assert (derived.table, derived.column) == ("orders", "d")


def test_findsABoundInsideAUsedCteOrDerivedTableBody():
    cte = "WITH recent AS (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30) SELECT count(*) AS n FROM recent"
    assert _found(cte) == [("orders", "d")]
    nested = (
        "WITH a AS (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30), b AS (SELECT * FROM a) "
        "SELECT count(*) AS n FROM (SELECT * FROM b) AS t JOIN items i ON i.oid = t.id"
    )
    assert _found(nested) == [("orders", "d")]


def test_aUnionAllNeedsABoundInEveryBranch():
    sql = "SELECT d FROM orders WHERE d >= CURRENT_DATE - 30 UNION ALL SELECT shipped FROM items WHERE shipped >= CURRENT_DATE - 30"
    assert _found(sql) == [("orders", "d"), ("items", "shipped")]
    inCte = (
        "WITH u AS (SELECT d FROM orders WHERE d >= CURRENT_DATE - 30 UNION ALL SELECT d FROM items WHERE d >= CURRENT_DATE - 30) "
        "SELECT count(*) AS n FROM u"
    )
    assert _found(inCte) == [("orders", "d"), ("items", "d")]


def test_aCteThatShadowsATableIsReadAsTheCte():
    assert _found("WITH orders AS (SELECT shipped AS d FROM items) SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 30") == [("items", "shipped")]


@pytest.mark.parametrize(
    "sql",
    [
        # Unqualified column with two sources in the scope.
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id WHERE created_at >= CURRENT_DATE - 30",
        # Qualifier that names no source in the scope.
        "SELECT count(*) AS n FROM orders o WHERE x.created_at >= CURRENT_DATE - 30",
        # A CTE over a join, read by an unqualified column.
        "WITH j AS (SELECT * FROM orders o JOIN items i ON i.order_id = o.id) SELECT count(*) AS n FROM j WHERE created_at >= CURRENT_DATE - 30",
        # A CTE column that is an aggregate.
        "WITH m AS (SELECT max(d) AS last FROM orders) SELECT count(*) AS n FROM m WHERE last >= CURRENT_DATE - 30",
        # A CTE over a set operation.
        "WITH u AS (SELECT d FROM orders UNION ALL SELECT d FROM items) SELECT count(*) AS n FROM u WHERE d >= CURRENT_DATE - 30",
        # A source that is not a project table.
        "SELECT count(*) AS n FROM (VALUES (DATE '2020-01-01')) AS v(d) WHERE d >= CURRENT_DATE - 30",
        "SELECT count(*) AS n FROM unknown_table WHERE d >= CURRENT_DATE - 30",
        # One resolvable bound does not excuse another that cannot be resolved.
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id WHERE created_at >= CURRENT_DATE - 30 AND o.d >= CURRENT_DATE - 30",
    ],
)
def test_anUnresolvableBoundDeclinesTheQuery(sql):
    assert findPeriodProbes(sql, TABLES) == []


@pytest.mark.parametrize(
    "sql",
    [
        # A column list renames the source's columns (review M2).
        "WITH m(d, created) AS (SELECT created, d FROM orders) SELECT count(*) AS n FROM m WHERE d >= CURRENT_DATE - 30",
        "SELECT count(*) AS n FROM (SELECT created, d FROM orders) AS t(d, created) WHERE t.d >= CURRENT_DATE - 30",
        "SELECT count(*) AS n FROM orders AS o(created, d) WHERE o.d >= CURRENT_DATE - 30",
    ],
)
def test_aSourceWithAColumnListDeclines(sql):
    assert findPeriodProbes(sql, TABLES) == []


def test_identicalComparisonsGiveOneProbe():
    sql = "SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 30 AND amount > 1 AND d >= CURRENT_DATE - 30"
    assert len(findPeriodProbes(sql, TABLES)) == 1


def test_unparsableSqlGivesNoProbe():
    assert findPeriodProbes("SELECT FROM WHERE >=", TABLES) == []


# -- the compared side (review M1) ------------------------------------------


@pytest.mark.parametrize(
    "side, expected",
    [
        ("date_trunc('month', d)", "DATE_TRUNC('MONTH', d)"),
        ("CAST(d AS DATE)", "CAST(d AS DATE)"),
        ("TRY_CAST(d AS DATE)", "TRY_CAST(d AS DATE)"),
        ("CAST(date_trunc('month', d) AS DATE)", "CAST(DATE_TRUNC('MONTH', d) AS DATE)"),
    ],
)
def test_aWrappedSideQuotesTheBareColumn(side, expected):
    probe = _probe(f"SELECT count(*) AS n FROM orders WHERE {side} >= CURRENT_DATE - 30")
    assert (probe.column, probe.side) == ("d", expected)
    assert probe.sql == _expectedSql("d", "CURRENT_DATE - 30", '"orders"', side=expected)


@pytest.mark.parametrize(
    "side",
    ["year(d)", "d + INTERVAL 1 DAY", "strftime(d, '%Y-%m-%d')", "coalesce(d, e)", "date_trunc('month', d + 1)"],
)
def test_anyOtherWrapperDeclines(side):
    assert findPeriodProbes(f"SELECT count(*) AS n FROM orders WHERE {side} >= CURRENT_DATE - 30", TABLES) == []


# -- one combined probe (review M4) -----------------------------------------


def test_theCombinedProbeCrossJoinsEveryBound():
    probes = findPeriodProbes(
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.oid = o.id WHERE o.d >= CURRENT_DATE - 30 AND i.s >= CURRENT_DATE - 7",
        TABLES,
    )
    assert combinedProbeSql(probes) == (
        "SELECT p1.last_value AS last_value_1, p1.ended_before AS ended_before_1, "
        "p2.last_value AS last_value_2, p2.ended_before AS ended_before_2 "
        f"FROM ({probes[0].sql}) AS p1 CROSS JOIN ({probes[1].sql}) AS p2"
    )
    assert checkSql(combinedProbeSql(probes), TABLES).tables == ("orders", "items")


def test_moreThanEightBoundsDecline():
    def query(count):
        conjuncts = " AND ".join(f"d >= CURRENT_DATE - {days}" for days in range(1, count + 1))
        return f"SELECT count(*) AS n FROM orders WHERE {conjuncts}"

    assert MAX_BOUNDS == 8
    assert len(findPeriodProbes(query(8), TABLES)) == 8
    assert findPeriodProbes(query(9), TABLES) == []


# -- quoting -----------------------------------------------------------------


def test_quotesNamesWithSpacesAndDoubleQuotes():
    tables = ['Order "Lines" 2024']
    sql = 'SELECT count(*) AS n FROM "order ""lines"" 2024" WHERE "Ship ""Day""" >= CURRENT_DATE - 30'
    probe = _probe(sql, tables)
    assert probe.table == 'Order "Lines" 2024'
    assert probe.column == '"Ship ""Day"""'
    assert probe.sql == _expectedSql('"Ship ""Day"""', "CURRENT_DATE - 30", '"Order ""Lines"" 2024"')
    assert checkSql(probe.sql, tables).tables == ('Order "Lines" 2024',)
    assert checkSql(combinedProbeSql([probe]), tables).tables == ('Order "Lines" 2024',)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 29",
        'SELECT count(*) AS n FROM "Order Lines" AS l WHERE l."Ship Date" BETWEEN now() - INTERVAL 1 YEAR AND now()',
        "WITH m AS (SELECT CAST(o.d AS DATE) AS day FROM orders o) SELECT count(*) AS n FROM m WHERE day > today() - 7",
    ],
)
def test_theProbePassesTheSqlCheck(sql):
    probe = _probe(sql)
    assert checkSql(probe.sql, TABLES).tables == (probe.table,)
    assert checkSql(combinedProbeSql([probe]), TABLES).tables == (probe.table,)


# -- the real DuckDB ---------------------------------------------------------


def _duckdb(**tables):
    connection = duckdb.connect(":memory:")
    for name, table in tables.items():
        connection.register(name, table)
    return connection


@pytest.mark.parametrize("day, endedBefore", [(date.today() - timedelta(days=365 * 4), True), (date.today(), False)])
def test_theProbeRunsOnDuckDb(day, endedBefore):
    sql = "SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 29"
    probe = _probe(sql)
    connection = _duckdb(orders=pa.table({"d": pa.array([day], pa.date32())}))
    try:
        assert connection.execute(sql).fetchone()[0] == (0 if endedBefore else 1)
        lastValue, ended = connection.execute(probe.sql).fetchone()
        combined = connection.execute(combinedProbeSql([probe])).fetchall()
    finally:
        connection.close()
    assert ended is endedBefore
    assert lastValue == day.isoformat()
    assert combined == [(day.isoformat(), endedBefore)]


def test_theCombinedProbeAnswersEveryBoundOnDuckDb():
    old, today = date.today() - timedelta(days=365 * 4), date.today()
    sql = (
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.oid = o.id "
        "WHERE date_trunc('month', o.d) >= CURRENT_DATE - 29 AND i.s >= CURRENT_DATE - 29"
    )
    probes = findPeriodProbes(sql, TABLES)
    connection = _duckdb(
        orders=pa.table({"d": pa.array([old], pa.date32()), "id": [1]}),
        items=pa.table({"s": pa.array([today], pa.date32()), "oid": [1]}),
    )
    try:
        row = connection.execute(combinedProbeSql(probes)).fetchone()
    finally:
        connection.close()
    # The wrapped side decides `ended_before`; `last_value` is the bare column's latest date.
    assert row == (old.isoformat(), True, today.isoformat(), False)


def test_theProbeOfAnEmptyTableEndedBeforeWithNoValue():
    probe = _probe("SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 29")
    connection = _duckdb(orders=pa.table({"d": pa.array([], pa.date32())}))
    try:
        assert connection.execute(probe.sql).fetchone() == (None, True)
    finally:
        connection.close()


# -- a list of periods filled in from the data -------------------------------

MONTHS_CTE = (
    "months AS (SELECT unnest(generate_series(date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH, "
    "date_trunc('month', CURRENT_DATE), INTERVAL 1 MONTH)) AS month_start)"
)
PER_MONTH_CTE = (
    "per_month AS (SELECT date_trunc('month', \"ts\") AS month_start, COUNT(\"id\") AS n FROM \"events\" "
    "WHERE \"ts\" >= date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH AND \"ts\" < CURRENT_DATE + INTERVAL 1 DAY GROUP BY ALL)"
)
FILLED_SELECT = (
    "SELECT strftime(m.month_start, '%Y-%m') AS month, COALESCE(p.n, 0) AS n FROM months AS m "
    "LEFT JOIN per_month AS p ON p.month_start = m.month_start ORDER BY m.month_start"
)
FILLED_IN = f"WITH {MONTHS_CTE}, {PER_MONTH_CTE} {FILLED_SELECT}"
EVENTS = ["events", "customers"]


def test_aListOfPeriodsLeftJoinedToFilteredDataProbesTheDataBound():
    probe = _probe(FILLED_IN, EVENTS)
    assert (probe.table, probe.column) == ("events", '"ts"')
    assert probe.bound == "DATE_TRUNC('MONTH', CURRENT_DATE) - INTERVAL '11' MONTH"
    assert checkSql(FILLED_IN, EVENTS).tables == ("events",)
    assert checkSql(combinedProbeSql([probe]), EVENTS).tables == ("events",)


@pytest.mark.parametrize(
    "periods",
    [
        "months AS (SELECT * FROM (VALUES (DATE '2024-01-01'), (DATE '2024-02-01')) AS v(month_start))",
        "months AS (SELECT CAST(date_trunc('month', CURRENT_DATE) AS DATE) AS month_start)",
        "base AS (SELECT unnest(generate_series(1, 12)) AS k), months AS (SELECT date_trunc('month', CURRENT_DATE) - k * INTERVAL 1 MONTH AS month_start FROM base)",
    ],
)
def test_aPeriodListBuiltWithoutATableIsAccepted(periods):
    sql = f"WITH {periods}, {PER_MONTH_CTE} {FILLED_SELECT}"
    assert _found(sql, EVENTS) == [("events", '"ts"')]


def test_aBoundInTheOnOfTheFillingJoinCounts():
    sql = (
        f"WITH {MONTHS_CTE} SELECT m.month_start, count(e.id) AS n FROM months AS m LEFT JOIN events AS e "
        "ON date_trunc('month', e.ts) = m.month_start AND e.ts >= CURRENT_DATE - INTERVAL 1 YEAR GROUP BY 1"
    )
    assert _found(sql, EVENTS) == [("events", "ts")]


@pytest.mark.parametrize(
    "sql",
    [
        # The period list selects from a project table.
        f"WITH months AS (SELECT DISTINCT date_trunc('month', ts) AS month_start FROM events), {PER_MONTH_CTE} {FILLED_SELECT}",
        # ... or reads one in a subquery.
        f"WITH months AS (SELECT unnest(generate_series((SELECT min(ts) FROM events), CURRENT_DATE, INTERVAL 1 MONTH)) AS month_start), {PER_MONTH_CTE} {FILLED_SELECT}",
        # ... or through another CTE.
        f"WITH raw AS (SELECT ts FROM events), months AS (SELECT DISTINCT date_trunc('month', ts) AS month_start FROM raw), {PER_MONTH_CTE} {FILLED_SELECT}",
        # The preserved side is a project table.
        f"WITH recent AS (SELECT * FROM events WHERE ts >= CURRENT_DATE - 30) SELECT c.region, count(r.id) AS n FROM customers c LEFT JOIN recent r ON r.cid = c.id GROUP BY 1",
        # A second outer-joined source reads a table too, so not every measure comes from the filtered side.
        f"WITH {MONTHS_CTE}, {PER_MONTH_CTE} SELECT m.month_start, COALESCE(p.n, 0) AS n, count(c.id) AS c FROM months AS m "
        "LEFT JOIN per_month AS p ON p.month_start = m.month_start LEFT JOIN customers AS c ON c.joined = m.month_start GROUP BY ALL",
        # RIGHT and FULL joins still decline.
        f"WITH {MONTHS_CTE}, {PER_MONTH_CTE} SELECT m.month_start, p.n FROM per_month AS p RIGHT JOIN months AS m ON p.month_start = m.month_start",
        f"WITH {MONTHS_CTE}, {PER_MONTH_CTE} SELECT m.month_start, p.n FROM months AS m FULL JOIN per_month AS p ON p.month_start = m.month_start",
        # A bound reached only through a subquery expression.
        f"WITH {MONTHS_CTE} SELECT m.month_start, (SELECT count(*) FROM events WHERE ts >= CURRENT_DATE - 30) AS n FROM months AS m",
    ],
)
def test_anyOtherOuterJoinDeclines(sql):
    assert findPeriodProbes(sql, EVENTS) == []


# -- the no-data predicate ---------------------------------------------------


def _category(chartType, labels, *datasets):
    return {"chartType": chartType, "title": "T", "data": {"labels": labels, "datasets": [{"label": "v", "data": list(d)} for d in datasets]}}


def _points(chartType, *datasets):
    return {"chartType": chartType, "title": "T", "data": {"datasets": [{"label": "v", "data": list(d)} for d in datasets]}}


NO_DATA = [
    _category("bar", []),
    _category("line", ["2024-01", "2024-02"], [0, 0]),
    _category("line", ["2024-01", "2024-02"], [0, None], [0.0, 0]),
    _category("area", ["a"], [None]),
    _category("radar", ["a", "b"]),
    _category("histogram", [], []),
    _category("pie", [], []),
    _category("doughnut", ["a", "b"], [0, 0]),
    _category("polarArea", ["a"], [None]),
    _points("scatter"),
    _points("scatter", []),
    _points("bubble", [], []),
    {"chartType": "card", "title": "T", "label": "Total", "data": 0},
    {"chartType": "card", "title": "T", "label": "Total", "data": 0.0},
    {"chartType": "card", "title": "T", "label": "Total", "data": None},
    {"chartType": "table", "title": "T", "columns": [], "data": []},
    {"chartType": "table", "title": "T", "columns": ["a"], "data": []},
    {"chartType": "pivot", "title": "T", "data": {}},
    {"chartType": "pivot", "title": "T", "data": {"x": {"r1": 0, "r2": None}, "y": {"r1": 0.0, "r2": 0}}},
    {"chartType": "geoMap", "title": "T", "map": {"mapType": "scatterMap", "data": {"points": []}}},
]

WITH_DATA = [
    _category("bar", ["a"], [1]),
    _category("line", ["2024-01", "2024-02"], [0, 0], [0, 3]),
    _category("area", ["a"], [-2]),
    _category("radar", ["a"], [0.5]),
    _category("histogram", ["1-2"], [4]),
    _category("pie", ["a", "b"], [0, 7]),
    _category("doughnut", ["a"], [1]),
    _category("polarArea", ["a"], [2]),
    _points("scatter", [{"x": 0, "y": 0}]),
    _points("bubble", [], [{"x": 1, "y": 2, "r": 3}]),
    {"chartType": "card", "title": "T", "label": "Total", "data": 12},
    {"chartType": "card", "title": "T", "label": "Total", "data": "North"},
    {"chartType": "card", "title": "T", "label": "Total", "data": "false"},
    {"chartType": "table", "title": "T", "columns": ["a"], "data": [{"a": 0}]},
    {"chartType": "pivot", "title": "T", "data": {"x": {"r1": 0, "r2": 5}}},
    {"chartType": "geoMap", "title": "T", "map": {"mapType": "scatterMap", "data": {"points": [{"id": "0", "lat": 1, "long": 2, "label": "p"}]}}},
]


@pytest.mark.parametrize("chart", NO_DATA, ids=lambda chart: chart["chartType"])
def test_aChartWithoutDataHasNoData(chart):
    assert chartHasNoData(chart) is True


@pytest.mark.parametrize("chart", WITH_DATA, ids=lambda chart: chart["chartType"])
def test_aChartWithDataHasData(chart):
    assert chartHasNoData(chart) is False


@pytest.mark.parametrize("chart", [None, {}, {"chartType": "unknown"}, {"chartType": "bar", "data": "x"}, {"chartType": "card"}])
def test_anUnknownShapeIsNotCountedAsNoData(chart):
    assert chartHasNoData(chart) is False


def test_theBuilderEmptyChartsHaveNoData():
    from nubrix.components.chartBuilder import emptyChart
    from nubrix.components.chartRequest import CHART_TYPES

    for chartType in CHART_TYPES:
        assert chartHasNoData(emptyChart(chartType, "T")) is True, chartType
