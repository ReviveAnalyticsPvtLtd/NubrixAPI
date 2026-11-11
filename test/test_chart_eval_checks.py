"""The chart evaluation harness without a sandbox: the value check, case validation, `expectSql`, the verdicts, the
Redis and log switches, and the exit codes. Nothing here runs a workflow, so it is fast."""

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from scripts import chartEval

FIXTURES = Path(__file__).parent / "fixtures" / "chart_eval"
DATA = FIXTURES / "data"
CASES = FIXTURES / "cases.jsonl"
STUB = FIXTURES / "modelStub.jsonl"
PROJECT = "11111111-2222-4333-8444-555555555555"

# The value check


def _bar(*datasets, labels=("a", "b")):
    return {"chartType": "bar", "data": {"labels": list(labels), "datasets": [{"label": name, "data": data} for name, data in datasets]}}


def _check(expect, chart):
    return chartEval.checkValues({"relTol": 0, **expect}, chart)


def test_aNullOrMissingFieldIsNotChecked():
    assert _check({}, _bar(("Total", [1, 2])))["ok"] is True
    assert _check({"labels": None, "values": None, "rows": None, "card": None, "points": None}, _bar(("Total", [1, 2])))["ok"] is True


def test_aWrongValueNamesTheFieldTheDatasetAndTheIndex():
    failed = _check({"values": {"Total": [1, 2, 3]}}, _bar(("Total", [1, 2, 9]), labels=("a", "b", "c")))
    assert (failed["ok"], failed["field"], failed["dataset"], failed["index"]) == (False, "values", "Total", 2)
    assert failed["expectedLength"] == failed["gotLength"] == 3
    failed = _check({"labels": ["a", "x"]}, _bar(("Total", [1, 2])))
    assert (failed["field"], failed["index"]) == ("labels", 1)


def test_aBoolIsNeverANumber():
    assert _check({"values": {"T": [1]}}, _bar(("T", [True]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [True]}}, _bar(("T", [1]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [True]}}, _bar(("T", [True]), labels=("a",)))["ok"] is True
    assert _check({"card": True}, {"chartType": "card", "data": 1})["ok"] is False
    assert _check({"card": 1}, {"chartType": "card", "data": True})["ok"] is False
    assert _check({"rows": [{"a": 1}]}, {"chartType": "table", "data": [{"a": True}]})["ok"] is False


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nanAndInfinityNeverMatch(bad):
    assert _check({"values": {"T": [1.5]}, "relTol": 1e-4}, _bar(("T", [bad]), labels=("a",)))["ok"] is False
    assert _check({"card": 2.5, "relTol": 1e-4}, {"chartType": "card", "data": bad})["ok"] is False
    assert _check({"card": bad}, {"chartType": "card", "data": bad})["ok"] is False


def test_anExtraDatasetRowOrLabelFails():
    assert _check({"values": {"A": [1, 2]}}, _bar(("A", [1, 2]), ("B", [3, 4])))["ok"] is False
    assert _check({"values": {"A": [1, 2], "B": [3, 4]}}, _bar(("A", [1, 2])))["ok"] is False
    assert _check({"labels": ["a", "b"]}, _bar(("A", [1, 2, 3]), labels=("a", "b", "c")))["ok"] is False
    assert _check({"labels": ["a", "b", "c"]}, _bar(("A", [1, 2])))["ok"] is False
    assert _check({"values": {"A": [1, 2]}}, _bar(("A", [1, 2, 3]), labels=("a", "b", "c")))["ok"] is False
    table = {"chartType": "table", "data": [{"a": 1}, {"a": 2}, {"a": 3}]}
    failed = _check({"rows": [{"a": 1}, {"a": 2}]}, table)
    assert failed["ok"] is False and failed["index"] == 2
    assert _check({"rows": [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}]}, table)["ok"] is False


def test_orderMatters():
    assert _check({"labels": ["b", "a"]}, _bar(("A", [1, 2])))["ok"] is False
    assert _check({"values": {"A": [2, 1]}}, _bar(("A", [1, 2])))["ok"] is False
    table = {"chartType": "table", "data": [{"a": 1}, {"a": 2}]}
    assert _check({"rows": [{"a": 2}, {"a": 1}]}, table)["ok"] is False


def test_anIntegerIsExactWhateverTheToleranceIs():
    assert _check({"values": {"T": [12345]}, "relTol": 1e-4}, _bar(("T", [12346]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [12345]}, "relTol": 1e-4}, _bar(("T", [12345]), labels=("a",)))["ok"] is True
    assert _check({"values": {"T": [12345]}, "relTol": 1e-4}, _bar(("T", [12345.0]), labels=("a",)))["ok"] is True
    assert _check({"card": 12345, "relTol": 1e-4}, {"chartType": "card", "data": 12346})["ok"] is False
    assert _check({"rows": [{"n": 7}], "relTol": 0.5}, {"chartType": "table", "data": [{"n": 8}]})["ok"] is False
    # A float uses the tolerance; zero stays exact.
    assert _check({"values": {"T": [100.0]}, "relTol": 1e-4}, _bar(("T", [100.005]), labels=("a",)))["ok"] is True
    assert _check({"values": {"T": [100.0]}, "relTol": 1e-4}, _bar(("T", [100.02]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [100.0]}}, _bar(("T", [100.0000001]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [0.0]}, "relTol": 1e-4}, _bar(("T", [1e-9]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [1]}}, _bar(("T", ["1"]), labels=("a",)))["ok"] is False
    assert _check({"values": {"T": [1, None]}}, _bar(("T", [1, 2])))["ok"] is False
    assert _check({"values": {"T": [1, None]}}, _bar(("T", [1, None])))["ok"] is True


