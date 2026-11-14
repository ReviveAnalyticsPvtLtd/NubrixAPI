"""The generation guards: a number label cast to text, and a scatter or bubble whose every point is its own series.

Each chart is built the way the engine builds it: the SQL runs on an in-process DuckDB over invented rows and the
builder turns the result into the chart, so the labels a guard reads are the ones the client would get.
"""

from datetime import date

import duckdb
import pyarrow as pa
import pytest

from nubrix.components import chartGuards
from nubrix.components.chartBuilder import buildChart
from nubrix.components.chartRequest import ChartRequest

TABLES = {
    "orders": pa.table(
        {
            "order_id": [1, 2, 3, 4, 5, 6],
            "items": [1, 2, 10, 11, 2, 1],
            "amount": [1.0, 2.0, 2.0, 3.0, 40.0, 51.0],
            "region": ["north", "south", "north", "east", "west", "south"],
            "postal_code": ["01026", "01027", "10001", "01026", "20002", "10001"],
            "ordered_at": pa.array([date(2024, month, 1) for month in (1, 2, 3, 3, 4, 5)], pa.date32()),
            "lat": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            "long": [20.0, 21.0, 22.0, 23.0, 24.0, 25.0],
        }
    ),
}


def _chart(chartType, sql, encoding):
    """The request and the chart the engine would build for it."""
    request = ChartRequest.model_validate({"chartType": chartType, "title": "Chart", "sql": sql, "encoding": encoding})
    connection = duckdb.connect(":memory:")
    try:
        for name, table in TABLES.items():
            connection.register(name, table)
        result = connection.execute(sql).arrow()
    finally:
        connection.close()
    return request, buildChart(request, result)


def _message(chartType, sql, encoding):
    request, chart = _chart(chartType, sql, encoding)
    return chartGuards.guardMessage(request, sql, chart)


CAST_ITEMS = 'SELECT CAST("items" AS VARCHAR) AS items, COUNT(*) AS n FROM "orders" GROUP BY "items" ORDER BY "items"'
TEXT_ORDER = ["1", "10", "11", "2"]


def _textOrderMessage(name):
    return chartGuards.castLabelText(name, TEXT_ORDER, None)


def test_aNumberLabelCastToTextAndSortedAsTextFires():
    request, chart = _chart("bar", CAST_ITEMS, {"x": "items", "y": ["n"]})
    assert chart["data"]["labels"] == TEXT_ORDER

    message = chartGuards.guardMessage(request, CAST_ITEMS, chart)

    assert message == (
        "Column 'items' casts a number to text, so its labels come out in text order (1, 10, 11, 2). Keep it a number: "
        "drop the CAST and order by the numeric value. The backend shows a number label as text and keeps the query's "
        "row order."
    )


def test_labelsCastToTextInDescendingTextOrderFire():
    sql = 'SELECT CAST("items" AS VARCHAR) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items DESC'
    request, chart = _chart("bar", sql, {"x": "items", "y": ["n"]})
    assert chart["data"]["labels"] == ["2", "11", "10", "1"]

    assert chartGuards.guardMessage(request, sql, chart) == chartGuards.castLabelText("items", ["2", "11", "10", "1"], None)


def test_aRoundedDoubleCastToTextShowsAsOnePointZeroAndFires():
    sql = 'SELECT CAST(round("amount") AS VARCHAR) AS amount_band, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY MIN("amount")'
    request, chart = _chart("pie", sql, {"label": "amount_band", "value": "n"})
    assert chart["data"]["labels"] == ["1.0", "2.0", "3.0", "40.0", "51.0"]

    assert chartGuards.guardMessage(request, sql, chart) == (
        "Column 'amount_band' casts a number to text, so a whole number shows as '1.0'. Keep it a number: drop the CAST, "
        "and when the value is a DECIMAL or DOUBLE holding whole numbers, CAST it AS BIGINT. The backend shows a "
        "whole-number BIGINT or DOUBLE label as '3' but a DECIMAL as '3.00'."
    )


