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


def test_compareCharts_wholeNumberTextKeepsFullPrecision():
    big, near = "1234567890123456789", "1234567890123456788"
    assert compareCharts({"chartType": "table", "data": [{"id": big}]}, {"chartType": "table", "data": [{"id": near}]}) == "differ"
    assert compareCharts({"chartType": "table", "data": [{"id": big}]}, {"chartType": "table", "data": [{"id": big}]}) == "match"

    def bar(label):
        return {"chartType": "bar", "data": {"labels": [label], "datasets": [{"label": "n", "data": [1]}]}}

    assert compareCharts(bar(big), bar(near)) == "differ"
    assert compareCharts(bar(big), bar(big)) == "match"
    assert compareCharts({"chartType": "card", "data": big}, {"chartType": "card", "data": near}) == "differ"
    assert compareCharts({"chartType": "card", "data": "+5"}, {"chartType": "card", "data": 5}) == "match"


def test_compareCharts_largeIntegersThatShareSixDigitsStillMatchInAnyOrder():
    a = {"chartType": "table", "data": [{"id": 1000001, "v": 1}, {"id": 1000002, "v": 1}]}
    b = {"chartType": "table", "data": [{"id": 1000002, "v": 1}, {"id": 1000001, "v": 1}]}
    assert compareCharts(a, b) == "match"
    assert compareCharts(_scatter([{"x": 1234567, "y": 1}, {"x": 1234568, "y": 1}]), _scatter([{"x": 1234568, "y": 1}, {"x": 1234567, "y": 1}])) == "match"


def test_compareCharts_floatsThatTieAfterRoundingMatchInAnyOrder():
    def table(*values):
        return {"chartType": "table", "data": [{"v": v, "k": "a"} for v in values]}

    assert compareCharts(table(1.0000004, 1.0000016), table(1.0000016, 1.0000004)) == "match"


def test_compareCharts_aRealDifferenceInsideATieGroupDiffers():
    a = {"chartType": "table", "data": [{"v": 1.0000004, "k": "a"}, {"v": 1.0000016, "k": "a"}]}
    b = {"chartType": "table", "data": [{"v": 1.0000016, "k": "a"}, {"v": 1.000004, "k": "a"}]}
    assert compareCharts(a, b) == "differ"
    wholeA = {"chartType": "table", "data": [{"id": 1000001}, {"id": 1000002}]}
    wholeB = {"chartType": "table", "data": [{"id": 1000002}, {"id": 1000003}]}
    assert compareCharts(wholeA, wholeB) == "differ"


def _tableOf(values):
    return {"chartType": "table", "data": [{"v": v} for v in values]}


def test_compareCharts_threeTiedFloatsMatchWhenTheOrderIsOpposite():
    values = [1234561.5, 1234562.5, 1234563.5]
    assert compareCharts(_tableOf(values), _tableOf(list(reversed(values)))) == "match"


@pytest.mark.parametrize("seed", range(20))
def test_compareCharts_randomAmountsInOneBucketMatchDescAgainstAsc(seed):
    import random

    rng = random.Random(seed)
    values = sorted(round(rng.uniform(1.0e6, 1.1e6), 2) for _ in range(1000))
    assert compareCharts(_tableOf(values), _tableOf(list(reversed(values)))) == "match"


def test_compareCharts_aRealDifferenceInATieGroupOfManyDiffers():
    values = [1234561.5 + i for i in range(50)]
    changed = list(reversed(values))
    changed[10] += 7.0
    assert compareCharts(_tableOf(values), _tableOf(changed)) == "differ"


