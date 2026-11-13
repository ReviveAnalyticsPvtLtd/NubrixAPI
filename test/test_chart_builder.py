import datetime
import json
import logging
from decimal import Decimal

import pyarrow as pa
import pytest

from nubrix.components.chartBuilder import ChartBuildError, buildChart, emptyChart, readableLabel
from nubrix.components.chartContract import contractErrors
from nubrix.components.chartRequest import CHART_TYPES, LABEL_CAP, ChartRequest


def makeRequest(chartType, encoding, **extra):
    return ChartRequest(
        chartType=chartType, title="t", sql="SELECT 1", encoding=encoding, **extra
    )


def build(request, table, **kwargs):
    chart = buildChart(request, table, **kwargs)
    assert contractErrors(chart) == []
    return chart


def test_readableLabel():
    assert readableLabel("revenue") == "Revenue"
    assert readableLabel("total_payment_value") == "Total Payment Value"
    assert readableLabel("avgPrice") == "Avg Price"
    assert readableLabel("orderID") == "Order ID"


def test_barWithOneY():
    request = makeRequest("bar", {"x": "state", "y": "revenue"}, xLabel="State")
    table = pa.table({"state": ["SP", "RJ"], "revenue": [10.5, 3]})
    assert build(request, table) == {
        "chartType": "bar",
        "title": "t",
        "xLabels": "State",
        "yLabels": "Revenue",
        "data": {"labels": ["SP", "RJ"], "datasets": [{"label": "Revenue", "data": [10.5, 3]}]},
    }


def test_lineWithSeveralY():
    request = makeRequest(
        "line",
        {"x": "month", "y": ["sales", "total_cost"]},
        datasetLabels={"sales": "Net sales"},
    )
    table = pa.table({"month": ["Jan", "Feb"], "sales": [1, 2], "total_cost": [3, 4]})
    chart = build(request, table)
    assert chart["xLabels"] == "Month"
    assert chart["yLabels"] == "Sales"
    assert chart["data"] == {
        "labels": ["Jan", "Feb"],
        "datasets": [
            {"label": "Net sales", "data": [1, 2]},
            {"label": "Total Cost", "data": [3, 4]},
        ],
    }


def test_seriesLongFormFillsMissingCellsWithNull():
    request = makeRequest("bar", {"x": "month", "y": "n", "series": "grp"})
    table = pa.table({"month": ["Jan", "Jan", "Feb"], "grp": ["a", "b", "a"], "n": [1, 2, 3]})
    chart = build(request, table)
    assert chart["data"] == {
        "labels": ["Jan", "Feb"],
        "datasets": [{"label": "a", "data": [1, 3]}, {"label": "b", "data": [2, None]}],
    }


@pytest.mark.parametrize("lenient", [False, True])
def test_seriesDuplicatePairRaises(lenient):
    request = makeRequest("bar", {"x": "month", "y": "n", "series": "grp"})
    table = pa.table({"month": ["Jan", "Jan"], "grp": ["a", "a"], "n": [1, 2]})
    with pytest.raises(ChartBuildError, match="GROUP BY"):
        buildChart(request, table, lenient=lenient)


def test_radarHasNoAxisLabels():
    request = makeRequest("radar", {"x": "skill", "y": "score"})
    chart = build(request, pa.table({"skill": ["a", "b"], "score": [1, 2]}))
    assert set(chart) == {"chartType", "title", "data"}


@pytest.mark.parametrize("chartType", ["pie", "doughnut", "polarArea"])
def test_pieUsesOneDataset(chartType):
    request = makeRequest(chartType, {"label": "kind", "value": "total_sales"})
    chart = build(request, pa.table({"kind": ["a", "b"], "total_sales": [1, 2]}))
    assert chart == {
        "chartType": chartType,
        "title": "t",
        "data": {"labels": ["a", "b"], "datasets": [{"label": "Total Sales", "data": [1, 2]}]},
    }


@pytest.mark.parametrize("lenient", [False, True])
def test_categoryValueColumnMustBeNumeric(lenient):
    request = makeRequest("bar", {"x": "a", "y": "b"})
    table = pa.table({"a": ["x"], "b": ["not a number"]})
    with pytest.raises(ChartBuildError, match="numeric"):
        buildChart(request, table, lenient=lenient)