def test_aSingleDatasetIsComparedWithoutItsLabel():
    assert _check({"values": {"Total Revenue": [1, 2]}}, _bar(("Revenue", [1, 2])))["ok"] is True
    failed = _check({"values": {"Total Revenue": [1, 2]}}, _bar(("Revenue", [1, 3])))
    assert failed["ok"] is False and failed["dataset"] == "Total Revenue" and failed["index"] == 1


def test_severalDatasetsAreComparedByTheirLabels():
    chart = _bar(("A", [1, 2]), ("B", [3, 4]))
    assert _check({"values": {"B": [3, 4], "A": [1, 2]}}, chart)["ok"] is True
    assert _check({"values": {"A": [1, 2], "C": [3, 4]}}, chart)["ok"] is False
    assert _check({"values": {"A": [3, 4], "B": [1, 2]}}, chart)["ok"] is False
    assert _check({"values": {"A": [1, 2]}}, _bar(("A", [1, 2]), ("A", [1, 2])))["ok"] is False


def test_tableRowsAreComparedByPositionNotByColumnName():
    table = {"chartType": "table", "data": [{"product_name": "Oak", "revenue": 5.0}, {"product_name": "Elm", "revenue": 6.0}]}
    assert _check({"rows": [{"name": "Oak", "total": 5}, {"name": "Elm", "total": 6}], "relTol": 1e-9}, table)["ok"] is True
    assert _check({"rows": [{"a": "Oak", "b": 5}, {"a": "Elm", "b": 7}], "relTol": 1e-9}, table)["ok"] is False
    assert _check({"rows": [{"a": "Oak"}, {"a": "Elm"}]}, table)["ok"] is False
    assert _check({"rows": [{"b": 5, "a": "Oak"}, {"b": 6, "a": "Elm"}]}, table)["ok"] is False
    named = {"rows": [{"product_name": "Oak", "revenue": 5}, {"product_name": "Elm", "revenue": 6}], "columnNames": True}
    assert _check(named, table)["ok"] is True
    named["rows"][0] = {"name": "Oak", "revenue": 5}
    assert _check(named, table)["ok"] is False


def test_aCardIsComparedByItsValueOnly():
    assert _check({"card": 36}, {"chartType": "card", "label": "Whatever", "data": 36})["ok"] is True
    assert _check({"card": "n/a"}, {"chartType": "card", "label": "x", "data": "n/a"})["ok"] is True
    assert _check({"card": 36}, {"chartType": "card", "label": "x", "data": "36"})["ok"] is False


def _geoMap(*points):
    return {"chartType": "geoMap", "title": "T", "map": {"mapType": "scatterMap", "data": {"points": list(points)}}}


def test_geoMapPointsAreComparedInOrderOnTheKeysTheyList():
    chart = _geoMap({"id": "0", "lat": 40.5, "long": -74.0, "label": "Harbor"}, {"id": "1", "lat": 34.1, "long": -118.2, "label": "Hill"})
    both = [{"lat": 40.5, "long": -74.0, "label": "Harbor"}, {"lat": 34.1, "long": -118.2, "label": "Hill"}]
    assert _check({"points": both}, chart)["ok"] is True
    assert _check({"points": [{"lat": 40.5, "long": -74.0}, {"lat": 34.1, "long": -118.2}]}, chart)["ok"] is True
    assert _check({"points": [{"id": "0", "lat": 40.5, "long": -74.0}, {"id": "1", "lat": 34.1, "long": -118.2}]}, chart)["ok"] is True
    assert _check({"points": [{"id": "5", "lat": 40.5, "long": -74.0}, {"lat": 34.1, "long": -118.2}]}, chart)["ok"] is False
    assert _check({"points": both[::-1]}, chart)["ok"] is False
    failed = _check({"points": [both[0], {"lat": 34.1, "long": -118.0, "label": "Hill"}]}, chart)
    assert (failed["ok"], failed["field"], failed["index"]) == (False, "points", 1)
    assert _check({"points": both[:1]}, chart)["ok"] is False
    assert _check({"points": both + both[:1]}, chart)["ok"] is False
    assert _check({"points": [{"lat": 40.5001, "long": -74.0}, both[1]], "relTol": 1e-3}, chart)["ok"] is True
    assert _check({"points": both}, {"chartType": "bar", "data": {}})["ok"] is False
    assert _check({"points": both}, _geoMap())["ok"] is False


def test_scatterAndBubbleGoThroughValuesWithPointDicts():
    scatter = {"chartType": "scatter", "data": {"datasets": [{"label": "S", "data": [{"x": 1, "y": 2.5}, {"x": 2, "y": 3.5}]}]}}
    assert _check({"values": {"Points": [{"x": 1, "y": 2.5}, {"x": 2, "y": 3.5}]}}, scatter)["ok"] is True
    assert _check({"values": {"Points": [{"x": 1, "y": 2.5}, {"x": 2, "y": 3.6}]}}, scatter)["ok"] is False
    assert _check({"values": {"Points": [{"x": 1, "y": 2.5}, {"x": 2, "y": 3.51}]}, "relTol": 1e-2}, scatter)["ok"] is True
    assert _check({"values": {"Points": [{"x": 1}, {"x": 2}]}}, scatter)["ok"] is False
    bubble = {"chartType": "bubble", "data": {"datasets": [{"label": "B", "data": [{"x": 1, "y": 2, "r": 3}]}]}}
    assert _check({"values": {"B": [{"x": 1, "y": 2, "r": 3}]}}, bubble)["ok"] is True
    assert _check({"values": {"B": [{"x": 1, "y": 2, "r": 4}]}}, bubble)["ok"] is False


