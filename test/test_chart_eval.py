"""The chart evaluation harness, run on its synthetic tables with the model replaced by recorded replies.

Everything after the model is real: the table reads through the local storage server, the check, the sandbox child,
the builder and the contract check. These tests are slow (a sandbox child per chart), so only the end-to-end one runs
every case twice; the others run one case. The value check, validation and the exit codes are in
`test_chart_eval_checks.py`.
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
FIXTURE_KEY = "chart-eval-fixture-key"


@pytest.fixture(autouse=True)
def fixtureKey(monkeypatch):
    """The synthetic tables carry markers made from this made-up key; the harness takes it from the environment."""
    monkeypatch.setenv("CHART_EVAL_STORAGE_KEY", FIXTURE_KEY)


def _args(tmp_path, *extra, cases=CASES, stub=STUB, runs=2):
    return [
        "--cases", str(cases), "--data", str(DATA), "--project", PROJECT, "--engine", "sql",
        "--runs", str(runs), "--out", str(tmp_path / "report.json"), "--model-stub", str(stub),
        *extra,
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


def test_harnessPassesOnSyntheticSetWithStubbedModel(tmp_path, capsys, monkeypatch):
    import redis

    from utils import initMethods
    import importlib

    creditModule = importlib.import_module("api.services.credits.creditService")
    used = []

    class PoolFromEarlier(redis.ConnectionPool):
        """A pool a test or the app made earlier in this process: a run must not touch it."""

        def get_connection(self, *args, **kwargs):
            used.append(1)
            raise AssertionError("a run used a Redis pool that existed before it")

    earlier = PoolFromEarlier()
    monkeypatch.setattr(initMethods, "_REDIS_POOL", earlier)
    monkeypatch.setattr(creditModule, "_redis_pool", earlier)
    env = dict(os.environ)

    assert chartEval.main(_args(tmp_path)) == 0
    assert capsys.readouterr().out.splitlines() == ["PASS 7/7", "PASS 7/7", "ALL RUNS PASS"]
    # No Redis was reached, and the pools and the environment are as they were.
    assert used == []
    assert initMethods._REDIS_POOL is earlier and creditModule._redis_pool is earlier
    assert dict(os.environ) == env

    report = _report(tmp_path)
    assert [run["passed"] for run in report["runs"]] == [7, 7] and report["engine"] == "sql" and report["allRunsPass"] is True
    assert all(run["date"] and run["stale"] is False for run in report["runs"])
    stages = {
        "bar-region": "ok", "line-last-12-months": "ok", "card-order-count": "ok", "table-top-products": "ok",
        "pivot-region-category": "ok", "geo-store-locations": "ok", "doubt-unclear": "doubt",
    }
    for run in report["runs"]:
        assert {case["id"]: case["stage"] for case in run["cases"]} == stages
        for case in run["cases"]:
            assert case["ok"] is True and case["contractErrors"] == [] and case["durationSeconds"] >= 0
            if case["id"] != "doubt-unclear":
                assert case["generatedCode"].startswith('{"nubrixChart":1') and case["repairs"] == 0 and case["error"] is None
                assert case["expected"] and case["got"]
            else:
                assert case["doubt"] == "Which numbers would you like to see, and for what period?" and case["generatedCode"] is None
    first = {case["id"]: case for case in report["runs"][0]["cases"]}
    assert {case["chartType"] for case in first.values()} >= {"bar", "line", "card", "table", "pivot", "geoMap"}
    assert first["geo-store-locations"]["got"]["points"][0]["label"] == "Harbor Store"
    assert first["card-order-count"]["got"] == {"card": 36}
    # The relative period was computed by expectSql from today's date, and the chart agrees.
    assert len(first["line-last-12-months"]["expected"]["labels"]) == 12
    # A DATE label: the expected text is the builder's ISO date, not a strftime month.
    assert len(first["line-last-12-months"]["expected"]["labels"][0]) == 10
    assert first["line-last-12-months"]["got"]["labels"] == first["line-last-12-months"]["expected"]["labels"]
    # The marked tables' footers were read by HEAD and Range requests, not by downloads for the schema read.
    storage = report["storage"]
    assert storage["tableHead"] > 0 and storage["tableRangeReads"] > 0


def test_theModelIsShownTheMetadataAServedRequestGives(tmp_path):
    seen = []

    class Spy(chartEval._Replay):
        def __init__(self, stub):
            super().__init__(stub)
            seen.append(self)

    original = chartEval._Replay
    chartEval._Replay = Spy
    try:
        assert chartEval.main(_args(tmp_path, "--only", "doubt-unclear", runs=1)) == 0
    finally:
        chartEval._Replay = original
    [spy] = seen
    [(key, inputs)] = spy.inputs
    assert key == "rephrased" and inputs["query"] == "Show me the numbers"
    metadata = inputs["metadata"]
    assert set(metadata) == {"orders", "products", "stores", "visits"}
    assert "archived_orders" not in metadata and "_meta" not in metadata
    # The hints are those of a warm served request (the row count comes from the metadata read), not the cold "unknown".
    assert metadata["orders"]["size_class"] == "small" and metadata["orders"]["_size_check"] is True
    assert all(entry["size_class"] == "small" and "unknown" not in entry["size_hint"] for entry in metadata.values())


def test_wrongValueFailsTheCase(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://example.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "kept")

    def edit(expect):
        expect["values"]["Total Revenue"][2] += 5

    cases = _editedCases(tmp_path, "bar-region", edit)
    assert chartEval.main(_args(tmp_path, "--only", "bar-region", cases=cases, runs=1)) == 1
    assert capsys.readouterr().out.splitlines() == ["PASS 0/1", "FAILED bar-region"]
    case = _report(tmp_path)["runs"][0]["cases"][0]
    assert case["ok"] is False and case["stage"] == "value"
    check = case["valueCheck"]
    assert (check["field"], check["dataset"], check["index"]) == ("values", "Total Revenue", 2)
    assert check["expectedType"] == "list" and check["gotType"] == "list" and check["expectedLength"] == check["gotLength"] == 4
    # The report shows the edited value next to the one the chart had.
    expected, got = case["expected"]["values"]["Total Revenue"], list(case["got"]["values"].values())[0]
    assert expected[2] == got[2] + 5 and expected[:2] == got[:2] and case["generatedCode"]
    # The run's environment is gone with it.
    assert os.environ["SUPABASE_URL"] == "http://example.invalid" and os.environ["SUPABASE_KEY"] == "kept"


def test_contractErrorFailsTheCase(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(chartEval, "contractErrors", lambda chart: ["data: broken"])
    assert chartEval.main(_args(tmp_path, "--only", "card-order-count", runs=1)) == 1
    case = _report(tmp_path)["runs"][0]["cases"][0]
    assert case["ok"] is False and case["stage"] == "contract" and case["contractErrors"] == ["data: broken"]
    assert "ALL RUNS PASS" not in capsys.readouterr().out


def test_aStubThatRepliesWithBadSqlFailsAtItsStageAndTheReportSaysWhy(tmp_path):
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
    assert "no_such_table" in case["generatedCode"] and case["error"] and case["failureDetail"]
    assert case["repairs"] == 2 and case["rephrasedOutput"]