def test_missingColumnRaisesWithTheColumnNames():
    request = makeRequest("bar", {"x": "a", "y": "nope"})
    with pytest.raises(ChartBuildError, match="'nope'.*\\['a', 'b'\\]"):
        buildChart(request, pa.table({"a": ["x"], "b": [1]}))


def test_scatterSeriesMakesOneDatasetPerCategory():
    request = makeRequest("scatter", {"x": "w", "y": "h", "series": "kind"})
    table = pa.table({"w": [1, 2, 3], "h": [4.5, 5, 6], "kind": ["a", "b", "a"]})
    chart = build(request, table)
    assert chart == {
        "chartType": "scatter",
        "title": "t",
        "xLabels": "W",
        "yLabels": "H",
        "data": {
            "datasets": [
                {"label": "a", "data": [{"x": 1, "y": 4.5}, {"x": 3, "y": 6}]},
                {"label": "b", "data": [{"x": 2, "y": 5}]},
            ]
        },
    }


def test_scatterWithoutSeriesUsesTheYName():
    request = makeRequest("scatter", {"x": "w", "y": "height_cm"})
    chart = build(request, pa.table({"w": [1], "height_cm": [2]}))
    assert chart["data"]["datasets"] == [{"label": "Height Cm", "data": [{"x": 1, "y": 2}]}]


def test_bubblePointsCarryR():
    request = makeRequest("bubble", {"x": "a", "y": "b", "r": "c"}, xLabel="A axis", yLabel="B axis")
    chart = build(request, pa.table({"a": [1, 2], "b": [3, 4], "c": [5.5, 6]}))
    assert chart["xLabels"] == "A axis"
    assert chart["yLabels"] == "B axis"
    assert chart["data"]["datasets"] == [
        {"label": "B", "data": [{"x": 1, "y": 3, "r": 5.5}, {"x": 2, "y": 4, "r": 6}]}
    ]


@pytest.mark.parametrize("chartType", ["scatter", "bubble"])
@pytest.mark.parametrize(
    "xColumn",
    [pa.array(["a", "b"]), pa.array([datetime.date(2024, 1, 1)] * 2)],
    ids=["text", "date"],
)
@pytest.mark.parametrize("lenient", [False, True])
def test_pointsWithNonNumericColumnRaise(chartType, xColumn, lenient):
    encoding = {"x": "x", "y": "y", **({"r": "r"} if chartType == "bubble" else {})}
    request = makeRequest(chartType, encoding)
    table = pa.table({"x": xColumn, "y": [1, 2], "r": [1, 2]})
    with pytest.raises(ChartBuildError, match="line or bar chart"):
        buildChart(request, table, lenient=lenient)


@pytest.mark.parametrize("lenient", [False, True])
def test_pointsWithNullOrNaNAreDroppedInBothModes(lenient):
    request = makeRequest("bubble", {"x": "x", "y": "y", "r": "r"})
    table = pa.table(
        {
            "x": [1.0, None, 3.0, 4.0, 5.0],
            "y": [1.0, 2.0, float("nan"), 4.0, 5.0],
            "r": [1.0, 2.0, 3.0, None, 5.0],
        }
    )
    chart = build(request, table, lenient=lenient)
    assert chart["data"]["datasets"][0]["data"] == [{"x": 1.0, "y": 1.0, "r": 1.0}, {"x": 5.0, "y": 5.0, "r": 5.0}]


def test_pointsAcceptDecimalAsFloat():
    request = makeRequest("scatter", {"x": "x", "y": "y"})
    table = pa.table({"x": pa.array([Decimal("1.50")], pa.decimal128(5, 2)), "y": [2]})
    point = build(request, table)["data"]["datasets"][0]["data"][0]
    assert point == {"x": 1.5, "y": 2}
    assert isinstance(point["x"], float)


def test_cardSingleValue():
    request = makeRequest("card", {"value": "total_revenue"})
    chart = build(request, pa.table({"total_revenue": [42]}))
    assert chart == {"chartType": "card", "title": "t", "label": "Total Revenue", "data": 42}


def test_cardUsesTheEncodingLabel():
    request = makeRequest("card", {"value": "n", "label": "Orders placed"})
    assert build(request, pa.table({"n": [7]}))["label"] == "Orders placed"


def test_cardTextValueStaysText():
    request = makeRequest("card", {"value": "top"})
    assert build(request, pa.table({"top": ["Brazil"]}))["data"] == "Brazil"


