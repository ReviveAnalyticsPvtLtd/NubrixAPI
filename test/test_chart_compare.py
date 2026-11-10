"""compareCharts: do the Python chart and the SQL chart hold the same data?"""

import copy

import pytest

from nubrix.components.chartCompare import compareCharts

BAR = {
    "chartType": "bar",
    "title": "Sales",
    "xLabels": "Region",
    "yLabels": "Total",
    "data": {
        "labels": ["north", "south"],
        "datasets": [{"label": "Total", "data": [10.0, 20.0]}, {"label": "Count", "data": [1, None]}],
    },
}


def test_compareCharts_matchesTheSameChart():
    assert compareCharts(BAR, copy.deepcopy(BAR)) == "match"


def test_compareCharts_differsOnAValue():
    other = copy.deepcopy(BAR)
    other["data"]["datasets"][0]["data"][1] = 20.5
    assert compareCharts(BAR, other) == "differ"


def test_compareCharts_differsOnChartType():
    other = copy.deepcopy(BAR)
    other["chartType"] = "line"
    assert compareCharts(BAR, other) == "differ"


def test_compareCharts_ignoresDatasetOrderAndTitles():
    other = copy.deepcopy(BAR)
    other["data"]["datasets"].reverse()
    other["title"] = "Another title"
    assert compareCharts(BAR, other) == "match"


def test_compareCharts_labelsAreComparedAsTextInOrder():
    other = copy.deepcopy(BAR)
    other["data"]["labels"].reverse()
    assert compareCharts(BAR, other) == "differ"
    numbers = {"chartType": "bar", "data": {"labels": [1, 2], "datasets": [{"label": "n", "data": [1, 2]}]}}
    strings = {"chartType": "bar", "data": {"labels": ["1", "2"], "datasets": [{"label": "n", "data": [1, 2]}]}}
    assert compareCharts(numbers, strings) == "match"


def test_compareCharts_toleranceAndNull():
    other = copy.deepcopy(BAR)
    other["data"]["datasets"][0]["data"][0] = 10.0000001
    assert compareCharts(BAR, other) == "match"
    other["data"]["datasets"][0]["data"][0] = 10.001
    assert compareCharts(BAR, other) == "differ"
    assert compareCharts(BAR, other, relTol=1e-3) == "match"
    nullVsZero = copy.deepcopy(BAR)
    nullVsZero["data"]["datasets"][1]["data"][1] = 0
    assert compareCharts(BAR, nullVsZero) == "differ"


def test_compareCharts_card():
    card = {"chartType": "card", "title": "t", "label": "a", "data": 5}
    assert compareCharts(card, {"chartType": "card", "title": "u", "label": "b", "data": 5.0}) == "match"
    assert compareCharts(card, {"chartType": "card", "data": 6}) == "differ"


def test_compareCharts_tableRowsAreTextNormalised():
    a = {"chartType": "table", "columns": ["a", "b"], "data": [{"a": 1, "b": " x "}, {"a": 2.5, "b": None}]}
    b = {"chartType": "table", "columns": ["a", "b"], "data": [{"a": 1.0, "b": "x"}, {"a": 2.5, "b": None}]}
    assert compareCharts(a, b) == "match"
    b["data"][1]["b"] = "y"
    assert compareCharts(a, b) == "differ"
    assert compareCharts(a, {"chartType": "table", "columns": ["a"], "data": [{"a": 1}, {"a": 2.5}]}) == "differ"


def test_compareCharts_pivotKeyByKey():
    a = {"chartType": "pivot", "data": {"x": {"r1": 1.0, "r2": None}}}
    assert compareCharts(a, {"chartType": "pivot", "data": {"x": {"r1": 1, "r2": None}}}) == "match"
    assert compareCharts(a, {"chartType": "pivot", "data": {"x": {"r1": 1, "r3": None}}}) == "differ"
    assert compareCharts(a, {"chartType": "pivot", "data": {"y": {"r1": 1, "r2": None}}}) == "differ"


def test_compareCharts_geoMapPointsAreASet():
    def geo(points):
        return {"chartType": "geoMap", "map": {"mapType": "scatterMap", "data": {"points": points}}}

    a = geo([{"id": "1", "lat": 1.0000001, "long": 2.0, "label": "A"}, {"id": "2", "lat": 3, "long": 4, "label": "B"}])
    b = geo([{"id": "x", "lat": 3, "long": 4, "label": "B"}, {"id": "y", "lat": 1.0000002, "long": 2, "label": "A"}])
    assert compareCharts(a, b) == "match"
    b["map"]["data"]["points"][0]["label"] = "C"
    assert compareCharts(a, b) == "differ"


def test_compareCharts_scatterDatasetsByLabel():
    def scatter(datasets):
        return {"chartType": "scatter", "data": {"datasets": datasets}}

    a = scatter([{"label": "a", "data": [{"x": 1, "y": 2}]}, {"label": "b", "data": [{"x": 3, "y": 4}]}])
    b = scatter([{"label": "b", "data": [{"x": 3, "y": 4}]}, {"label": "a", "data": [{"x": 1, "y": 2.0}]}])
    assert compareCharts(a, b) == "match"
    b["data"]["datasets"][0]["data"][0]["y"] = 5
    assert compareCharts(a, b) == "differ"


@pytest.mark.parametrize("a, b", [({}, {}), (None, BAR), (BAR, "x"), ({"chartType": "bar"}, BAR)])
def test_compareCharts_neverRaises(a, b):
    assert compareCharts(a, b) in ("match", "differ")