def test_aWholeDecimalCastToTextFiresAndTheMessageFixWorksForADecimal():
    """Dropping the CAST alone still shows a DECIMAL as '1.00'; CAST AS BIGINT shows '1', as the message says."""
    cast = 'SELECT CAST(CAST("items" AS DECIMAL(10, 2)) AS VARCHAR) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY MIN("items")'
    request, chart = _chart("bar", cast, {"x": "items", "y": ["n"]})
    assert chart["data"]["labels"] == ["1.00", "2.00", "10.00", "11.00"]
    message = chartGuards.guardMessage(request, cast, chart)
    assert message is not None and "a whole number shows as '1.00'" in message and "CAST it AS BIGINT" in message

    decimal = 'SELECT CAST("items" AS DECIMAL(10, 2)) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items'
    whole = 'SELECT CAST(CAST("items" AS DECIMAL(10, 2)) AS BIGINT) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items'
    double = 'SELECT CAST("items" AS DOUBLE) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items'
    assert _chart("bar", decimal, {"x": "items", "y": ["n"]})[1]["data"]["labels"] == ["1.00", "2.00", "10.00", "11.00"]
    assert _chart("bar", whole, {"x": "items", "y": ["n"]})[1]["data"]["labels"] == ["1", "2", "10", "11"]
    assert _chart("bar", double, {"x": "items", "y": ["n"]})[1]["data"]["labels"] == ["1", "2", "10", "11"]


def test_yearsCastToTextDoNotFire():
    sql = 'SELECT CAST("order_id" + 2015 AS VARCHAR) AS order_year, SUM("amount") AS total FROM "orders" GROUP BY ALL ORDER BY order_year'
    request, chart = _chart("line", sql, {"x": "order_year", "y": ["total"]})
    assert chart["data"]["labels"] == ["2016", "2017", "2018", "2019", "2020", "2021"]

    assert chartGuards.guardMessage(request, sql, chart) is None


def test_aTwoSlicePieInMeasureOrderDoesNotFire():
    sql = (
        'SELECT CAST("items" AS VARCHAR) AS items, SUM("amount") AS total FROM "orders" WHERE "items" IN (2, 10) '
        "GROUP BY ALL ORDER BY total DESC, items"
    )
    request, chart = _chart("pie", sql, {"label": "items", "value": "total"})
    assert chart["data"]["labels"] == ["2", "10"]

    assert chartGuards.guardMessage(request, sql, chart) is None


def test_labelsCastToTextInMeasureOrderDoNotFire():
    sql = 'SELECT CAST("items" AS VARCHAR) AS items, SUM("amount") AS total FROM "orders" GROUP BY ALL ORDER BY total DESC, items'
    request, chart = _chart("bar", sql, {"x": "items", "y": ["total"]})
    assert chart["data"]["labels"] == ["1", "2", "11", "10"]

    assert chartGuards.guardMessage(request, sql, chart) is None


@pytest.mark.parametrize(
    "projection",
    [
        'CAST("items" AS VARCHAR)',
        'TRY_CAST("items" AS VARCHAR)',
        '"items"::VARCHAR',
        '"items"::TEXT',
        'CAST("items" AS STRING)',
        'CAST("items" AS VARCHAR(10))',
        '"items"::BPCHAR',
        '(CAST("items" AS VARCHAR))',
    ],
)
def test_everySpellingOfATextCastFires(projection):
    sql = f'SELECT {projection} AS item_count, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY item_count'
    assert _message("bar", sql, {"x": "item_count", "y": ["n"]}) == _textOrderMessage("item_count")


@pytest.mark.parametrize("chartType", ["bar", "line", "area", "histogram", "radar"])
def test_everyAxisChartTypeFiresOnItsX(chartType):
    assert _message(chartType, CAST_ITEMS, {"x": "items", "y": ["n"]}) is not None


@pytest.mark.parametrize("chartType", ["pie", "doughnut", "polarArea"])
def test_everySliceChartTypeFiresOnItsLabel(chartType):
    assert _message(chartType, CAST_ITEMS, {"label": "items", "value": "n"}) is not None


def test_aLongFormChartFiresOnItsXNotItsSeries():
    castX = 'SELECT CAST("items" AS VARCHAR) AS items, "region" AS region, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items, region'
    castSeries = 'SELECT "region" AS region, CAST("items" AS VARCHAR) AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY region, items'

    assert _message("line", castX, {"x": "items", "y": ["n"], "series": "region"}) is not None
    assert _message("line", castSeries, {"x": "region", "y": ["n"], "series": "items"}) is None


