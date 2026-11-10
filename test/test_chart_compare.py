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


def _scatter(points, label="a"):
    return {"chartType": "scatter", "data": {"datasets": [{"label": label, "data": points}]}}


def test_compareCharts_scatterPointOrderDoesNotCount():
    a = _scatter([{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}])
    b = _scatter([{"x": 5, "y": 6}, {"x": 1, "y": 2}, {"x": 3, "y": 4}])
    assert compareCharts(a, b) == "match"
    b["data"]["datasets"][0]["data"][0]["y"] = 7
    assert compareCharts(a, b) == "differ"


def test_compareCharts_bubblePointsAreAMultiset():
    def bubble(points):
        return {"chartType": "bubble", "data": {"datasets": [{"label": "a", "data": points}]}}

    a = bubble([{"x": 1, "y": 2, "r": 3}, {"x": 1, "y": 2, "r": 3}, {"x": 2, "y": 2, "r": 3}])
    assert compareCharts(a, bubble([{"x": 2, "y": 2, "r": 3}, {"x": 1, "y": 2, "r": 3}, {"x": 1, "y": 2, "r": 3}])) == "match"
    assert compareCharts(a, bubble([{"x": 2, "y": 2, "r": 3}, {"x": 2, "y": 2, "r": 3}, {"x": 1, "y": 2, "r": 3}])) == "differ"


def test_compareCharts_tableRowOrderDoesNotCount():
    a = {"chartType": "table", "data": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]}
    b = {"chartType": "table", "data": [{"a": 2, "b": "y"}, {"a": 1, "b": "x"}]}
    assert compareCharts(a, b) == "match"
    b["data"][0]["b"] = "z"
    assert compareCharts(a, b) == "differ"


def test_compareCharts_tableIntegersAreExactAndOthersUseRelTol():
    def table(value):
        return {"chartType": "table", "data": [{"a": value}]}

    assert compareCharts(table(1234567), table(1234570)) == "differ"
    assert compareCharts(table(1234567), table(1234567.0)) == "match"
    assert compareCharts(table(2.5), table(2.5000001)) == "match"
    assert compareCharts(table(2.5), table(2.51)) == "differ"


def test_compareCharts_categoryLabelOrderDoesNotCount():
    other = copy.deepcopy(BAR)
    other["data"]["labels"].reverse()
    for dataset in other["data"]["datasets"]:
        dataset["data"].reverse()
    assert compareCharts(BAR, other) == "match"
    other["data"]["datasets"][0]["data"][0] = 99
    assert compareCharts(BAR, other) == "differ"
    wrongLabel = copy.deepcopy(BAR)
    wrongLabel["data"]["labels"] = ["north", "east"]
    assert compareCharts(BAR, wrongLabel) == "differ"


def test_compareCharts_duplicateLabelsFallBackToOrderedLists():
    a = {"chartType": "bar", "data": {"labels": ["x", "x"], "datasets": [{"label": "n", "data": [1, 2]}]}}
    assert compareCharts(a, copy.deepcopy(a)) == "match"
    swapped = {"chartType": "bar", "data": {"labels": ["x", "x"], "datasets": [{"label": "n", "data": [2, 1]}]}}
    assert compareCharts(a, swapped) == "differ"


def test_compareCharts_nanNoneAndMissingAreEqual():
    a = copy.deepcopy(BAR)
    b = copy.deepcopy(BAR)
    a["data"]["datasets"][1]["data"][1] = float("nan")
    assert compareCharts(a, b) == "match"
    assert compareCharts(_scatter([{"x": 1, "y": float("nan")}]), _scatter([{"x": 1, "y": None}])) == "match"


def test_compareCharts_integersCompareExactly():
    def bar(value):
        return {"chartType": "bar", "data": {"labels": ["a"], "datasets": [{"label": "n", "data": [value]}]}}

    assert compareCharts(bar(1234567), bar(1234568)) == "differ"
    assert compareCharts(bar(1234567), bar(1234567.0)) == "match"
    assert compareCharts(bar(1.0000001), bar(1.0)) == "match"


def test_compareCharts_numericTextEqualsTheNumber():
    assert compareCharts({"chartType": "card", "data": "5"}, {"chartType": "card", "data": 5}) == "match"
    assert compareCharts({"chartType": "card", "data": "5"}, {"chartType": "card", "data": 6}) == "differ"
    assert compareCharts({"chartType": "card", "data": "n/a"}, {"chartType": "card", "data": "n/b"}) == "differ"


def test_compareCharts_midnightDateLabelsEqualTheDate():
    def bar(label):
        return {"chartType": "bar", "data": {"labels": [label], "datasets": [{"label": "n", "data": [1]}]}}

    assert compareCharts(bar("2024-01-01 00:00:00"), bar("2024-01-01")) == "match"
    assert compareCharts(bar("2024-01-01T00:00:00"), bar("2024-01-01")) == "match"
    assert compareCharts(bar("2024-01-01 10:00:00"), bar("2024-01-01")) == "differ"