def test_aPivotUsesRowKeysAsLabelsAndColumnKeysAsDatasets():
    pivot = {"chartType": "pivot", "data": {"c1": {"r1": 1, "r2": None}, "c2": {"r1": 2, "r2": 3}}}
    assert _check({"labels": ["r1", "r2"], "values": {"c1": [1, None], "c2": [2, 3]}}, pivot)["ok"] is True
    assert _check({"labels": ["r2", "r1"]}, pivot)["ok"] is False
    assert _check({"values": {"c1": [1, None], "c3": [2, 3]}}, pivot)["ok"] is False
    assert _check({"values": {"c1": [1, None]}}, pivot)["ok"] is False
    assert _check({"values": {"c2": [2, 4]}}, {"chartType": "pivot", "data": {"c2": {"r1": 2, "r2": 3}}})["ok"] is False
    # A pivot's one column is named by data, so unlike a chart's one dataset its name is compared.
    assert _check({"values": {"other": [2, 3]}}, {"chartType": "pivot", "data": {"c2": {"r1": 2, "r2": 3}}})["ok"] is False


# Case validation: every mistake is a usage error naming the line, and nothing runs


def _caseLine(**expect):
    return json.dumps({"id": "c1", "prompt": "p", "expect": {"doubt": False, "chartType": "bar", **expect}})


def _run(tmp_path, lines, *extra, data=DATA, project=PROJECT):
    cases = tmp_path / "cases.jsonl"
    cases.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    argv = ["--cases", str(cases), "--data", str(data), "--project", project, "--out", str(tmp_path / "report.json"), *extra]
    return chartEval.main(argv)


BAD_LINES = [
    ('{"id": "c1", "prompt": "p", "expect": {"lables": ["zzz"]}}', "unknown expect key"),
    ('{"id": "c1", "prompt": "p", "expect": {"labels": ["a"]}, "extra": 1}', "unknown key"),
    ('{"prompt": "p", "expect": {"labels": ["a"]}}', "id must be"),
    ('{"id": "c1", "expect": {"labels": ["a"]}}', "prompt must be"),
    ('{"id": "c1", "prompt": "p"}', "expect must be an object"),
    ('{"id": "c1", "prompt": "p", "expect": []}', "expect must be an object"),
    ('{"id": "c1", "prompt": "p", "expect": {"doubt": "yes", "labels": ["a"]}}', "doubt must be"),
    (_caseLine(), "must check at least one"),
    (_caseLine(labels=None, values=None, rows=None, card=None), "must check at least one"),
    (_caseLine(values={"A": 5}), "values"),
    (_caseLine(values={}), "values"),
    (_caseLine(values={"A": ["x"]}), "values"),
    (_caseLine(labels="a"), "labels must be"),
    (_caseLine(labels=[1]), "labels must be"),
    (_caseLine(rows={"a": 1}), "rows must be"),
    (_caseLine(rows=[1]), "rows must be"),
    (_caseLine(card=True), "card must be"),
    (_caseLine(card=[1]), "card must be"),
    (_caseLine(labels=["a"], relTol=-1), "relTol"),
    (_caseLine(labels=["a"], relTol="0.1"), "relTol"),
    (_caseLine(labels=["a"], relTol=True), "relTol"),
    (_caseLine(labels=["a"], columnNames="yes"), "columnNames"),
    (_caseLine(labels=["a"], chartType=5), "chartType"),
    (_caseLine(chartType="geoMap", points=[{"long": 1}]), "each point"),
    (_caseLine(chartType="geoMap", points=[{"lat": 1, "long": 2, "zoom": 3}]), "each point"),
    (_caseLine(chartType="geoMap", points=[]), "points must be"),
    (_caseLine(chartType="bar", points=[{"lat": 1, "long": 2}]), "points needs chartType geoMap"),
    (_caseLine(chartType="geoMap", labels=["a"]), "geoMap case needs points"),
    (_caseLine(expectSql="SELECT 1", chartType=None), "needs a chartType"),
    (_caseLine(expectSql="SELECT 1", labels=["a"]), "cannot be combined with labels"),
    (_caseLine(expectSql="SELECT 1", card=1), "cannot be combined with card"),
    (_caseLine(expectSql="SELECT 1", chartType="scatter"), "no form for a scatter"),
    (_caseLine(expectSql="", chartType="bar"), "expectSql must be"),
    (json.dumps({"id": "c1", "prompt": "p", "expect": {"doubt": True, "labels": ["a"]}}), "doubt case checks only"),
    (json.dumps({"id": "c1", "prompt": "p", "expect": {"doubt": True, "chartType": "bar"}}), "doubt case checks only"),
    ("not json", "invalid JSON"),
    ("[1, 2]", "needs a JSON object"),
]


