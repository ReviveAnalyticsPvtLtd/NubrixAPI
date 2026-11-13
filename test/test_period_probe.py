"""The period probe helper: current-date lower bounds in a chart query, their probe queries, and the no-data predicate."""

from datetime import date, timedelta

import duckdb
import pyarrow as pa
import pytest

from nubrix.components.periodProbe import chartHasNoData, findPeriodProbes
from nubrix.components.sqlCheck import checkSql

TABLES = ["orders", "items", "Order Lines"]


def _probe(sql, tables=TABLES):
    probes = findPeriodProbes(sql, tables)
    assert len(probes) == 1, probes
    return probes[0]


def _expectedSql(column, bound, table):
    return (
        f"SELECT CAST(max({column}) AS VARCHAR) AS last_value, "
        f"(max({column}) IS NULL OR max({column}) < ({bound})) AS ended_before FROM {table}"
    )


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
    ],
)
def test_findsTheCurrentDateLowerBound(where, bound):
    probe = _probe(f"SELECT count(*) AS n FROM orders WHERE {where}")
    assert probe.table == "orders"
    assert probe.column == "d"
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


def test_ignoresAnAggregateOnTheColumnSide():
    sql = "SELECT a FROM orders GROUP BY a HAVING max(d) >= CURRENT_DATE - 30"
    assert findPeriodProbes(sql, TABLES) == []


def test_aBoundInsideACaseInTheSelectListCounts():
    probe = _probe("SELECT sum(CASE WHEN d >= CURRENT_DATE - 30 THEN amount ELSE 0 END) AS total FROM orders")
    assert probe.table == "orders" and probe.column == "d"


# -- resolving the column ----------------------------------------------------


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
    assert probe.table == "orders"
    assert probe.column == "created_at"
    assert probe.sql == _expectedSql("created_at", "CURRENT_DATE - 30", '"orders"')


def test_resolvesBothSidesOfAJoinSeparately():
    sql = (
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id "
        "WHERE o.created_at >= CURRENT_DATE - 30 AND i.shipped_at >= CURRENT_DATE - 30"
    )
    probes = findPeriodProbes(sql, TABLES)
    assert [(probe.table, probe.column) for probe in probes] == [("orders", "created_at"), ("items", "shipped_at")]


def test_resolvesACteOverOneTable():
    sql = (
        "WITH m AS (SELECT CAST(o.d AS DATE) AS day, amount FROM orders AS o) "
        "SELECT day, sum(amount) AS total FROM m WHERE day >= CURRENT_DATE - 30 GROUP BY 1"
    )
    probe = _probe(sql)
    assert probe.table == "orders"
    assert probe.column == "CAST(d AS DATE)"
    assert probe.sql == _expectedSql("CAST(d AS DATE)", "CURRENT_DATE - 30", '"orders"')


def test_resolvesAStarCteAndADerivedTable():
    star = _probe("WITH m AS (SELECT * FROM orders) SELECT count(*) AS n FROM m WHERE d >= CURRENT_DATE - 30")
    assert (star.table, star.column) == ("orders", "d")
    derived = _probe("SELECT count(*) AS n FROM (SELECT d AS dd FROM orders) AS s WHERE s.dd >= CURRENT_DATE - 30")
    assert (derived.table, derived.column) == ("orders", "d")


def test_resolvesABoundInsideACteBody():
    sql = "WITH recent AS (SELECT * FROM orders WHERE d >= CURRENT_DATE - 30) SELECT count(*) AS n FROM recent"
    assert _probe(sql).table == "orders"


@pytest.mark.parametrize(
    "sql",
    [
        # Unqualified column with two tables in FROM.
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
    ],
)
def test_skipsAnUnresolvableColumn(sql):
    assert findPeriodProbes(sql, TABLES) == []


def test_skipsOnlyTheUnresolvableComparison():
    sql = (
        "SELECT count(*) AS n FROM orders o JOIN items i ON i.order_id = o.id "
        "WHERE created_at >= CURRENT_DATE - 30 AND o.d >= CURRENT_DATE - 30"
    )
    assert [(probe.table, probe.column) for probe in findPeriodProbes(sql, TABLES)] == [("orders", "d")]


def test_identicalComparisonsGiveOneProbe():
    sql = "SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 30 OR (d >= CURRENT_DATE - 30 AND amount > 1)"
    assert len(findPeriodProbes(sql, TABLES)) == 1


def test_unparsableSqlGivesNoProbe():
    assert findPeriodProbes("SELECT FROM WHERE >=", TABLES) == []


# -- quoting -----------------------------------------------------------------


def test_quotesNamesWithSpacesAndDoubleQuotes():
    tables = ['Order "Lines" 2024']
    sql = 'SELECT count(*) AS n FROM "order ""lines"" 2024" WHERE "Ship ""Day""" >= CURRENT_DATE - 30'
    probe = _probe(sql, tables)
    assert probe.table == 'Order "Lines" 2024'
    assert probe.column == '"Ship ""Day"""'
    assert probe.sql == _expectedSql('"Ship ""Day"""', "CURRENT_DATE - 30", '"Order ""Lines"" 2024"')
    assert checkSql(probe.sql, tables).tables == ('Order "Lines" 2024',)


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


# -- the real DuckDB ---------------------------------------------------------


@pytest.mark.parametrize("day, endedBefore", [(date.today() - timedelta(days=365 * 4), True), (date.today(), False)])
def test_theProbeRunsOnDuckDb(day, endedBefore):
    sql = "SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 29"
    probe = _probe(sql)
    connection = duckdb.connect(":memory:")
    try:
        orders = pa.table({"d": pa.array([day], pa.date32())})
        connection.register("orders", orders)
        assert connection.execute(sql).fetchone()[0] == (0 if endedBefore else 1)
        lastValue, ended = connection.execute(probe.sql).fetchone()
    finally:
        connection.close()
    assert ended is endedBefore
    assert lastValue == day.isoformat()


def test_theProbeOfAnEmptyTableEndedBeforeWithNoValue():
    probe = _probe("SELECT count(*) AS n FROM orders WHERE d >= CURRENT_DATE - 29")
    connection = duckdb.connect(":memory:")
    try:
        connection.register("orders", pa.table({"d": pa.array([], pa.date32())}))
        assert connection.execute(probe.sql).fetchone() == (None, True)
    finally:
        connection.close()


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