@pytest.mark.parametrize("lenient", [False, True])
def test_cardTwoRowsRaisesEvenWhenLenient(lenient):
    request = makeRequest("card", {"value": "n"})
    with pytest.raises(ChartBuildError, match="LIMIT 1"):
        buildChart(request, pa.table({"n": [1, 2]}), lenient=lenient)


def test_cardEmptyGivesZero():
    request = makeRequest("card", {"value": "n"})
    chart = build(request, pa.table({"n": pa.array([], pa.int64())}))
    assert chart == {"chartType": "card", "title": "t", "label": "N", "data": 0}


def test_cardNullAndNaNGiveZero():
    request = makeRequest("card", {"value": "n"})
    assert build(request, pa.table({"n": pa.array([None], pa.int64())}))["data"] == 0
    assert build(request, pa.table({"n": [float("nan")]}))["data"] == 0


def test_tableRowsFollowColumnsOrder():
    request = makeRequest("table", {"columns": ["b", "a"]})
    chart = build(request, pa.table({"a": [1, 2], "b": ["x", "y"], "c": [0, 0]}))
    assert chart["columns"] == ["b", "a"]
    assert chart["data"] == [{"b": "x", "a": 1}, {"b": "y", "a": 2}]
    assert all(list(row) == ["b", "a"] for row in chart["data"])


def test_tableDefaultColumnsAreQueryOrder():
    request = makeRequest("table", {})
    chart = build(request, pa.table({"z": [1], "a": [2]}))
    assert chart["columns"] == ["z", "a"]
    assert list(chart["data"][0]) == ["z", "a"]


def test_pivotSingleLevel():
    request = makeRequest("pivot", {"rows": ["state"], "columns": ["quarter"], "value": "n"})
    table = pa.table({"state": ["SP", "RJ", "SP"], "quarter": ["Q1", "Q2", "Q2"], "n": [3, 5, 6]})
    assert build(request, table)["data"] == {
        "Q1": {"SP": 3, "RJ": None},
        "Q2": {"SP": 6, "RJ": 5},
    }


def test_pivotMultiLevelUsesTupleTextAndCurlyApostrophe():
    request = makeRequest("pivot", {"rows": ["state", "city"], "columns": ["dept", "year"], "value": "n"})
    table = pa.table(
        {"state": ["SP"], "city": ["Sao Paulo"], "dept": ["Women's"], "year": ["2024"], "n": [1]}
    )
    chart = build(request, table)
    assert chart["data"] == {"('Women’s', '2024')": {"('SP', 'Sao Paulo')": 1}}
    import re

    assert re.findall(r"'([^']+)'", next(iter(chart["data"]))) == ["Women’s", "2024"]


def test_pivotDuplicateCellRaises():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    table = pa.table({"s": ["a", "a"], "q": ["x", "x"], "n": [1, 2]})
    with pytest.raises(ChartBuildError, match="GROUP BY"):
        buildChart(request, table, lenient=True)


def test_pivotTruncatedRaisesEvenWhenLenient():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    table = pa.table({"s": ["a"], "q": ["x"], "n": [1]})
    with pytest.raises(ChartBuildError, match="totals"):
        buildChart(request, table, truncated=True, lenient=True)


def test_geoMapDropsBadCoordinatesAndFillsLabels():
    request = makeRequest("geoMap", {"lat": "lat", "long": "long"})
    table = pa.table(
        {
            "lat": [1.5, None, 95.0, 10.0, float("nan"), 20.0],
            "long": [2.5, 3.0, 4.0, -181.0, 5.0, 30.0],
        }
    )
    chart = build(request, table)
    assert chart == {
        "chartType": "geoMap",
        "title": "t",
        "map": {
            "mapType": "scatterMap",
            "data": {
                "points": [
                    {"id": "0", "lat": 1.5, "long": 2.5, "label": "1.5, 2.5"},
                    {"id": "5", "lat": 20.0, "long": 30.0, "label": "20.0, 30.0"},
                ]
            },
        },
    }


def test_geoMapUsesLabelAndIdColumns():
    request = makeRequest("geoMap", {"lat": "a", "long": "b", "label": "name", "id": "code"})
    table = pa.table({"a": [1, 2], "b": [3, 4], "name": ["Shop", None], "code": [101, None]})
    points = build(request, table)["map"]["data"]["points"]
    assert points == [
        {"id": "101", "lat": 1, "long": 3, "label": "Shop"},
        {"id": "1", "lat": 2, "long": 4, "label": "2, 4"},
    ]