@pytest.mark.parametrize("line, problem", BAD_LINES, ids=[f"{n}-{p[:30]}" for n, (_, p) in enumerate(BAD_LINES)])
def test_aBadCaseLineIsAUsageErrorThatNamesTheLineAndRunsNothing(tmp_path, capsys, monkeypatch, line, problem):
    monkeypatch.setattr(chartEval, "_Engine", lambda *a, **k: pytest.fail("a workflow was built"))
    good = _caseLine(labels=["a"]).replace('"c1"', '"c0"')
    assert _run(tmp_path, [good, line]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert problem in captured.err
    assert "line 2" in captured.err or "on line 2" in captured.err
    assert not (tmp_path / "report.json").exists()


def test_aGoodCaseLineValidates(tmp_path, capsys, monkeypatch):
    ok = [
        _caseLine(labels=["a"]),
        _caseLine(values={"A": [1, None, {"x": 1}]}).replace('"c1"', '"c2"'),
        _caseLine(chartType="geoMap", points=[{"lat": 1, "long": 2, "label": "x", "id": "1"}]).replace('"c1"', '"c3"'),
        _caseLine(chartType="line", expectSql="SELECT 1").replace('"c1"', '"c4"'),
        json.dumps({"id": "c5", "prompt": "p", "expect": {"doubt": True}}),
    ]
    cases, lines = chartEval._loadCases(_write(tmp_path, ok))
    assert [case["id"] for case in cases] == ["c1", "c2", "c3", "c4", "c5"] and lines["c5"] == 5


def _write(tmp_path, lines):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_everyProblemIsReportedWithItsLine(tmp_path, capsys):
    lines = [_caseLine(), _caseLine(labels=["a"]).replace('"c1"', '"c2"'), '{"id": "c2", "prompt": "p", "expect": {"lables": 1}}']
    assert _run(tmp_path, lines) == 2
    err = capsys.readouterr().err
    assert "line 1" in err and "line 3" in err and "id repeats line 2" in err


def test_anEmptyOrBlankCasesFileIsAUsageError(tmp_path, capsys):
    assert _run(tmp_path, []) == 2
    assert "no cases" in capsys.readouterr().err
    assert _run(tmp_path, ["", "  "]) == 2
    assert not (tmp_path / "report.json").exists()


def test_aMissingCasesFileIsAUsageError(tmp_path, capsys):
    argv = ["--cases", str(tmp_path / "none.jsonl"), "--data", str(DATA), "--project", PROJECT, "--out", str(tmp_path / "r.json")]
    assert chartEval.main(argv) == 2
    assert "Cannot read the cases file" in capsys.readouterr().err


def test_theShippedCasesValidate():
    cases, _ = chartEval._loadCases(str(CASES))
    assert len(cases) >= 7 and {case["expect"]["chartType"] for case in cases} >= {"bar", "line", "card", "table", "pivot", "geoMap"}


# Exit codes: 0 all passed, 1 a case failed, 2 a usage or input error


@pytest.mark.parametrize(
    "extra",
    [["--runs", "0"], ["--only", "nope"], ["--engine", "python", "--model-stub", str(STUB)], ["--model-stub", "missing.jsonl"]],
)
def test_usageErrorsExitTwo(tmp_path, capsys, extra):
    assert _run(tmp_path, [_caseLine(labels=["a"])], *extra) == 2
    assert capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_argparseErrorsExitTwo(capsys):
    assert chartEval.main(["--runs", "1"]) == 2
    assert chartEval.main(["--cases", "x", "--data", "y", "--project", "z", "--out", "o", "--engine", "cobol"]) == 2


def test_aStubWithoutACaseIsAUsageError(tmp_path, capsys):
    stub = tmp_path / "stub.jsonl"
    stub.write_text("", encoding="utf-8")
    assert chartEval.main(["--cases", str(CASES), "--data", str(DATA), "--project", PROJECT, "--out", str(tmp_path / "r.json"),
                           "--model-stub", str(stub)]) == 2
    assert "has no entry for" in capsys.readouterr().err


def test_aProjectFolderWithoutMetadataIsAnInputError(tmp_path, capsys):
    (tmp_path / "data" / "p1").mkdir(parents=True)
    argv = ["--cases", str(CASES), "--data", str(tmp_path / "data"), "--project", "p1", "--out", str(tmp_path / "r.json"),
            "--model-stub", str(STUB), "--only", "doubt-unclear"]
    assert chartEval.main(argv) == 2
    assert "could not start" in capsys.readouterr().err


# expectSql: the expected answer of a relative period


def _sources():
    return chartEval._tableSources(DATA / PROJECT)


def test_expectSqlFillsTheExpectedFieldsByShape():
    sources = _sources()
    table = chartEval._expectedFromSql("SELECT product_id, product_name FROM products ORDER BY product_id LIMIT 2", "table", sources)
    assert table == {"rows": [{"product_id": 1, "product_name": "Oak Desk"}, {"product_id": 2, "product_name": "Pine Shelf"}]}
    assert chartEval._expectedFromSql("SELECT COUNT(*) AS n FROM orders", "card", sources) == {"card": 36}
    bar = chartEval._expectedFromSql("SELECT region, COUNT(*) AS orders, SUM(quantity) AS units FROM orders GROUP BY region ORDER BY region", "bar", sources)
    assert bar["labels"] == ["East", "North", "South", "West"] and set(bar["values"]) == {"orders", "units"}
    assert sum(bar["values"]["orders"]) == 36
    geo = chartEval._expectedFromSql("SELECT latitude AS lat, longitude AS long, store_name AS label FROM stores ORDER BY store_id", "geoMap", sources)
    assert geo["points"][0] == {"lat": 40.5, "long": -74.0, "label": "Harbor Store"} and len(geo["points"]) == 3
    with pytest.raises(chartEval._UsageError):
        chartEval._expectedFromSql("SELECT 1 AS only_one", "bar", sources)
    with pytest.raises(chartEval._UsageError):
        chartEval._expectedFromSql("SELECT order_id FROM orders WHERE 1 = 0", "card", sources)
    with pytest.raises(chartEval._UsageError):
        chartEval._expectedFromSql("SELECT store_name FROM stores", "geoMap", sources)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "INSERT INTO orders VALUES (1)",
        "CREATE TABLE t AS SELECT 1",
        "DROP TABLE orders",
        "COPY orders TO 'out.csv'",
        "PRAGMA threads=1",
        "SET enable_external_access = true",
        "ATTACH 'x.db'",
        "SELECT * FROM read_csv('cases.jsonl')",
        "SELECT * FROM read_parquet('" + (DATA / PROJECT / "orders.parquet").as_posix() + "')",
        "SELECT * FROM no_such_table",
        "SELEKT 1",
    ],
)
def test_expectSqlRefusesWhatIsNotOneReadOnlyStatement(sql):
    with pytest.raises(chartEval._UsageError):
        chartEval._expectedFromSql(sql, "card", _sources())


def test_expectSqlAcceptsAWithStatement():
    sql = "WITH t AS (SELECT quantity FROM orders) SELECT SUM(quantity) AS units FROM t"
    assert chartEval._expectedFromSql(sql, "card", _sources()) == {"card": 90}


def _visitsCase(sql):
    return {"id": "rel", "prompt": "p", "expect": {"doubt": False, "chartType": "line", "expectSql": sql, "relTol": 0}}