def test_compareCharts_theWorstCaseForTheTieGroupFinishesQuickly():
    import time

    n = 10000
    rows = [{"v": 1.0 + 1.1e-6 * (i * 8 // n)} for i in range(n)]
    a, b = {"chartType": "table", "data": rows}, {"chartType": "table", "data": list(reversed(rows))}
    start = time.perf_counter()
    assert compareCharts(a, b) == "match"
    assert time.perf_counter() - start < 2.0


def test_compareCharts_theGreedyFallbackStopsAtItsBudget(monkeypatch):
    from nubrix.components import chartCompare

    real = chartCompare._same
    state = {"calls": 0, "depth": 0}

    def counting(a, b, relTol):
        top = state["depth"] == 0
        state["calls"] += top
        state["depth"] += 1
        try:
            return real(a, b, relTol)
        finally:
            state["depth"] -= 1

    monkeypatch.setattr(chartCompare, "_same", counting)
    monkeypatch.setattr(chartCompare, "_GREEDY_BUDGET", 500)
    # The two sides differ in every row of one tie group, so nothing pairs in order and the greedy pass has to scan.
    n = 400
    a = _tableOf([1234561.0 + 0.01 * i for i in range(n)])
    b = _tableOf([1234561.0 + 0.01 * i + 3.0 for i in range(n)])
    assert compareCharts(a, b) == "differ"
    assert state["calls"] <= 500 + n
    assert state["calls"] >= 500


# --- names are not data: dataset labels of a single series and table column names ---


def _bar(label, values, labels=("n", "s")):
    return {"chartType": "bar", "data": {"labels": list(labels), "datasets": [{"label": label, "data": list(values)}]}}


def test_compareCharts_singleDatasetLabelIsIgnored():
    assert compareCharts(_bar("Revenue", [1, 2]), _bar("Total Revenue", [1, 2])) == "match"
    assert compareCharts(_bar("Revenue", [1, 2]), _bar("Total Revenue", [1, 3])) == "differ"


def test_compareCharts_singleScatterDatasetLabelIsIgnored():
    def scatter(label, y):
        return {"chartType": "scatter", "data": {"datasets": [{"label": label, "data": [{"x": 1, "y": y}]}]}}

    assert compareCharts(scatter("a", 2), scatter("b", 2)) == "match"
    assert compareCharts(scatter("a", 2), scatter("b", 3)) == "differ"


def test_compareCharts_oneDatasetAgainstTwoDiffers():
    two = _bar("a", [1, 2])
    two["data"]["datasets"].append({"label": "b", "data": [3, 4]})
    assert compareCharts(_bar("a", [1, 2]), two) == "differ"


def _two(labelA, labelB, a=(1, 2), b=(3, 4)):
    return {
        "chartType": "bar",
        "data": {"labels": ["n", "s"], "datasets": [{"label": labelA, "data": list(a)}, {"label": labelB, "data": list(b)}]},
    }


def test_compareCharts_twoDatasetsStillPairByLabel():
    swapped = _two("y", "x", a=(3, 4), b=(1, 2))
    assert compareCharts(_two("x", "y"), swapped) == "match"
    assert compareCharts(_two("x", "y"), _two("x", "z")) == "differ"
    assert compareCharts(_two("x", "y"), _two("y", "x")) == "differ"


def test_compareCharts_titleAxisLabelsAndCardLabelDoNotCount():
    other = _bar("v", [1, 2])
    other.update({"title": "T2", "xLabels": "X2", "yLabels": "Y2"})
    base = _bar("v", [1, 2])
    base.update({"title": "T1", "xLabels": "X1", "yLabels": "Y1"})
    assert compareCharts(base, other) == "match"
    assert compareCharts({"chartType": "card", "label": "a", "data": 1}, {"chartType": "card", "label": "b", "data": 1}) == "match"


def _table(columns, rows):
    return {"chartType": "table", "columns": columns, "data": [dict(zip(columns, r)) for r in rows]}


def test_compareCharts_e2eTableWithRenamedColumnAndFloatNoise():
    py = _table(["product", "category", "total_revenue"], [["A", "x", 236045.58000000002], ["B", "y", 5.5]])
    sql = _table(["product", "category", "revenue"], [["A", "x", 236045.58], ["B", "y", 5.5]])
    assert compareCharts(py, sql) == "match"


def test_compareCharts_tableRenamedReorderedColumnsAndRows():
    a = _table(["id", "name", "amt"], [[1, "p", 1.5], [2, "q", 2.5], [3, "r", 3.5]])
    b = _table(["value", "who", "n"], [[3.5, "r", 3], [1.5, "p", 1], [2.5, "q", 2]])
    assert compareCharts(a, b) == "match"


def test_compareCharts_tableRenamedColumnWithDifferentValuesDiffers():
    a = _table(["id", "amt"], [[1, 1.5], [2, 2.5]])
    assert compareCharts(a, _table(["id", "total"], [[1, 1.5], [2, 9.5]])) == "differ"
    assert compareCharts(a, _table(["id", "total", "extra"], [[1, 1.5, 0], [2, 2.5, 0]])) == "differ"


def test_compareCharts_tableRowsMustStayTogetherUnderTheMapping():
    a = _table(["a", "b"], [[1, "x"], [2, "y"]])
    assert compareCharts(a, _table(["c", "d"], [["x", 2], ["y", 1]])) == "differ"


def test_compareCharts_tableTiedColumnSignaturesGiveTheCorrectResult():
    a = _table(["p", "q", "r"], [[1, 1, "a"], [2, 2, "b"]])
    assert compareCharts(a, _table(["x", "y", "z"], [["a", 1, 1], ["b", 2, 2]])) == "match"
    # same column multisets, rows pair them differently
    c = _table(["p", "q"], [[1, 2], [2, 1]])
    assert compareCharts(c, _table(["x", "y"], [[2, 1], [1, 2]])) == "match"
    d = _table(["p", "q"], [[1, 1], [2, 2]])
    assert compareCharts(d, _table(["x", "y"], [[1, 2], [2, 1]])) == "differ"


def test_compareCharts_tableTenTiedColumnsFinishFast():
    import time

    cols = [f"a{i}" for i in range(10)]
    rows = [[r] * 10 for r in range(50)]
    a = _table(cols, rows)
    b = _table([f"b{i}" for i in range(10)], rows)
    start = time.monotonic()
    verdict = compareCharts(a, b)
    assert time.monotonic() - start < 1
    assert verdict in ("match", "differ")