def test_geoMapNonFiniteCoordinatesAreDroppedButKeepTheTitle():
    request = makeRequest("geoMap", {"lat": "a", "long": "b"})
    chart = build(request, pa.table({"a": [float("nan")], "b": [float("inf")]}))
    assert chart["map"]["data"]["points"] == []
    assert chart["title"] == "t"


def test_valuesAreJsonSafe():
    request = makeRequest("table", {})
    table = pa.table(
        {
            "dec": pa.array([Decimal("1.25")], pa.decimal128(5, 2)),
            "nan": [float("nan")],
            "inf": [float("inf")],
            "ninf": [float("-inf")],
            "day": pa.array([datetime.date(2024, 1, 31)]),
            "ts": pa.array([datetime.datetime(2024, 1, 31, 10, 30)]),
            "exact": [2**53],
            "big": [2**53 + 1],
            "none": pa.array([None], pa.int64()),
        }
    )
    row = build(request, table)["data"][0]
    assert row == {
        "dec": 1.25,
        "nan": None,
        "inf": None,
        "ninf": None,
        "day": "2024-01-31",
        "ts": "2024-01-31T10:30:00",
        "exact": 2**53,
        "big": "9007199254740993",
        "none": None,
    }
    assert isinstance(row["dec"], float)


def test_cardBigIntegerBecomesText():
    request = makeRequest("card", {"value": "n"})
    assert build(request, pa.table({"n": [2**53 + 1]}))["data"] == "9007199254740993"


def test_categoryValuesAreJsonSafeAndLabelsAreText():
    request = makeRequest("line", {"x": "day", "y": "v"})
    table = pa.table(
        {
            "day": pa.array([datetime.date(2024, 1, 31), datetime.date(2024, 2, 1), None]),
            "v": pa.array([Decimal("1.5"), None, Decimal("2")], pa.decimal128(5, 1)),
        }
    )
    data = build(request, table)["data"]
    assert data["labels"] == ["2024-01-31", "2024-02-01", ""]
    assert data["datasets"][0]["data"] == [1.5, None, 2.0]
    nonFinite = pa.table({"day": [1, 2, 3], "v": [float("nan"), float("inf"), 2.5]})
    assert build(request, nonFinite)["data"]["datasets"][0]["data"] == [None, None, 2.5]
    bigInts = pa.table({"day": [1, 2], "v": pa.array([2**53, 2**53 + 1], pa.int64())})
    assert build(request, bigInts)["data"]["datasets"][0]["data"] == [2**53, float(2**53 + 1)]
    assert all(isinstance(label, str) for label in build(request, nonFinite)["data"]["labels"])


@pytest.mark.parametrize("withSeries", [False, True])
def test_categoryOverLabelCapRaisesWhenStrictAndTruncatesWhenLenient(withSeries, caplog):
    count = LABEL_CAP + 1
    columns = {"x": [f"l{i}" for i in range(count)], "y": list(range(count))}
    encoding = {"x": "x", "y": "y"}
    if withSeries:
        columns["s"] = ["only"] * count
        encoding["series"] = "s"
    request = makeRequest("bar", encoding)
    table = pa.table(columns)
    with pytest.raises(ChartBuildError, match=f"LIMIT {LABEL_CAP}"):
        buildChart(request, table)
    with caplog.at_level(logging.WARNING):
        chart = build(request, table, lenient=True)
    assert chart["data"]["labels"] == [f"l{i}" for i in range(LABEL_CAP)]
    assert chart["data"]["datasets"][0]["data"] == list(range(LABEL_CAP))
    assert "keeping the first 500 labels" in caplog.text


def test_categoryAtLabelCapIsFine():
    request = makeRequest("bar", {"x": "x", "y": "y"})
    table = pa.table({"x": [f"l{i}" for i in range(LABEL_CAP)], "y": list(range(LABEL_CAP))})
    assert len(build(request, table)["data"]["labels"]) == LABEL_CAP