LAST_12_MONTHS = (
    "SELECT strftime(m, '%Y-%m') AS month, SUM(visits) AS monthly_visits FROM (SELECT date_trunc('month', visit_date) AS m, visits FROM visits) "
    "WHERE m >= date_trunc('month', current_date) - INTERVAL 12 MONTH AND m < date_trunc('month', current_date) GROUP BY m ORDER BY m"
)


def test_aRelativePeriodCountsBackFromToday():
    cases = chartEval._resolveCases([_visitsCase(LAST_12_MONTHS)], {"rel": 1}, DATA / PROJECT)
    today = dt.date.today()
    expect = cases[0]["expect"]
    assert len(expect["labels"]) == 12 and list(expect["values"]) == ["monthly_visits"]
    previousMonth = (today.replace(day=1) - dt.timedelta(days=1))
    assert expect["labels"][-1] == previousMonth.strftime("%Y-%m")
    assert all(isinstance(value, int) for value in expect["values"]["monthly_visits"])


def test_expectSqlIsResolvedOnceBeforeTheRunsAndNamesItsLineWhenWrong():
    cases = [{"id": "a", "prompt": "p", "expect": {"doubt": False, "chartType": "card", "card": 1}}, _visitsCase("SELECT 1; SELECT 2")]
    with pytest.raises(chartEval._UsageError, match="Cases line 2: expectSql must be a single"):
        chartEval._resolveCases(cases, {"a": 1, "rel": 2}, DATA / PROJECT)
    assert chartEval._resolveCases(cases[:1], {"a": 1}, DATA / PROJECT) == cases[:1]


