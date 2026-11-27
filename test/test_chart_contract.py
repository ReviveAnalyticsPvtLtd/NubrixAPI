import copy

import pytest

from nubrix.components.chartContract import contractErrors

CATEGORY_DATA = {"labels": ["a", "b"], "datasets": [{"label": "Total", "data": [1, None]}]}

GOLDEN = {
    "bar": {"chartType": "bar", "title": "t", "xLabels": "X", "yLabels": "Y", "data": CATEGORY_DATA},
    "line": {"chartType": "line", "title": "t", "xLabels": "X", "yLabels": "Y", "data": CATEGORY_DATA},
    "area": {"chartType": "area", "title": "t", "xLabels": "X", "yLabels": "Y", "data": CATEGORY_DATA},
    "histogram": {"chartType": "histogram", "title": "t", "xLabels": "X", "yLabels": "Y", "data": CATEGORY_DATA},
    "radar": {"chartType": "radar", "title": "t", "data": CATEGORY_DATA},
    "pie": {"chartType": "pie", "title": "t", "data": CATEGORY_DATA},
    "doughnut": {"chartType": "doughnut", "title": "t", "data": CATEGORY_DATA},
    "polarArea": {"chartType": "polarArea", "title": "t", "data": CATEGORY_DATA},
    "scatter": {
        "chartType": "scatter",
        "title": "t",
        "xLabels": "X",
        "yLabels": "Y",
        "data": {"datasets": [{"label": "g", "data": [{"x": 1, "y": 2.5}]}]},
    },
    "bubble": {
        "chartType": "bubble",
        "title": "t",
        "xLabels": "X",
        "yLabels": "Y",
        "data": {"datasets": [{"label": "g", "data": [{"x": 1, "y": 2, "r": 3}]}]},
    },
    "card": {"chartType": "card", "title": "t", "label": "", "data": 12.5},
    "table": {
        "chartType": "table",
        "title": "t",
        "columns": ["b", "a"],
        "data": [{"b": 1, "a": "x"}, {"b": None, "a": "y"}],
    },
    "pivot": {
        "chartType": "pivot",
        "title": "t",
        "data": {"('L1', 'L2')": {"r1": 1, "r2": None}},
    },
    "geoMap": {
        "chartType": "geoMap",
        "title": "t",
        "map": {
            "mapType": "scatterMap",
            "data": {"points": [{"id": "1", "lat": 1.5, "long": 2, "label": "Place"}]},
        },
    },
}


@pytest.mark.parametrize("chartType", sorted(GOLDEN))
def test_goldenShapesPass(chartType):
    assert contractErrors(GOLDEN[chartType]) == []


def test_cardWithTextAndLabelPasses():
    assert contractErrors({"chartType": "card", "title": "t", "label": "Total", "data": "n/a"}) == []


def mutated(chartType, change):
    chart = copy.deepcopy(GOLDEN[chartType])
    change(chart)
    return chart


VIOLATIONS = {
    "xLabelsAsList": mutated("bar", lambda c: c.update(xLabels=["X"])),
    "pieWithTwoDatasets": mutated("pie", lambda c: c["data"]["datasets"].append({"label": "b", "data": [1, 2]})),
    "scatterPointWithoutY": mutated("scatter", lambda c: c["data"]["datasets"][0]["data"][0].pop("y")),
    "bubblePointWithoutR": mutated("bubble", lambda c: c["data"]["datasets"][0]["data"][0].pop("r")),
    "tableRowsInOtherOrder": mutated("table", lambda c: c["data"].__setitem__(0, {"a": "x", "b": 1})),
    "pivotValueIsList": mutated("pivot", lambda c: c["data"].__setitem__("('L1', 'L2')", [1, 2])),
    "geoMapEmptyLabel": mutated("geoMap", lambda c: c["map"]["data"]["points"][0].update(label="")),
    "geoMapWithDataKey": mutated("geoMap", lambda c: c.update(data=[])),
    "geoMapWrongMapType": mutated("geoMap", lambda c: c["map"].update(mapType="choropleth")),
    "cardWithoutLabel": mutated("card", lambda c: c.pop("label")),
    "cardDataIsList": mutated("card", lambda c: c.update(data=[1])),
    "barValueIsText": mutated("bar", lambda c: c["data"]["datasets"][0].update(data=["1"])),
    "missingTitle": mutated("bar", lambda c: c.pop("title")),
    "unknownType": {"chartType": "box-plot", "title": "t"},
    "notAnObject": [],
}


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_eachViolationIsReported(name):
    assert contractErrors(VIOLATIONS[name]) != []


def test_geoMapWithoutTitlePasses():
    chart = copy.deepcopy(GOLDEN["geoMap"])
    chart.pop("title")
    assert contractErrors(chart) == []


def test_geoMapWithNonStringTitleIsReported():
    chart = copy.deepcopy(GOLDEN["geoMap"])
    chart["title"] = 5
    assert contractErrors(chart) != []


def test_scatterWithTextXIsReported():
    chart = copy.deepcopy(GOLDEN["scatter"])
    chart["data"]["datasets"][0]["data"] = [{"x": "SP", "y": 2.5}]
    assert contractErrors(chart) != []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    "chartType, change",
    [
        ("bar", lambda c, v: c["data"]["datasets"][0].update(data=[v])),
        ("scatter", lambda c, v: c["data"]["datasets"][0]["data"][0].update(y=v)),
        ("bubble", lambda c, v: c["data"]["datasets"][0]["data"][0].update(r=v)),
        ("card", lambda c, v: c.update(data=v)),
        ("pivot", lambda c, v: c["data"]["('L1', 'L2')"].update(r1=v)),
        ("geoMap", lambda c, v: c["map"]["data"]["points"][0].update(lat=v)),
    ],
)
def test_nonFiniteNumbersAreReported(chartType, change, bad):
    chart = copy.deepcopy(GOLDEN[chartType])
    change(chart, bad)
    assert contractErrors(chart) != []


def test_scatterWithTextYIsReported():
    chart = copy.deepcopy(GOLDEN["scatter"])
    chart["data"]["datasets"][0]["data"] = [{"x": 1, "y": "2"}]
    assert contractErrors(chart) != []


def pivotChart(*keys):
    return {"chartType": "pivot", "title": "t", "data": {key: {"r": 1} for key in keys}}


def test_pivotKeysTheClientReadsAreAccepted():
    assert contractErrors(pivotChart("Q1", "('A', 'B')", "('C', 'D')")) == []


@pytest.mark.parametrize(
    "key",
    ["('Kids (Boys)', '2024')", "('', '2024')", "('Q1', '')", "('A', 'B'"],
)
def test_pivotKeyTheClientWouldMisreadIsReported(key):
    errors = contractErrors(pivotChart(key))
    assert any("misread" in error for error in errors)


def test_pivotKeysOfDifferentDepthAreReported():
    errors = contractErrors(pivotChart("('A', 'B')", "('A', 'B', 'C')"))
    assert any("same depth" in error for error in errors)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), object(), {1: 2}, [float("nan")]])
def test_tableCellMustBeJsonSafe(bad):
    chart = {"chartType": "table", "title": "t", "columns": ["a"], "data": [{"a": bad}]}
    assert any("JSON-safe" in error for error in contractErrors(chart))


def test_tableCellsOfNestedSafeValuesAreAccepted():
    chart = {"chartType": "table", "title": "t", "columns": ["a"], "data": [{"a": {"k": [1, "x", None, True]}}]}
    assert contractErrors(chart) == []