@pytest.mark.parametrize(
    "rows, columns, name",
    [
        (["items"], ["region"], "items"),
        (["region"], ["items"], "items"),
        (["n_band", "items"], ["region"], "items"),
    ],
)
def test_aPivotFiresOnACastRowOrColumnLevel(rows, columns, name):
    sql = (
        'SELECT CAST("items" AS VARCHAR) AS items, "region" AS region, \'all\' AS n_band, SUM("amount") AS total '
        'FROM "orders" GROUP BY ALL ORDER BY items, region'
    )
    assert _message("pivot", sql, {"rows": rows, "columns": columns, "value": "total"}) == _textOrderMessage(name)


def test_aPivotInNumericOrderDoesNotFire():
    sql = (
        'SELECT CAST("items" AS VARCHAR) AS items, "region" AS region, SUM("amount") AS total '
        'FROM "orders" GROUP BY ALL ORDER BY MIN("items"), region'
    )
    assert _message("pivot", sql, {"rows": ["items"], "columns": ["region"], "value": "total"}) is None


@pytest.mark.parametrize(
    "chartType, sql, encoding",
    [
        # A numeric label with no cast.
        ("bar", 'SELECT "items" AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY items', {"x": "items", "y": ["n"]}),
        # A text label joined from parts.
        (
            "bar",
            "SELECT 'Q' || CAST(quarter(\"ordered_at\") AS VARCHAR) AS order_quarter, COUNT(*) AS n FROM \"orders\" GROUP BY ALL ORDER BY 1",
            {"x": "order_quarter", "y": ["n"]},
        ),
        # The 'Other' slice's CASE.
        (
            "pie",
            "WITH ranked AS (SELECT \"items\" AS items, COUNT(*) AS m, row_number() OVER (ORDER BY COUNT(*) DESC, \"items\") AS rn, "
            "COUNT(*) OVER () AS k FROM \"orders\" GROUP BY \"items\") "
            "SELECT CASE WHEN rn <= 2 OR k <= 3 THEN CAST(items AS VARCHAR) ELSE 'Other' END AS items, SUM(m) AS m FROM ranked GROUP BY ALL ORDER BY MIN(rn)",
            {"label": "items", "value": "m"},
        ),
        # strftime labels.
        (
            "line",
            "SELECT strftime(date_trunc('month', \"ordered_at\"), '%Y') AS order_year, COUNT(*) AS n FROM \"orders\" GROUP BY ALL ORDER BY 1",
            {"x": "order_year", "y": ["n"]},
        ),
        # '0-50' ranges.
        (
            "histogram",
            "WITH binned AS (SELECT CAST(floor(\"amount\" / 50) AS INTEGER) AS bin FROM \"orders\") "
            "SELECT CAST(bin * 50 AS VARCHAR) || '-' || CAST((bin + 1) * 50 AS VARCHAR) AS value_range, COUNT(*) AS n FROM binned GROUP BY bin ORDER BY bin",
            {"x": "value_range", "y": ["n"]},
        ),
        # A cast to text in a CTE that the outer SELECT passes through.
        (
            "bar",
            'WITH counted AS (SELECT CAST("items" AS VARCHAR) AS items, COUNT(*) AS n FROM "orders" GROUP BY "items") SELECT items, n FROM counted ORDER BY n DESC, items',
            {"x": "items", "y": ["n"]},
        ),
        # A code with a leading zero.
        (
            "bar",
            'SELECT CAST("postal_code" AS VARCHAR) AS postal_code, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY n DESC, postal_code',
            {"x": "postal_code", "y": ["n"]},
        ),
        # A UNION at the top.
        (
            "bar",
            'SELECT CAST("items" AS VARCHAR) AS items, 1 AS n FROM "orders" WHERE "items" < 5 UNION ALL SELECT CAST("items" AS VARCHAR) AS items, 2 AS n FROM "orders" WHERE "items" >= 5',
            {"x": "items", "y": ["n"]},
        ),
        # A cast to a number, not to text.
        ("bar", 'SELECT CAST("amount" AS BIGINT) AS amount, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY 1', {"x": "amount", "y": ["n"]}),
        # Text cast to text.
        ("bar", 'SELECT CAST("region" AS VARCHAR) AS region, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY n DESC, region', {"x": "region", "y": ["n"]}),
        # A table casting a year.
        (
            "table",
            "SELECT CAST(year(\"ordered_at\") AS VARCHAR) AS order_year, COUNT(*) AS n FROM \"orders\" GROUP BY ALL ORDER BY MIN(year(\"ordered_at\"))",
            {"columns": ["order_year", "n"]},
        ),
        # A geoMap label cast.
        (
            "geoMap",
            'SELECT "lat" AS lat, "long" AS long, CAST("order_id" AS VARCHAR) AS label FROM "orders" ORDER BY "order_id"',
            {"lat": "lat", "long": "long", "label": "label"},
        ),
        # A card.
        ("card", 'SELECT CAST(COUNT(*) AS VARCHAR) AS n FROM "orders"', {"value": "n"}),
    ],
)
def test_theCastGuardDeclines(chartType, sql, encoding):
    assert _message(chartType, sql, encoding) is None