def test_categoryTruncatedFollowsTheSameRule(caplog):
    request = makeRequest("bar", {"x": "x", "y": "y"})
    table = pa.table({"x": ["a", "b"], "y": [1, 2]})
    with pytest.raises(ChartBuildError, match="ORDER BY"):
        buildChart(request, table, truncated=True)
    with caplog.at_level(logging.WARNING):
        assert build(request, table, truncated=True, lenient=True)["data"]["labels"] == ["a", "b"]
    assert caplog.text


@pytest.mark.parametrize("chartType", ["table", "geoMap", "scatter", "bubble"])
def test_truncatedChangesNothingForRowCappedTypes(chartType):
    encodings = {
        "table": {},
        "geoMap": {"lat": "a", "long": "b"},
        "scatter": {"x": "a", "y": "b"},
        "bubble": {"x": "a", "y": "b", "r": "c"},
    }
    request = makeRequest(chartType, encodings[chartType])
    table = pa.table({"a": [1], "b": [2], "c": [3]})
    assert build(request, table, truncated=True) == build(request, table)


EMPTY_ENCODINGS = {
    "bar": {"x": "a", "y": "b"},
    "line": {"x": "a", "y": "b"},
    "area": {"x": "a", "y": "b"},
    "histogram": {"x": "a", "y": "b"},
    "radar": {"x": "a", "y": "b"},
    "pie": {"label": "a", "value": "b"},
    "doughnut": {"label": "a", "value": "b"},
    "polarArea": {"label": "a", "value": "b"},
    "scatter": {"x": "a", "y": "b"},
    "bubble": {"x": "a", "y": "b", "r": "c"},
    "card": {"value": "a"},
    "table": {},
    "pivot": {"rows": ["a"], "columns": ["b"], "value": "c"},
    "geoMap": {"lat": "a", "long": "b"},
}


@pytest.mark.parametrize("chartType", CHART_TYPES)
def test_emptyResultGivesEmptyChartOfEachType(chartType):
    request = makeRequest(chartType, EMPTY_ENCODINGS[chartType], xLabel="X axis", yLabel="Y axis")
    table = pa.table({name: pa.array([], pa.float64()) for name in "abc"})
    chart = build(request, table)
    expected = emptyChart(chartType, "t", "X axis", "Y axis")
    if chartType == "card":
        expected["label"] = "A"
    assert chart == expected


def test_emptyChartShapes():
    assert emptyChart("bar", "t") == {
        "chartType": "bar",
        "title": "t",
        "xLabels": "",
        "yLabels": "",
        "data": {"labels": [], "datasets": []},
    }
    assert emptyChart("pivot", "t")["data"] == {}
    assert emptyChart("table", "t")["columns"] == []
    assert emptyChart("geoMap", "t")["title"] == "t"
    with pytest.raises(ValueError):
        emptyChart("sankey", "t")


@pytest.mark.parametrize("chartType", CHART_TYPES)
def test_everyEmptyChartPassesTheContract(chartType):
    assert contractErrors(emptyChart(chartType, "t")) == []


def test_geoMapOutputAlwaysCarriesATitle():
    request = makeRequest("geoMap", {"lat": "a", "long": "b"})
    full = buildChart(request, pa.table({"a": [1], "b": [2]}))
    empty = buildChart(request, pa.table({"a": pa.array([], pa.int64()), "b": pa.array([], pa.int64())}))
    assert full["title"] == "t"
    assert empty["title"] == "t"


@pytest.mark.parametrize("lenient", [False, True])
def test_pivotKeysSurviveTheClientParsers(lenient):
    request = makeRequest("pivot", {"rows": ["r"], "columns": ["a", "b"], "value": "n"})
    table = pa.table(
        {
            "r": ["Kids (Boys)", None, ""],
            "a": ["Kids (Boys)", "", None],
            "b": ["2024", "Q1", ""],
            "n": [1, 2, 3],
        }
    )
    chart = build(request, table, lenient=lenient)
    assert chart["data"] == {
        "('Kids （Boys）', '2024')": {"Kids （Boys）": 1, "—": None},
        "('—', 'Q1')": {"Kids （Boys）": None, "—": 2},
        "('—', '—')": {"Kids （Boys）": None, "—": 3},
    }


def test_pivotSingleLevelKeysAreCleanedToo():
    request = makeRequest("pivot", {"rows": ["r"], "columns": ["c"], "value": "n"})
    table = pa.table({"r": ["a (b)"], "c": [None], "n": [1]})
    assert build(request, table)["data"] == {"—": {"a （b）": 1}}


