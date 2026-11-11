"""The chart evaluation harness, run on its synthetic tables with the model replaced by recorded replies.

Everything after the model is real: the table reads through the local storage server, the check, the sandbox child,
the builder and the contract check.
"""

import json
import os
from pathlib import Path

import pytest

from scripts import chartEval

FIXTURES = Path(__file__).parent / "fixtures" / "chart_eval"
DATA = FIXTURES / "data"
CASES = FIXTURES / "cases.jsonl"
STUB = FIXTURES / "modelStub.jsonl"
PROJECT = "11111111-2222-4333-8444-555555555555"


def _args(tmp_path, *extra, cases=CASES, stub=STUB, runs=2):
    return [
        "--cases", str(cases), "--data", str(DATA), "--project", PROJECT, "--engine", "sql",
        "--runs", str(runs), "--out", str(tmp_path / "report.json"), "--model-stub", str(stub), *extra,
    ]


def _report(tmp_path):
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


def _editedCases(tmp_path, caseId, edit):
    lines = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        if case["id"] == caseId:
            edit(case["expect"])
        lines.append(json.dumps(case))
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_harnessPassesOnSyntheticSetWithStubbedModel(tmp_path, capsys):
    assert chartEval.main(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["PASS 6/6", "PASS 6/6", "ALL RUNS PASS"]
    report = _report(tmp_path)
    assert [run["passed"] for run in report["runs"]] == [6, 6]
    assert report["engine"] == "sql"
    for run in report["runs"]:
        assert {case["id"]: case["stage"] for case in run["cases"]} == {
            "bar-region": "ok", "line-last-12-months": "ok", "card-order-count": "ok",
            "table-top-products": "ok", "pivot-region-category": "ok", "doubt-unclear": "doubt",
        }
        for case in run["cases"]:
            assert case["ok"] is True and case["contractErrors"] == [] and case["durationSeconds"] >= 0
    assert {case["chartType"] for case in report["runs"][0]["cases"]} >= {"bar", "line", "card", "table", "pivot"}


def test_wrongValueFailsTheCase(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "kept")

    def edit(expect):
        expect["values"]["Total Revenue"][2] += 5

    cases = _editedCases(tmp_path, "bar-region", edit)
    assert chartEval.main(_args(tmp_path, "--only", "bar-region", cases=cases, runs=1)) == 1
    out = capsys.readouterr().out
    assert out.splitlines() == ["PASS 0/1", "FAILED bar-region"]
    case = _report(tmp_path)["runs"][0]["cases"][0]
    assert case["ok"] is False and case["stage"] == "value"
    assert case["valueCheck"]["field"] == "values"
    assert case["valueCheck"]["dataset"] == "Total Revenue"
    assert case["valueCheck"]["expectedType"] == "list" and case["valueCheck"]["gotType"] == "list"
    assert case["valueCheck"]["expectedLength"] == case["valueCheck"]["gotLength"] == 4
    # The run's environment is gone with it.
    assert os.environ["SUPABASE_URL"] == "http://example.invalid" and os.environ["SUPABASE_KEY"] == "kept"


def test_contractErrorFailsTheCase(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chartEval, "contractErrors", lambda chart: ["data: broken"])
    assert chartEval.main(_args(tmp_path, "--only", "card-order-count", runs=1)) == 1
    case = _report(tmp_path)["runs"][0]["cases"][0]
    assert case["ok"] is False and case["stage"] == "contract" and case["contractErrors"] == ["data: broken"]
    assert "ALL RUNS PASS" not in capsys.readouterr().out


def test_aStubThatRepliesWithBadSqlFailsAtItsStage(tmp_path):
    stub = tmp_path / "stub.jsonl"
    lines = []
    for line in STUB.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry["id"] == "bar-region":
            entry["reply"] = entry["reply"].replace("FROM orders", "FROM no_such_table")
        lines.append(json.dumps(entry))
    stub.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert chartEval.main(_args(tmp_path, "--only", "bar-region", stub=stub, runs=1)) == 1
    case = _report(tmp_path)["runs"][0]["cases"][0]
    assert case["ok"] is False and case["stage"] in ("check", "bind")


def test_pythonEngineRefusesAModelStub(tmp_path, capsys):
    argv = _args(tmp_path)
    argv[argv.index("--engine") + 1] = "python"
    with pytest.raises(SystemExit) as exit_:
        chartEval.main(argv)
    assert exit_.value.code == 2
    assert "--model-stub" in capsys.readouterr().err


def test_aStubWithoutACaseIsAUsageError(tmp_path):
    stub = tmp_path / "stub.jsonl"
    stub.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        chartEval.main(_args(tmp_path, stub=stub))


def _judged(expect, output, failedStage=None):
    case = {"id": "c", "prompt": "p", "expect": {"doubt": False, "chartType": "bar", **expect}}
    return chartEval._judge(case, {"finalOutput": output}, failedStage)


def test_aCaseNeedsTheDoubtAnswerTheChartTypeAndACleanContract():
    chart = {"chartType": "bar", "title": "T", "xLabels": "x", "yLabels": "y", "data": {"labels": ["a"], "datasets": [{"label": "Total", "data": [1]}]}}
    assert _judged({}, chart)["stage"] == "ok"
    assert _judged({"chartType": "area"}, chart)["stage"] == "chartType"
    assert _judged({"doubt": True}, chart)["stage"] == "expectedDoubt"
    doubt = {"response": "Which numbers?"}
    assert _judged({"doubt": True, "chartType": None}, doubt) == {
        "id": "c", "ok": True, "stage": "doubt", "chartType": None, "contractErrors": [], "valueCheck": None,
    }
    assert _judged({}, doubt)["stage"] == "unexpectedDoubt" and _judged({}, doubt)["ok"] is False
    assert _judged({}, {"error": "x"}, "build")["stage"] == "build"
    assert _judged({}, {"error": "x"})["stage"] == "error"
    assert _judged({}, {"chartType": "bar", "data": "nope"})["stage"] == "contract"


# The value check

CHART = {"chartType": "bar", "data": {"labels": ["a", "b"], "datasets": [{"label": "Total", "data": [1, 2.5]}]}}


def _check(expect, chart=CHART):
    return chartEval.checkValues({"labels": None, "values": None, "rows": None, "card": None, "relTol": 0, **expect}, chart)


def test_valueCheckComparesLabelsInOrder():
    assert _check({"labels": ["a", "b"]})["ok"] is True
    failed = _check({"labels": ["b", "a"]})
    assert failed["ok"] is False and failed["field"] == "labels"
    assert _check({"labels": ["a"]})["gotLength"] == 2


def test_valueCheckMatchesDatasetsByExactLabelAndUsesTheTolerance():
    assert _check({"values": {"Total": [1, 2.5]}})["ok"] is True
    assert _check({"values": {"total": [1, 2.5]}})["ok"] is False
    assert _check({"values": {"Total": [1, 2.5], "Other": [1, 2]}})["ok"] is False
    assert _check({"values": {"Total": [1, 2.5000001]}})["ok"] is False
    assert _check({"values": {"Total": [1, 2.5000001]}, "relTol": 1e-4})["ok"] is True
    assert _check({"values": {"Total": [2, 2.5]}, "relTol": 1e-4})["ok"] is False
    assert _check({"values": {"Total": [1, None]}})["ok"] is False


def test_valueCheckTreatsNullFieldsAsUnchecked():
    assert _check({})["ok"] is True


def test_valueCheckCardRowsAndText():
    card = {"chartType": "card", "label": "x", "data": 36}
    assert _check({"card": 36}, card)["ok"] is True
    assert _check({"card": 37}, card)["ok"] is False
    assert _check({"card": "36"}, card)["ok"] is False
    assert _check({"card": "36"}, {"data": "36"})["ok"] is True
    table = {"chartType": "table", "data": [{"a": "x", "b": 1.0}, {"a": "y", "b": 2.0}]}
    assert _check({"rows": [{"a": "x", "b": 1}, {"a": "y", "b": 2}]}, table)["ok"] is True
    assert _check({"rows": [{"a": "y", "b": 2}, {"a": "x", "b": 1}]}, table)["ok"] is False
    assert _check({"rows": [{"a": "x"}, {"a": "y"}]}, table)["ok"] is False


def test_valueCheckPivotUsesRowKeysAsLabels():
    pivot = {"chartType": "pivot", "data": {"c1": {"r1": 1, "r2": None}, "c2": {"r1": 2, "r2": 3}}}
    assert _check({"labels": ["r1", "r2"], "values": {"c1": [1, None], "c2": [2, 3]}}, pivot)["ok"] is True
    assert _check({"labels": ["r2", "r1"]}, pivot)["ok"] is False