def test_aStarInTheProjectionDeclines():
    sql = 'SELECT CAST("items" AS VARCHAR) AS item_label, * EXCLUDE ("items") FROM "orders" ORDER BY "order_id"'
    request, chart = _chart("bar", 'SELECT CAST("items" AS VARCHAR) AS item_label, COUNT(*) AS n FROM "orders" GROUP BY "items" ORDER BY "items"', {"x": "item_label", "y": ["n"]})
    assert chartGuards.guardMessage(request, sql, chart) is None


def test_anUnparsableSqlDeclines():
    request, chart = _chart("bar", CAST_ITEMS, {"x": "items", "y": ["n"]})
    assert chartGuards.guardMessage(request, "SELECT CAST(( FROM", chart) is None


def test_twoProjectionsWithTheLabelNameDecline():
    sql = 'SELECT CAST("items" AS VARCHAR) AS items, "items" AS items, COUNT(*) AS n FROM "orders" GROUP BY ALL'
    request, chart = _chart("bar", CAST_ITEMS, {"x": "items", "y": ["n"]})
    assert chartGuards.guardMessage(request, sql, chart) is None


def test_anUnaliasedCastDeclines():
    # DuckDB names it CAST(items AS VARCHAR), not "items", so its output name cannot be decided here.
    sql = 'SELECT CAST("items" AS VARCHAR), COUNT(*) AS n FROM "orders" GROUP BY ALL'
    request, chart = _chart("bar", CAST_ITEMS, {"x": "items", "y": ["n"]})
    assert chartGuards.guardMessage(request, sql, chart) is None


@pytest.mark.parametrize(
    "labels, fires",
    [
        # Text order that is not numeric order, ascending or descending.
        (["1", "10", "2"], True),
        (["2", "10", "1"], True),
        (["-3", "0", "10", "2", ""], True),
        (["1", "10", "10", "2"], True),
        # A whole number written with a fractional part.
        (["3", "1.0", "2"], True),
        (["12.00"], True),
        (["-4.0", "7"], True),
        (["9", "80", "700", "10"], True),
        (["1", "10", "11", "2"], True),
        # Numeric order in either direction (text order the other way), or neither order, with no whole-number decimal.
        (["2", "10"], False),
        (["10", "2"], False),
        (["8", "16", "128"], False),
        (["128", "16", "8"], False),
        (["9", "80", "700"], False),
        (["1", "2", "10"], False),
        (["10", "2", "1"], False),
        (["2016", "2017", "2018"], False),
        (["2018", "2017"], False),
        (["52", "42", "3", "2"], False),
        (["2", "10", "3"], False),
        (["1.5", "2.25"], False),
        (["7"], False),
        (["", ""], False),
        ([], False),
        # Not all number text.
        (["01026", "1", "10", "2"], False),
        (["1", "10", "1e3"], False),
        (["1.", "10", "2"], False),
        ([" 2", "1", "10"], False),
        (["1", "10", "north"], False),
        (["1.0", "Other"], False),
    ],
)
def test_onlyNumberLabelsInTextOrderOrWithAWholeDecimalFire(labels, fires):
    request = ChartRequest.model_validate({"chartType": "bar", "title": "T", "sql": CAST_ITEMS, "encoding": {"x": "items", "y": ["n"]}})
    chart = {"chartType": "bar", "title": "T", "data": {"labels": labels, "datasets": [{"label": "N", "data": [1] * len(labels)}]}}
    assert (chartGuards.guardMessage(request, CAST_ITEMS, chart) is not None) is fires