def test_cardBooleanBecomesLowercaseText():
    request = makeRequest("card", {"value": "ok"})
    assert build(request, pa.table({"ok": [True]}))["data"] == "true"
    assert build(request, pa.table({"ok": [False]}))["data"] == "false"


@pytest.mark.parametrize("lenient", [False, True])
def test_cardListValueRaises(lenient):
    request = makeRequest("card", {"value": "v"})
    with pytest.raises(ChartBuildError, match="single number or text"):
        buildChart(request, pa.table({"v": [[1, 2]]}), lenient=lenient)


def test_emptyCardKeepsItsLabel():
    request = makeRequest("card", {"value": "total_revenue"})
    empty = pa.table({"total_revenue": pa.array([], pa.int64())})
    assert build(request, empty)["label"] == "Total Revenue"
    labelled = makeRequest("card", {"value": "n", "label": "Orders"})
    assert build(labelled, pa.table({"n": pa.array([], pa.int64())}))["label"] == "Orders"


def test_geoMapStrictRejectsNonNumericCoordinateColumns():
    request = makeRequest("geoMap", {"lat": "a", "long": "b"})
    with pytest.raises(ChartBuildError, match="CAST"):
        buildChart(request, pa.table({"a": ["1"], "b": [2]}))


def test_wholeNumberFloatLabelsPrintWithoutDecimals():
    request = makeRequest("bar", {"x": "x", "y": "y"})
    table = pa.table({"x": [10.0, 2.5, 3], "y": [1, 2, 3]})
    assert build(request, table)["data"]["labels"] == ["10", "2.5", "3"]


def test_integerValuedDecimalActsLikeAnInt():
    request = makeRequest("table", {})
    kind = pa.decimal128(38, 0)
    table = pa.table({"small": pa.array([Decimal(5)], kind), "big": pa.array([Decimal(2**53 + 1)], kind)})
    row = build(request, table)["data"][0]
    assert row == {"small": 5, "big": "9007199254740993"}
    assert isinstance(row["small"], int)
    card = makeRequest("card", {"value": "big"})
    assert build(card, table.select(["big"]))["data"] == "9007199254740993"


def test_lenientTruncatedWithinCapSaysDataIsIncomplete(caplog):
    request = makeRequest("bar", {"x": "x", "y": "y"})
    with caplog.at_level(logging.WARNING):
        buildChart(request, pa.table({"x": ["a"], "y": [1]}), truncated=True, lenient=True)
    assert "incomplete" in caplog.text
    assert "keeping the first" not in caplog.text


def pivotLong(rowName, columnName, pairs):
    return pa.table(
        {
            rowName: [pair[0] for pair in pairs],
            columnName: [pair[1] for pair in pairs],
            "n": list(range(1, len(pairs) + 1)),
        }
    )


def test_pivotRowsMissingFromFirstColumnAreStillSorted():
    request = makeRequest("pivot", {"rows": ["status"], "columns": ["year"], "value": "n"})
    pairs = [("canceled", "2016"), ("delivered", "2016"), ("approved", "2017"), ("canceled", "2017"),
             ("created", "2017"), ("delivered", "2017")]
    chart = build(request, pivotLong("status", "year", pairs))
    assert list(chart["data"]) == ["2016", "2017"]
    assert list(chart["data"]["2016"]) == ["approved", "canceled", "created", "delivered"]
    assert chart["data"]["2016"] == {"approved": None, "canceled": 1, "created": None, "delivered": 2}
    assert chart["data"]["2017"] == {"approved": 3, "canceled": 4, "created": 5, "delivered": 6}


def test_pivotColumnsMissingFromFirstRowAreStillSorted():
    request = makeRequest("pivot", {"rows": ["year"], "columns": ["status"], "value": "n"})
    pairs = [("2016", "canceled"), ("2016", "delivered"), ("2017", "approved"), ("2017", "canceled"),
             ("2017", "created"), ("2017", "delivered")]
    chart = build(request, pivotLong("year", "status", pairs))
    assert list(chart["data"]) == ["approved", "canceled", "created", "delivered"]
    assert chart["data"]["canceled"] == {"2016": 1, "2017": 4}
    assert chart["data"]["approved"] == {"2016": None, "2017": 3}