def test_aBadExpectSqlInTheCasesFileExitsTwoBeforeAnyRun(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(chartEval, "_Engine", lambda *a, **k: pytest.fail("a workflow was built"))
    assert _run(tmp_path, [_caseLine(chartType="line", expectSql="DROP TABLE orders")]) == 2
    assert "Cases line 1: expectSql must be a single" in capsys.readouterr().err


# The verdict of one case


def _judged(expect, output, failedStage=None, **response):
    case = {"id": "c", "prompt": "p", "expect": {"doubt": False, "chartType": "bar", **expect}}
    return chartEval._judge(case, {"finalOutput": output, **response}, failedStage)


BAR = {"chartType": "bar", "title": "T", "xLabels": "x", "yLabels": "y", "data": {"labels": ["a"], "datasets": [{"label": "Total", "data": [1]}]}}


def test_aCaseNeedsTheDoubtAnswerTheChartTypeAndACleanContract():
    assert _judged({"labels": ["a"]}, BAR)["stage"] == "ok"
    assert _judged({"chartType": "area"}, BAR)["stage"] == "chartType"
    assert _judged({"doubt": True, "chartType": None}, BAR)["stage"] == "expectedDoubt"
    assert _judged({}, {"response": "Which numbers?"})["stage"] == "unexpectedDoubt"
    assert _judged({}, {"error": "x"}, "build")["stage"] == "build"
    assert _judged({}, {"error": "x"})["stage"] == "error"
    assert _judged({}, "nope")["stage"] == "noOutput"
    assert _judged({}, {"chartType": "bar", "data": "nope"})["stage"] == "contract"
    assert _judged({"labels": ["z"]}, BAR)["stage"] == "value"


def test_aDoubtCasePassesOnlyOnTheQuestionTheRephraserAsked():
    question = "Which numbers would you like to see?"
    genuine = _judged({"doubt": True, "chartType": None}, {"response": question}, rephrasedQuery={"doubt": question, "rephrasedOutput": None})
    assert (genuine["ok"], genuine["stage"], genuine["doubt"]) == (True, "doubt", question)
    # The workflow's fallback for a rephrase with an empty doubt is not a question.
    empty = _judged({"doubt": True, "chartType": None}, {"response": "Query could not be processed."}, rephrasedQuery={"doubt": "", "rephrasedOutput": "x"})
    assert (empty["ok"], empty["stage"]) == (False, "notADoubt") and "fallback" in empty["reason"]
    missing = _judged({"doubt": True, "chartType": None}, {"response": "Query could not be processed."}, rephrasedQuery={"rephrasedOutput": "x"})
    assert (missing["ok"], missing["stage"]) == (False, "notADoubt")
    other = _judged({"doubt": True, "chartType": None}, {"response": "Something else"}, rephrasedQuery={"doubt": question})
    assert other["ok"] is False and other["stage"] == "notADoubt"
    withCode = _judged({"doubt": True, "chartType": None}, {"response": question}, rephrasedQuery={"doubt": question}, generatedCode="print(1)")
    assert withCode["ok"] is False
    # What a generated Python program printed.
    printed = chartEval._judge(
        {"id": "c", "prompt": "p", "expect": {"doubt": True}},
        {"finalOutput": {"response": "42"}, "rephrasedQuery": {"rephrasedOutput": "r", "doubt": None}, "generatedCode": "print(1)", "codeOutput": '{"response": "42"}'},
        None, None, "python",
    )
    assert (printed["ok"], printed["stage"]) == (False, "notADoubt") and "Python engine printed" in printed["reason"]


def test_theReportExplainsAFailure():
    failure = {"stage": "check", "error": "Binder Error: no such column", "repairs": 2}
    result = chartEval._judge(
        {"id": "c", "prompt": "p", "expect": {"doubt": False, "chartType": "bar", "labels": ["a"]}},
        {"finalOutput": {"error": "The query could not be run."}, "generatedCode": '{"sql": "SELECT 1"}', "debugAttempts": 2,
         "rephrasedQuery": {"rephrasedOutput": "Bar of x.", "doubt": None}},
        "check", failure,
    )
    assert result["stage"] == "check" and result["error"] == "The query could not be run."
    assert result["generatedCode"] == '{"sql": "SELECT 1"}' and result["repairs"] == 2
    assert result["failureDetail"] == "Binder Error: no such column" and result["rephrasedOutput"] == "Bar of x."
    assert result["expected"] == {"labels": ["a"], "chartType": "bar"}


def test_theReportHoldsWhatTheChartHadNextToWhatWasExpected():
    result = _judged({"labels": ["a"], "values": {"Total": [2]}}, BAR, generatedCode='{"sql": "SELECT 1"}', debugAttempts=0)
    assert result["stage"] == "value" and result["valueCheck"]["index"] == 0
    assert result["expected"] == {"labels": ["a"], "values": {"Total": [2]}, "chartType": "bar"}
    assert result["got"] == {"labels": ["a"], "values": {"Total": [1]}}
    assert result["generatedCode"] == '{"sql": "SELECT 1"}' and result["repairs"] == 0
    table = _judged({"rows": [{"a": 1}]}, {"chartType": "table", "title": "T", "data": [{"x": 2}]})
    assert table["got"] == {"rows": [{"x": 2}]}
    card = _judged({"card": 1, "chartType": "card"}, {"chartType": "card", "title": "T", "label": "L", "data": 5})
    assert card["got"] == {"card": 5}
    geo = _judged({"chartType": "geoMap", "points": [{"lat": 1, "long": 2}]}, _geoMap({"id": "0", "lat": 1.0, "long": 2.0, "label": "1.0, 2.0"}))
    assert geo["stage"] == "ok" and geo["got"]["points"][0]["lat"] == 1.0


# The environment of a run


class _Spy:
    """A pool someone made earlier in the process: any use of it is recorded."""

    def __init__(self):
        import redis

        self.pool = type("SpyPool", (redis.ConnectionPool,), {"get_connection": lambda me, *a, **k: self.used.append(1) or (_ for _ in ()).throw(AssertionError("used"))})()
        self.used = []


def _slots():
    import importlib

    initMethods = importlib.import_module("utils.initMethods")
    creditModule = importlib.import_module("api.services.credits.creditService")
    return initMethods, creditModule


def test_aRunNeverUsesAPoolThatAlreadyExistsAndPutsItBack(monkeypatch):
    import redis

    initMethods, creditModule = _slots()
    spy = _Spy()
    monkeypatch.setattr(initMethods, "_REDIS_POOL", spy.pool)
    monkeypatch.setattr(creditModule, "_redis_pool", spy.pool)
    with chartEval._noRedis():
        assert initMethods._redis_pool() is not spy.pool
        for client in (redis.Redis(connection_pool=initMethods._redis_pool()), creditModule.creditService._redis()):
            with pytest.raises(redis.exceptions.ConnectionError):
                client.get("anything")
    assert spy.used == []
    assert initMethods._REDIS_POOL is spy.pool and creditModule._redis_pool is spy.pool


def test_theRedisPoolsAreRestoredExactlyEvenWhenTheRunFails(monkeypatch):
    initMethods, creditModule = _slots()
    monkeypatch.setattr(initMethods, "_REDIS_POOL", None)
    monkeypatch.setattr(creditModule, "_redis_pool", None)
    with pytest.raises(RuntimeError):
        with chartEval._noRedis():
            assert initMethods._REDIS_POOL is not None and creditModule._redis_pool is not None
            raise RuntimeError("boom")
    assert initMethods._REDIS_POOL is None and creditModule._redis_pool is None


def test_redisFailsAtOnceAndNoEnvironmentVariableIsNeeded(monkeypatch):
    import time

    import redis

    for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"):
        monkeypatch.setenv(name, "203.0.113.1" if name == "REDIS_HOST" else "6379")
    initMethods, creditModule = _slots()
    monkeypatch.setattr(initMethods, "_REDIS_POOL", None)
    monkeypatch.setattr(creditModule, "_redis_pool", None)
    began = time.monotonic()
    with chartEval._noRedis():
        with pytest.raises(redis.exceptions.ConnectionError):
            creditModule.creditService._redis().zcard("semaphore:p")
    assert time.monotonic() - began < 1


def test_theCodeExecutorsProjectSlotCountsADeadRedisAsTaken():
    from utils.codeExecutor import REPLManager

    with chartEval._noRedis():
        with REPLManager(timeoutSeconds=5)._projectSlot("p1", 0) as acquired:
            assert acquired is True


def _logFrom(moduleName, message):
    """Log `message` as a module of that name would: loguru names a record by the `__name__` of the calling code."""
    exec("from loguru import logger; logger.info(message)", {"__name__": moduleName, "message": message})


def test_theLogSwitchPutsBackExactlyWhatWasThere():
    from loguru import logger

    seen = []
    handler = logger.add(seen.append, format="{message}")
    core = logger._core
    logger.disable("someOtherLibrary")
    before = (list(core.activation_list), dict(core.enabled), core.activation_none)
    try:
        with chartEval._quietLogs():
            _logFrom("utils.fake", "from the run")
            _logFrom("elsewhere.module", "not the run")
        assert [str(message).strip() for message in seen] == ["not the run"]
        assert (list(core.activation_list), dict(core.enabled), core.activation_none) == before
        _logFrom("utils.fake", "after")
        _logFrom("someOtherLibrary.x", "still off")
        assert [str(message).strip() for message in seen] == ["not the run", "after"]
    finally:
        logger.remove(handler)
        logger.enable("someOtherLibrary")


# N1: expectSql values are converted the way the chart builder converts them, so a correct chart matches


def _realChart(sql, chartType, encoding):
    """The chart the real builder makes from the result of `sql` over the fixture tables."""
    import duckdb

    from nubrix.components.chartBuilder import buildChart
    from nubrix.components.chartRequest import ChartRequest

    connection = duckdb.connect(":memory:")
    for name, source in _sources().items():
        connection.register(name, source)
    table = connection.execute(sql).fetch_arrow_table()
    return buildChart(ChartRequest.model_validate({"chartType": chartType, "title": "T", "sql": sql, "encoding": encoding}), table)


def _matches(sql, chartType, encoding):
    expect = {"relTol": 0, **chartEval._expectedFromSql(sql, chartType, _sources())}
    return chartEval.checkValues(expect, _realChart(sql, chartType, encoding)), expect


def test_aDateLabelMatchesTheChartsIsoText():
    sql = ("SELECT CAST(date_trunc('month', visit_date) AS DATE) AS month, SUM(visits) AS v FROM visits "
           "WHERE visit_date < DATE '2020-04-01' GROUP BY 1 ORDER BY 1")
    result, expect = _matches(sql, "line", {"x": "month", "y": ["v"]})
    assert result["ok"] is True, result
    assert expect["labels"] == ["2020-01-01", "2020-02-01", "2020-03-01"]


def test_anIntegerLabelMatchesTheChartsText():
    sql = "SELECT year(visit_date) AS y, SUM(visits) AS v FROM visits GROUP BY 1 ORDER BY 1"
    result, expect = _matches(sql, "bar", {"x": "y", "y": ["v"]})
    assert result["ok"] is True, result
    assert expect["labels"][0] == "2020"


def test_aTableWithADateColumnMatches():
    sql = "SELECT order_id, order_date, revenue FROM orders ORDER BY order_id LIMIT 3"
    result, expect = _matches(sql, "table", {"columns": ["order_id", "order_date", "revenue"]})
    assert result["ok"] is True, result
    assert expect["rows"][0]["order_date"] == "2025-01-05"


def test_aDateCardAndABoolCardMatch():
    result, expect = _matches("SELECT max(order_date) AS d FROM orders", "card", {"value": "d"})
    assert result["ok"] is True, result
    assert expect["card"] == "2025-11-16"
    result, expect = _matches("SELECT count(*) > 3 AS big FROM orders", "card", {"value": "big"})
    assert result["ok"] is True and expect["card"] == "true"


def test_decimalCoordinatesAndNumbersMatch():
    sql = "SELECT CAST(latitude AS DECIMAL(9, 2)) AS lat, CAST(longitude AS DECIMAL(9, 2)) AS long, store_name AS label FROM stores ORDER BY store_id"
    result, expect = _matches(sql, "geoMap", {"lat": "lat", "long": "long", "label": "label"})
    assert result["ok"] is True, result
    sql = "SELECT region, CAST(SUM(revenue) AS DECIMAL(18, 2)) AS total FROM orders GROUP BY region ORDER BY region"
    assert _matches(sql, "bar", {"x": "region", "y": ["total"]})[0]["ok"] is True


def test_aPivotsKeysAreTheBuildersLevelText():
    sql = "SELECT year(order_date) AS y, region, SUM(revenue) AS v FROM orders GROUP BY 1, 2 ORDER BY 1, 2"
    chart = _realChart(sql, "pivot", {"rows": ["y"], "columns": ["region"], "value": "v"})
    wide = ("SELECT year(order_date) AS y, SUM(revenue) FILTER (region = 'East') AS East, SUM(revenue) FILTER (region = 'North') AS North, "
            "SUM(revenue) FILTER (region = 'South') AS South, SUM(revenue) FILTER (region = 'West') AS West FROM orders GROUP BY 1 ORDER BY 1")
    expect = {"relTol": 0, **chartEval._expectedFromSql(wide, "pivot", _sources())}
    assert chartEval.checkValues(expect, chart)["ok"] is True
    assert expect["labels"] == ["2025"]


# N2: the date of an expectSql is read for every run, and a run in which it changed is stale


def _datesFrom(monkeypatch, *dates):
    queue = list(dates)
    current = []

    def today():
        if queue:
            current[:] = [queue.pop(0)]
        return current[0]

    monkeypatch.setattr(chartEval, "_today", today)


def _doubtArgv(tmp_path):
    return ["--cases", str(CASES), "--data", str(DATA), "--project", PROJECT, "--out", str(tmp_path / "r.json"),
            "--model-stub", str(STUB), "--only", "doubt-unclear", "--runs", "2"]


def test_aRunInWhichTheDateChangedIsStaleAndFails(tmp_path, capsys, monkeypatch):
    first, second = dt.date(2026, 11, 11), dt.date(2026, 11, 12)
    # Run 1 begins and ends on different days; run 2 begins and ends on the second.
    _datesFrom(monkeypatch, first, second, second, second)
    assert chartEval.main(_doubtArgv(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["PASS 1/1", "PASS 1/1"]
    assert captured.err.strip().splitlines() == ["python -m scripts.chartEval: the date changed during run 1."]
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert [(run["date"], run["stale"]) for run in report["runs"]] == [("2026-11-11", True), ("2026-11-12", False)]
    assert report["allRunsPass"] is False and "expectedOn" not in report


def test_aSteadyDateLeavesRunsFresh(tmp_path, capsys, monkeypatch):
    _datesFrom(monkeypatch, dt.date(2026, 11, 11))
    assert chartEval.main(_doubtArgv(tmp_path)) == 0
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert [(run["date"], run["stale"]) for run in report["runs"]] == [("2026-11-11", False)] * 2


def test_expectSqlIsResolvedAgainForEveryRun(tmp_path, monkeypatch):
    seen = []
    original = chartEval._resolveCases
    monkeypatch.setattr(chartEval, "_resolveCases", lambda *a, **k: seen.append(1) or original(*a, **k))
    # Preparation resolves once (to fail fast), then each of the two runs resolves again.
    assert chartEval.main(_doubtArgv(tmp_path)) == 0
    assert len(seen) == 3


# N4: no Redis connection can be made during a run, whatever client makes it


def test_aFreshRedisClientFailsAtOnceWithoutASocket(monkeypatch):
    import asyncio
    import socket
    import time

    import redis
    import redis.asyncio
    import redis.connection

    original = redis.connection.AbstractConnection.connect
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("a socket was opened"))
    began = time.monotonic()
    with chartEval._noRedis():
        with pytest.raises(redis.exceptions.ConnectionError):
            redis.Redis(host="203.0.113.1", socket_connect_timeout=5).ping()
        with pytest.raises(redis.exceptions.ConnectionError):
            redis.Redis.from_url("redis://203.0.113.1:6379/0").get("k")
        with pytest.raises(redis.exceptions.ConnectionError):
            asyncio.run(redis.asyncio.Redis(host="203.0.113.1").ping())
    assert time.monotonic() - began < 2
    assert redis.connection.AbstractConnection.connect is original


def test_theConnectPatchIsRestoredWhenTheRunFails():
    import redis.asyncio.connection
    import redis.connection

    def state():
        return (redis.connection.AbstractConnection.connect, redis.connection.AbstractConnection.connect_check_health,
                redis.asyncio.connection.AbstractConnection.connect)

    before = state()
    with pytest.raises(RuntimeError):
        with chartEval._noRedis():
            assert redis.connection.AbstractConnection.connect is not before[0]
            raise RuntimeError("boom")
    assert state() == before


# N5: the storage key comes from the environment, never argv


def test_theStorageKeyFlagIsGone(tmp_path, capsys):
    argv = ["--cases", str(CASES), "--data", str(DATA), "--project", PROJECT, "--out", str(tmp_path / "r.json"), "--storage-key", "k"]
    assert chartEval.main(argv) == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_theStorageKeyComesFromTheEnvironmentOrIsRandom(monkeypatch):
    import os

    monkeypatch.delenv("CHART_EVAL_STORAGE_KEY", raising=False)
    keys = []
    for _ in range(2):
        with chartEval._environment(True, str(DATA)):
            keys.append(os.environ["SUPABASE_KEY"])
    assert keys[0] != keys[1] and all(len(key) > 20 for key in keys)
    monkeypatch.setenv("CHART_EVAL_STORAGE_KEY", "chart-eval-fixture-key")
    with chartEval._environment(True, str(DATA)):
        assert os.environ["SUPABASE_KEY"] == "chart-eval-fixture-key"


def test_theDocsSayTheStorageKeyIsForSyntheticFixturesOnly():
    for text in (chartEval.__doc__, chartEval._parser().format_help()):
        text = " ".join(text.lower().split())
        assert "chart_eval_storage_key" in text and "synthetic" in text and "must never be a real key" in text


# N7: an expectSql geoMap drops the rows the builder drops


def test_aGeoMapExpectSqlSkipsTheRowsTheBuilderSkips():
    sql = (
        "SELECT * FROM (VALUES "
        "(CAST(40.5 AS DOUBLE), CAST(-74.0 AS DOUBLE), 'a', CAST(NULL AS VARCHAR)), "
        "(CAST(NULL AS DOUBLE), CAST(1.0 AS DOUBLE), 'null lat', CAST(NULL AS VARCHAR)), "
        "(CAST(2.0 AS DOUBLE), CAST(NULL AS DOUBLE), 'null long', CAST(NULL AS VARCHAR)), "
        "(CAST(91.0 AS DOUBLE), CAST(1.0 AS DOUBLE), 'lat too big', CAST(NULL AS VARCHAR)), "
        "(CAST(-90.5 AS DOUBLE), CAST(1.0 AS DOUBLE), 'lat too small', CAST(NULL AS VARCHAR)), "
        "(CAST(1.0 AS DOUBLE), CAST(180.5 AS DOUBLE), 'long too big', CAST(NULL AS VARCHAR)), "
        "(CAST(1.0 AS DOUBLE), CAST(-181.0 AS DOUBLE), 'long too small', CAST(NULL AS VARCHAR)), "
        "(CAST('nan' AS DOUBLE), CAST(1.0 AS DOUBLE), 'nan', CAST(NULL AS VARCHAR)), "
        "(CAST('inf' AS DOUBLE), CAST(1.0 AS DOUBLE), 'inf', CAST(NULL AS VARCHAR)), "
        "(CAST(90.0 AS DOUBLE), CAST(180.0 AS DOUBLE), 'edge', CAST(NULL AS VARCHAR)), "
        "(CAST(-90.0 AS DOUBLE), CAST(-180.0 AS DOUBLE), NULL, 'named')"
        ") AS t(lat, long, label, id)"
    )
    chart = _realChart(sql, "geoMap", {"lat": "lat", "long": "long", "label": "label", "id": "id"})
    built = chart["map"]["data"]["points"]
    assert [point["label"] for point in built] == ["a", "edge", "-90.0, -180.0"]
    result, expect = _matches(sql, "geoMap", {"lat": "lat", "long": "long", "label": "label", "id": "id"})
    assert result["ok"] is True, result
    # The id of a row without one is its position in the result, dropped rows included, as the builder has it.
    assert expect["points"] == built
    assert [point["id"] for point in built] == ["0", "9", "named"]


# N6: the Python engine's state carries the metadata the harness read


import types


def test_thePythonEnginesStateCarriesTheKnownMetadataAndTheSqlEnginesDoesNot():
    seen = []

    class Fake:
        def invoke(self, state, config=None):
            seen.append(state)
            return {"finalOutput": {"response": "x"}}

    for name in ("python", "sql"):
        engine = chartEval._Engine.__new__(chartEval._Engine)
        engine.engine, engine.replay, engine.failure, engine.workflow, engine.module = name, object(), None, Fake(), types.SimpleNamespace(logChartFailure=None)
        engine.knownMetadata = {"t": {"shape": [1, 1]}}
        engine.invoke({"t": {}}, "prompt", "p1")
    assert seen[0]["knownMetadata"] == {"t": {"shape": [1, 1]}} and seen[0]["projectId"] == "p1"
    assert "knownMetadata" not in seen[1]