def test_aChartOfAnUnexpectedShapeDeclines():
    request = ChartRequest.model_validate({"chartType": "bar", "title": "T", "sql": CAST_ITEMS, "encoding": {"x": "items", "y": ["n"]}})
    for chart in (None, {}, {"data": None}, {"data": {"labels": "1"}}, {"data": {"labels": [1, 2]}}):
        assert chartGuards.guardMessage(request, CAST_ITEMS, chart) is None


# Every point its own series.

PER_POINT = 'SELECT "amount" AS amount, "items" AS items, CAST("order_id" AS VARCHAR) AS order_ref FROM "orders" ORDER BY "order_id"'


@pytest.mark.parametrize("chartType", ["scatter", "bubble"])
def test_everyPointItsOwnSeriesFires(chartType):
    encoding = {"x": "amount", "y": ["items"], "series": "order_ref"}
    if chartType == "bubble":
        encoding["r"] = "items"
    request, chart = _chart(chartType, PER_POINT, encoding)
    assert len(chart["data"]["datasets"]) == 6

    assert chartGuards.guardMessage(request, PER_POINT, chart) == chartGuards.POINT_SERIES_MESSAGE


@pytest.mark.parametrize(
    "sql, encoding",
    [
        # Some series hold several points.
        ('SELECT "amount" AS amount, "items" AS items, "region" AS region FROM "orders" ORDER BY "order_id"', {"x": "amount", "y": ["items"], "series": "region"}),
        # No series.
        ('SELECT "amount" AS amount, "items" AS items FROM "orders" ORDER BY "order_id"', {"x": "amount", "y": ["items"]}),
        # A single point.
        (
            'SELECT "amount" AS amount, "items" AS items, CAST("order_id" AS VARCHAR) AS order_ref FROM "orders" WHERE "order_id" = 1',
            {"x": "amount", "y": ["items"], "series": "order_ref"},
        ),
    ],
)
def test_thePointSeriesGuardDeclines(sql, encoding):
    assert _message("scatter", sql, encoding) is None


def test_thePointSeriesGuardIgnoresCategoryCharts():
    sql = 'SELECT "region" AS region, CAST("order_id" AS VARCHAR) AS order_ref, SUM("amount") AS total FROM "orders" GROUP BY ALL ORDER BY region, order_ref'
    assert _message("bar", sql, {"x": "region", "y": ["total"], "series": "order_ref"}) is None


def test_theMessagesNameWhatTheGuardSawAndTheFix():
    assert chartGuards.castLabelText("n", ["1.0", "10.0", "2.0", "3.0", "4.0"], "1.0") == (
        "Column 'n' casts a number to text, so its labels come out in text order (1.0, 10.0, 2.0, 3.0, ...) and a whole "
        "number shows as '1.0'. Keep it a number: drop the CAST, and when the value is a DECIMAL or DOUBLE holding whole "
        "numbers, CAST it AS BIGINT; order by the numeric value. The backend shows a number label as text, keeps the "
        "query's row order, and shows a whole-number BIGINT or DOUBLE as '3' but a DECIMAL as '3.00'."
    )
    assert chartGuards.POINT_SERIES_MESSAGE == (
        "Each point has its own series value, so every point becomes its own dataset and legend entry. Leave `series` "
        "out when each row is its own point; use it only to group many points into a few datasets, such as a category "
        "or a region."
    )


def test_theBuilderShowsANumberLabelAsTheMessageSays():
    """The message's claims: a whole-number DOUBLE label reads '3', labels keep the query's row order."""
    sql = 'SELECT CAST("amount" AS DOUBLE) AS amount, COUNT(*) AS n FROM "orders" GROUP BY ALL ORDER BY amount DESC'
    _, chart = _chart("bar", sql, {"x": "amount", "y": ["n"]})
    assert chart["data"]["labels"] == ["51", "40", "3", "2", "1"]