def test_pivotConsistentInputKeepsFirstAppearanceOutput():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    pairs = [("a", "x"), ("b", "x"), ("c", "x"), ("a", "y"), ("b", "y"), ("c", "y")]
    data = build(request, pivotLong("s", "q", pairs))["data"]
    assert data == {"x": {"a": 1, "b": 2, "c": 3}, "y": {"a": 4, "b": 5, "c": 6}}
    assert list(data) == ["x", "y"]
    assert [list(column) for column in data.values()] == [["a", "b", "c"], ["a", "b", "c"]]
    assert json.dumps(data) == '{"x": {"a": 1, "b": 2, "c": 3}, "y": {"a": 4, "b": 5, "c": 6}}'


def test_pivotConflictingGroupsFallBackToFirstAppearance():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    pairs = [("a", "x"), ("b", "x"), ("b", "y"), ("a", "y"), ("c", "y")]
    data = build(request, pivotLong("s", "q", pairs))["data"]
    assert list(data) == ["x", "y"]
    assert [list(column) for column in data.values()] == [["a", "b", "c"], ["a", "b", "c"]]


def test_pivotMultiLevelKeysUseTheSameMerge():
    request = makeRequest("pivot", {"rows": ["state", "city"], "columns": ["year"], "value": "n"})
    table = pa.table(
        {
            "state": ["SP", "SP", "RJ", "SP", "SP"],
            "city": ["Campinas", "Santos", "Niteroi", "Bauru", "Campinas"],
            "year": ["2016", "2016", "2017", "2017", "2017"],
            "n": [1, 2, 3, 4, 5],
        }
    )
    data = build(request, table)["data"]
    # 2016 gives Campinas then Santos; 2017 gives Niteroi, Bauru, Campinas.
    order = ["('RJ', 'Niteroi')", "('SP', 'Bauru')", "('SP', 'Campinas')", "('SP', 'Santos')"]
    assert list(data) == ["2016", "2017"]
    assert list(data["2016"]) == order
    assert list(data["2017"]) == order
    assert data["2016"] == {order[0]: None, order[1]: None, order[2]: 1, order[3]: 2}
    assert data["2017"] == {order[0]: 3, order[1]: 4, order[2]: 5, order[3]: None}


def test_pivotOutputIsDeterministic():
    request = makeRequest("pivot", {"rows": ["status"], "columns": ["year"], "value": "n"})
    pairs = [("canceled", "2016"), ("delivered", "2016"), ("approved", "2017"), ("canceled", "2017"),
             ("created", "2017"), ("delivered", "2017")]
    first = json.dumps(build(request, pivotLong("status", "year", pairs)))
    second = json.dumps(build(request, pivotLong("status", "year", pairs)))
    assert first == second
    assert first.index('"approved"') < first.index('"canceled"') < first.index('"created"')


def test_pivotCycleInOneDimensionStillMergesTheOther():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    # Rows conflict (a,b in column y; b,a in column x), so rows keep first appearance.
    # Columns: row a gives y,x; row c gives z,y -> merged z,y,x although y appeared first.
    pairs = [("a", "y"), ("b", "y"), ("b", "x"), ("a", "x"), ("c", "z"), ("c", "y")]
    data = build(request, pivotLong("s", "q", pairs))["data"]
    assert list(data) == ["z", "y", "x"]
    assert [list(column) for column in data.values()] == [["a", "b", "c"]] * 3


def test_pivotDisjointGroupsKeepFirstAppearance():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    pairs = [("b", "x"), ("a", "x"), ("d", "y"), ("c", "y")]
    data = build(request, pivotLong("s", "q", pairs))["data"]
    assert list(data) == ["x", "y"]
    assert [list(column) for column in data.values()] == [["b", "a", "d", "c"]] * 2


def test_pivotSingleColumnAndEmptyResult():
    request = makeRequest("pivot", {"rows": ["s"], "columns": ["q"], "value": "n"})
    data = build(request, pivotLong("s", "q", [("b", "x"), ("a", "x"), ("c", "x")]))["data"]
    assert data == {"x": {"b": 1, "a": 2, "c": 3}}
    assert list(data["x"]) == ["b", "a", "c"]
    empty = pa.table({"s": pa.array([], pa.string()), "q": pa.array([], pa.string()), "n": pa.array([], pa.int64())})
    assert buildChart(request, empty)["data"] == {}
