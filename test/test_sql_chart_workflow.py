"""The SQL chart workflow: rephrase, ground, generate, evaluate, repair (at most twice), format."""

import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from langchain_core.callbacks import BaseCallbackHandler

from nubrix.components import chartFailureLog
from nubrix.components.chartBuilder import ChartBuildError
from nubrix.components.chartEngine import ChartFailure
from nubrix.components.chartRequest import decodeGeneratedCode
from nubrix.workflows import sqlChartWorkflow as module
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError

REQUEST = {
    "chartType": "bar",
    "title": "Totals",
    "sql": 'SELECT "a", SUM("b") AS total FROM "orders" GROUP BY ALL',
    "encoding": {"x": "a", "y": ["total"]},
}
MODEL_TEXT = json.dumps(REQUEST)
CHART = {"chartType": "bar", "data": {"labels": ["x"], "datasets": [{"label": "Total", "data": [1]}]}}
METADATA = {
    "orders": {"columns": [{"name": "a"}, {"name": "b"}]},
    "old_table": {"isActive": False, "columns": [{"name": "z"}]},
}
OUTPUT_KEYS = {"projectId", "inputQuery", "metadata", "rephrasedQuery", "generatedCode", "codeOutput", "finalOutput", "debugAttempts"}


class TraceHandler(BaseCallbackHandler):
    last_trace_id = "trace-1"


class Harness:
    def __init__(self, monkeypatch, parallel=False):
        self.generator = MagicMock()
        self.generator.invoke.return_value = MODEL_TEXT
        self.repair = MagicMock()
        self.repair.invoke.return_value = MODEL_TEXT
        self.rephraser = MagicMock()
        self.rephraser.invoke.return_value = {"rephrasedOutput": "plan", "doubt": None}
        chains = {"sqlChartGeneratorPrompt": self.generator, "sqlChartRepairPrompt": self.repair}
        monkeypatch.setattr(module, "buildLlmChain", lambda section, prompt, **kw: chains[prompt])
        rephraserClass = MagicMock()
        rephraserClass.return_value.getQueryRephraserChain.return_value = self.rephraser
        monkeypatch.setattr(module, "QueryRephaser", rephraserClass)
        monkeypatch.setattr(module, "ParallelQueryRephaser", rephraserClass)
        self.schemas = MagicMock(return_value={})
        monkeypatch.setattr(module, "tableSchemas", self.schemas)
        self.render = MagicMock(return_value=CHART)
        monkeypatch.setattr(module, "renderChartRequest", self.render)
        self.manager = MagicMock()
        self.managerClass = MagicMock(return_value=self.manager)
        monkeypatch.setattr(module, "REPLManager", self.managerClass)
        self.logs = []
        monkeypatch.setattr(chartFailureLog.logger, "warning", lambda message, *a, **k: self.logs.append(message))
        self.graph = module.buildSqlChartWorkflow(parallel=parallel)

    def run(self, config=None, metadata=METADATA):
        return self.graph.invoke({"metadata": metadata, "inputQuery": "Totals by a", "projectId": "p1"}, config=config)

    def failureRecords(self):
        return [json.loads(line[len("chart_failure "):]) for line in self.logs if line.startswith("chart_failure ")]


@pytest.fixture
def harness(monkeypatch):
    return Harness(monkeypatch)


def test_successResponseKeysAndShapes(harness):
    state = harness.run()
    assert set(state) <= OUTPUT_KEYS
    assert {"projectId", "inputQuery", "generatedCode", "codeOutput", "finalOutput"} <= set(state)
    assert state["finalOutput"] == CHART
    assert state["codeOutput"] == json.dumps(state["finalOutput"])
    assert decodeGeneratedCode(state["generatedCode"]).sql == REQUEST["sql"]
    assert state["generatedCode"].startswith('{"nubrixChart":1,')
    assert state["debugAttempts"] == 0
    assert harness.failureRecords() == []
    harness.managerClass.assert_called_once_with(timeoutSeconds=35)


def test_evaluateGetsActiveTablesAndTheManager(harness):
    harness.run()
    args = harness.render.call_args
    assert args.args[0] is harness.manager
    assert args.args[1] == "p1"
    assert args.args[2].chartType == "bar"
    assert list(args.args[3]) == ["orders"]
    harness.schemas.assert_called_once_with("p1", ["orders"], skipUnreadable=True)
    assert "orders" in harness.generator.invoke.call_args.args[0]["grounding"]
    assert harness.generator.invoke.call_args.args[0]["plan"] == "plan"
    assert harness.generator.invoke.call_args.args[0]["question"] == "Totals by a"


def test_doubtGivesResponse(monkeypatch):
    harness = Harness(monkeypatch)
    harness.rephraser.invoke.return_value = {"rephrasedOutput": None, "doubt": "Which year?"}
    state = harness.run()
    assert state["finalOutput"] == {"response": "Which year?"}
    assert "generatedCode" not in state and "codeOutput" not in state
    harness.generator.invoke.assert_not_called()
    harness.render.assert_not_called()
    assert harness.failureRecords() == []


@pytest.mark.parametrize("stage", ["model", "check", "bind", "run", "build"])
def test_eachFailureStageRepairsThenSucceeds(harness, stage):
    if stage == "model":
        harness.generator.invoke.return_value = "this is not json"
        expectedError = None
    else:
        expectedError = f"exact {stage} error text"
        harness.render.side_effect = [ChartFailure(stage, expectedError), CHART]
    state = harness.run()
    assert state["finalOutput"] == CHART
    assert state["debugAttempts"] == 1
    harness.repair.invoke.assert_called_once()
    given = harness.repair.invoke.call_args.args[0]
    assert given["question"] == "Totals by a"
    assert given["plan"] == "plan"
    assert "orders" in given["grounding"]
    if expectedError is None:
        assert given["error"].startswith("The reply is not a valid chart request")
        assert given["previous_request"] == "this is not json"
    else:
        assert given["error"] == expectedError
        assert json.loads(given["previous_request"])["sql"] == REQUEST["sql"]
    assert harness.failureRecords() == []


def test_repairStopsAfterTwo(harness):
    harness.render.side_effect = ChartFailure("run", "Binder Error: nope")
    state = harness.run()
    assert harness.render.call_count == 3
    assert harness.repair.invoke.call_count == 2
    assert state["finalOutput"] == {"error": "Binder Error: nope"}
    assert state["codeOutput"] == "Binder Error: nope"
    assert state["debugAttempts"] == 2
    assert decodeGeneratedCode(state["generatedCode"]).title == "Totals"
    records = harness.failureRecords()
    assert len(records) == 1
    assert records[0]["stage"] == "run" and records[0]["repairs"] == 2 and records[0]["chartType"] == "bar"
    assert records[0]["shadow"] is False and records[0]["projectId"] == "p1"


def test_noValidRequestKeepsTheRawModelText(harness):
    harness.generator.invoke.return_value = "first answer"
    harness.repair.invoke.side_effect = ["second answer", "```json\nthird answer\n```"]
    state = harness.run()
    harness.render.assert_not_called()
    assert state["debugAttempts"] == 2
    assert state["generatedCode"] == "```json\nthird answer\n```"
    assert state["finalOutput"]["error"].startswith("The reply is not a valid chart request")
    records = harness.failureRecords()
    assert len(records) == 1 and records[0]["stage"] == "model" and records[0]["chartType"] is None
    assert records[0]["errorClass"] == "ValidationError"
    assert "third answer" not in harness.logs[0]


@pytest.mark.parametrize(
    "reply",
    [
        f"<think>I could group by {{a}} and use {{}} here.</think>\n```json\n{MODEL_TEXT}\n```",
        f"Reasoning with a brace {{ and a note.\n{MODEL_TEXT}\nDone.",
        f"```json\n{MODEL_TEXT}\n```",
        MODEL_TEXT,
    ],
)
def test_requestIsReadOutOfTheReply(harness, reply):
    from langchain_core.messages import AIMessage

    from utils.llm import cleanThinkTokens

    # The chain hands the workflow text after cleanThinkTokens, which removes the tags and keeps the reasoning.
    harness.generator.invoke.return_value = cleanThinkTokens(AIMessage(content=reply)).content
    state = harness.run()
    assert state["finalOutput"] == CHART
    assert state["debugAttempts"] == 0
    harness.repair.invoke.assert_not_called()


def test_modelFailureLogNamesTheErrorTypes(harness):
    harness.generator.invoke.return_value = "no json here"
    harness.repair.invoke.side_effect = ['{"chartType": "nope", "title": "Secret Corp"}', '{"extraKey": 1}']
    harness.run()
    assert len(harness.logs) == 1
    error = harness.failureRecords()[0]["error"]
    assert error.startswith("The model reply is not a valid chart request.")
    assert "missing" in error and "extra_forbidden" in error
    assert "extraKey" not in error and "Secret Corp" not in error and "nope" not in error


def test_timeoutSkipsRepair(harness):
    harness.render.side_effect = ChartFailure("timeout", "Execution timed out after 35 seconds")
    state = harness.run()
    assert harness.render.call_count == 1
    harness.repair.invoke.assert_not_called()
    assert state["finalOutput"] == {"error": "Execution timed out after 35 seconds"}
    assert state["debugAttempts"] == 0
    assert [record["stage"] for record in harness.failureRecords()] == ["timeout"]


def test_busySkipsRepairAndShowsBusyMessage(harness):
    harness.render.side_effect = SandboxBusyError()
    state = harness.run()
    assert harness.render.call_count == 1
    harness.repair.invoke.assert_not_called()
    assert state["finalOutput"] == {"error": BUSY_MESSAGE}
    assert state["codeOutput"] == json.dumps({"error": BUSY_MESSAGE})
    records = harness.failureRecords()
    assert [record["stage"] for record in records] == ["busy"]
    assert records[0]["errorClass"] == "SandboxBusyError"


def test_shadowDoesNotWaitForASlot(harness):
    harness.run(config={"configurable": {"shadow": True}})
    assert harness.render.call_args.kwargs["waitForSlot"] is False


def test_shadowGroundingLeavesHeadroomAndServedGroundingIsUnchanged(harness):
    harness.run(config={"configurable": {"shadow": True}})
    harness.schemas.assert_called_once_with("p1", ["orders"], skipUnreadable=True, leaveHeadroom=True)
    harness.schemas.reset_mock()
    harness.run()
    harness.schemas.assert_called_once_with("p1", ["orders"], skipUnreadable=True)


def test_servedRunWaitsForASlot(harness):
    harness.run()
    assert harness.render.call_args.kwargs["waitForSlot"] is True


def test_shadowFailureIsLoggedAsShadow(harness):
    harness.render.side_effect = ChartFailure("timeout", "Execution timed out")
    harness.run(config={"configurable": {"shadow": True}})
    assert harness.failureRecords()[0]["shadow"] is True


def test_shadowFailureFillsItsOutcomeWithTheStage(harness):
    outcome = {}
    harness.render.side_effect = ChartFailure("bind", 'Binder Error: column "secret_column" not found')
    harness.run(config={"configurable": {"shadow": True, "outcome": outcome}})
    assert outcome == {"stage": "bind"}


def test_servedFailureFillsNoOutcome(harness):
    outcome = {}
    harness.render.side_effect = ChartFailure("bind", "x")
    harness.run(config={"configurable": {"outcome": outcome}})
    assert outcome == {}


@pytest.mark.parametrize("where", ["grounding", "run", "wrappedRun"])
def test_aBusyShadowWritesNoFailureLine(harness, where):
    error = SandboxBusyError()
    if where == "wrappedRun":
        error = RuntimeError("503")
        error.errorCode = "SANDBOX_BUSY"
    if where == "grounding":
        harness.schemas.side_effect = error
    else:
        harness.render.side_effect = error
    state = harness.run(config={"configurable": {"shadow": True}})
    assert state["finalOutput"] == {"error": BUSY_MESSAGE}
    assert harness.failureRecords() == []
    harness.repair.invoke.assert_not_called()


def test_aServedRunWithAWrappedBusyErrorIsUnchanged(harness):
    error = RuntimeError("503")
    error.errorCode = "SANDBOX_BUSY"
    harness.render.side_effect = error
    state = harness.run()
    assert state["finalOutput"] == {"error": module._UNEXPECTED_MESSAGE}
    assert [record["stage"] for record in harness.failureRecords()] == ["run"]


def test_internalKeysNeverLeak(harness):
    harness.render.side_effect = [ChartFailure("run", "boom"), CHART]
    state = harness.run()
    assert set(state) <= OUTPUT_KEYS
    for key in ("chartRequest", "failure", "grounding", "tables", "rawText", "chart"):
        assert key not in state
    harness.render.side_effect = ChartFailure("check", "bad")
    failed = harness.run()
    assert set(failed) <= OUTPUT_KEYS
    assert "failure" not in failed


def test_configReachesEveryChainCall(harness):
    handler = TraceHandler()
    harness.render.side_effect = [ChartFailure("run", "boom"), CHART]
    harness.run(config={"callbacks": [handler]})
    for chain in (harness.rephraser, harness.generator, harness.repair):
        config = chain.invoke.call_args.args[1] if len(chain.invoke.call_args.args) > 1 else chain.invoke.call_args.kwargs["config"]
        callbacks = config["callbacks"]
        handlers = getattr(callbacks, "handlers", callbacks)
        assert handler in handlers


def test_failureLogCarriesTraceIdAndTableNames(harness):
    harness.render.side_effect = ChartFailure("run", 'Binder Error: Table "orders" has no column "Secret" near 42')
    harness.run(config={"callbacks": [TraceHandler()]})
    record = harness.failureRecords()[0]
    assert record["traceId"] == "trace-1"
    assert record["error"] == 'Binder Error: Table "orders" has no column "?" near ?'


def test_failureLogOfABuildFailureHasNoLabel(harness):
    cause = ChartBuildError(
        "The query has more than one row for the x 'Secret Corp' and series 'North'; aggregate.",
        logMessage="The query has more than one row for an x and series pair.",
    )

    def fail(*args, **kwargs):
        try:
            raise cause
        except ChartBuildError as error:
            raise ChartFailure("build", str(error), logMessage=error.logMessage) from error

    harness.render.side_effect = fail
    state = harness.run()
    assert "Secret Corp" in state["finalOutput"]["error"]
    assert len(harness.logs) == 1
    assert "Secret Corp" not in harness.logs[0] and "North" not in harness.logs[0]
    record = harness.failureRecords()[0]
    assert record["errorClass"] == "ChartBuildError"
    assert record["error"] == "The query has more than one row for an x and series pair."
    assert record["stage"] == "build"
    # The repair prompt still gets the full text.
    assert "Secret Corp" in harness.repair.invoke.call_args.args[0]["error"]


def test_modelFailureLogHasNoModelText(harness):
    harness.generator.invoke.return_value = '{"chartType": "bar", "title": "Secret Corp"'
    harness.repair.invoke.return_value = '{"chartType": "nope"}'
    harness.run()
    assert len(harness.logs) == 1
    assert "Secret Corp" not in harness.logs[0] and "nope" not in harness.logs[0]


@pytest.mark.parametrize("error", [RuntimeError("No sandbox uid is free."), ValueError("unreadable result 'Secret Corp'")])
def test_unexpectedRunErrorEndsAsAnErrorWithoutRepair(harness, error):
    harness.render.side_effect = error
    state = harness.run()
    assert harness.render.call_count == 1
    harness.repair.invoke.assert_not_called()
    assert set(state["finalOutput"]) == {"error"}
    assert "Secret Corp" not in json.dumps(state["finalOutput"])
    assert state["debugAttempts"] == 0
    records = harness.failureRecords()
    assert len(records) == 1
    assert records[0]["stage"] == "run" and records[0]["errorClass"] == type(error).__name__
    assert records[0]["error"] == "The chart query could not be run."
    assert len(harness.logs) == 1
    assert not any("Secret Corp" in line or "uid" in line for line in harness.logs)


def test_lastValidRequestSurvivesInvalidRepairs(harness):
    harness.render.side_effect = ChartFailure("run", "Binder Error: nope")
    harness.repair.invoke.side_effect = ["not json 1", "not json 2"]
    state = harness.run()
    assert decodeGeneratedCode(state["generatedCode"]).sql == REQUEST["sql"]
    records = harness.failureRecords()
    assert len(records) == 1 and records[0]["stage"] == "model" and records[0]["chartType"] == "bar"
    second = harness.repair.invoke.call_args_list[1].args[0]
    assert json.loads(second["previous_request"])["sql"] == REQUEST["sql"]
    assert second["error"].startswith("The reply is not a valid chart request")


TWO_TABLES = {"orders": {"columns": [{"name": "a"}]}, "legacy": {"columns": [{"name": "z"}]}}


def test_groundingFallsBackPerTable(harness):
    def read(projectId, names, skipUnreadable=False):
        return {"orders": pa.schema([("a", pa.int64())])}

    harness.schemas.side_effect = read
    state = harness.run(metadata=TWO_TABLES)
    assert state["finalOutput"] == CHART
    harness.schemas.assert_called_once_with("p1", ["orders", "legacy"], skipUnreadable=True)
    grounding = harness.generator.invoke.call_args.args[0]["grounding"]
    assert '"a" BIGINT' in grounding
    assert '"z" UNKNOWN' in grounding


@pytest.mark.parametrize("kind", ["error", "code"])
def test_busyGroundingEndsAsBusyWithoutAModelCall(harness, kind):
    error = SandboxBusyError()
    if kind == "code":
        error = RuntimeError("busy")
        error.errorCode = "SANDBOX_BUSY"
    harness.schemas.side_effect = error
    state = harness.run(metadata=TWO_TABLES)
    harness.generator.invoke.assert_not_called()
    harness.render.assert_not_called()
    assert state["finalOutput"] == {"error": BUSY_MESSAGE}
    assert state["codeOutput"] == json.dumps({"error": BUSY_MESSAGE})
    assert harness.schemas.call_count == 1
    records = harness.failureRecords()
    assert len(records) == 1 and records[0]["stage"] == "busy" and records[0]["chartType"] is None
    assert records[0]["errorClass"] == type(error).__name__


def _errorsOf(reply):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        module._readRequest(reply)
    return [(item["type"], item["loc"]) for item in caught.value.errors()]


def test_invalidRequestReportsTheRequestsOwnError():
    reply = 'Plan {"step": 1} then {"chartType": "bar", "title": "T", "sql": "S"}'
    assert _errorsOf(reply) == [("missing", ("encoding",))]


INVALID_REQUEST = json.dumps({"chartType": "bar", "title": "T", "sql": "S"})
PROSE = " and that is my plan," * 20


def test_invalidRequestFollowedByProseWithABraceReportsItsOwnError():
    reply = INVALID_REQUEST + "\nNote:" + PROSE + " { see above" + PROSE
    assert _errorsOf(reply) == [("missing", ("encoding",))]


def test_braceInsideAStringOfTheRequestStartsNothing():
    reply = json.dumps({"chartType": "bar", "title": "T {x}", "sql": "S"}) + "\n" + PROSE * 2
    assert _errorsOf(reply) == [("missing", ("encoding",))]


def test_invalidRequestThenATruncatedRequestIsInvalidJson():
    assert _errorsOf(INVALID_REQUEST + "\n" + MODEL_TEXT[:-20]) == [("json_invalid", ())]


def test_truncatedRequestAfterEmptyBracesIsInvalidJson():
    assert _errorsOf("Use {} maybe.\n" + MODEL_TEXT[:-20]) == [("json_invalid", ())]


def test_truncatedRequestAloneIsInvalidJson():
    assert _errorsOf(MODEL_TEXT[:-20]) == [("json_invalid", ())]


@pytest.mark.parametrize(
    "reply",
    [
        'I could use {a} or {} here, or {"x": 1}.\n' + MODEL_TEXT,
        '{"note": "draft"}\n' + MODEL_TEXT,
        MODEL_TEXT + "\nHope this helps {smile}",
        "[" + MODEL_TEXT + "]",
    ],
)
def test_requestIsPickedOutOfSurroundingBraces(reply):
    assert module._readRequest(reply).title == "Totals"


def test_firstOfTwoValidRequestsIsTaken():
    other = json.dumps({**REQUEST, "title": "Other"})
    assert module._readRequest(MODEL_TEXT + "\n" + other).title == "Totals"


SLICE_REQUEST = {
    "chartType": "doughnut",
    "title": "Share",
    "sql": 'SELECT "a", SUM("b") AS total FROM "orders" GROUP BY ALL',
    "encoding": {"label": "a", "value": "total"},
}


@pytest.mark.parametrize(
    "plan, replied, served",
    [
        ("Pie chart of revenue share by category. Steps: ...", "doughnut", "pie"),
        ("pie chart. Steps: ...", "polarArea", "pie"),
        ("A doughnut chart of orders by status.", "pie", "doughnut"),
        ("Polar area chart of units by region.", "pie", "polarArea"),
        ("polarArea chart of units by region.", "doughnut", "polarArea"),
        ("Doughnut chart of orders by status.", "doughnut", "doughnut"),
        ("Share of revenue by category.", "doughnut", "doughnut"),
        ("Bar chart of revenue by category.", "doughnut", "doughnut"),
        ("Pies sold per bakery as a bar chart.", "doughnut", "doughnut"),
    ],
)
def test_sliceChartServesTheTypeThePlanNames(harness, plan, replied, served):
    harness.rephraser.invoke.return_value = {"rephrasedOutput": plan, "doubt": None}
    harness.generator.invoke.return_value = json.dumps({**SLICE_REQUEST, "chartType": replied})
    state = harness.run()
    assert harness.render.call_args.args[2].chartType == served
    assert decodeGeneratedCode(state["generatedCode"]).chartType == served


def test_sliceTypeIsAlignedAfterARepairToo(harness):
    harness.rephraser.invoke.return_value = {"rephrasedOutput": "Pie chart of revenue share.", "doubt": None}
    harness.generator.invoke.return_value = "not json"
    harness.repair.invoke.return_value = json.dumps(SLICE_REQUEST)
    state = harness.run()
    assert harness.render.call_args.args[2].chartType == "pie"
    assert decodeGeneratedCode(state["generatedCode"]).chartType == "pie"


def test_nonSliceRequestIsNeverRetyped(harness):
    harness.rephraser.invoke.return_value = {"rephrasedOutput": "Pie chart of revenue share.", "doubt": None}
    state = harness.run()
    assert harness.render.call_args.args[2].chartType == "bar"
    assert decodeGeneratedCode(state["generatedCode"]).chartType == "bar"


def test_generationStillRefusesAnInactiveTable(harness, monkeypatch):
    """Saved widgets re-run on inactive tables, but a new chart still sees and may name only the active ones."""
    from nubrix.components import chartEngine

    reply = json.dumps(dict(REQUEST, sql='SELECT "z" AS "a", COUNT(*) AS total FROM "old_table" GROUP BY ALL'))
    harness.generator.invoke.return_value = reply
    harness.repair.invoke.return_value = reply
    ran = []
    monkeypatch.setattr(chartEngine, "runChartQuery", lambda *args, **kwargs: ran.append(args))
    harness.render.side_effect = chartEngine.renderChartRequest

    state = harness.run()

    assert ran == []
    assert harness.render.call_count == 3
    assert all(list(call.args[3]) == ["orders"] for call in harness.render.call_args_list)
    assert "old_table" not in harness.generator.invoke.call_args.args[0]["grounding"]
    assert "Table 'old_table' is not available" in state["finalOutput"]["error"]
    assert harness.failureRecords()[0]["stage"] == "check"



# A period that counts back from today, over a table whose data may end before it.

PERIOD_REQUEST = {
    "chartType": "line",
    "title": "Monthly orders",
    "sql": (
        "SELECT strftime(date_trunc('month', o.\"b\"), '%Y-%m') AS month, count(*) AS orders FROM \"orders\" AS o "
        "WHERE date_trunc('month', o.\"b\") >= date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH GROUP BY 1 ORDER BY 1"
    ),
    "encoding": {"x": "month", "y": ["orders"]},
}
SIMPLE_PERIOD_REQUEST = {
    "chartType": "bar",
    "title": "Totals",
    "sql": 'SELECT "a", COUNT(*) AS total FROM "orders" WHERE "b" >= CURRENT_DATE - 29 GROUP BY ALL',
    "encoding": {"x": "a", "y": ["total"]},
}
PROBE_SQL = (
    "SELECT p1.last_value AS last_value_1, p1.ended_before AS ended_before_1 FROM ("
    'SELECT CAST(max("b") AS VARCHAR) AS last_value, '
    '(max("b") IS NULL OR max("b") < (CURRENT_DATE - 29)) AS ended_before FROM "orders") AS p1'
)
ZERO_CHART = {"chartType": "bar", "title": "Totals", "xLabels": "A", "yLabels": "Total", "data": {"labels": ["x", "y"], "datasets": [{"label": "Total", "data": [0, 0]}]}}
EMPTY_CHART = {"chartType": "bar", "title": "Totals", "xLabels": "A", "yLabels": "Total", "data": {"labels": [], "datasets": []}}
TWO_BOUND_REQUEST = dict(
    SIMPLE_PERIOD_REQUEST,
    sql=(
        'SELECT o."a", count(*) AS total FROM "orders" o JOIN "items" i ON i."a" = o."a" '
        'WHERE o."b" >= CURRENT_DATE - 29 AND i."c" >= CURRENT_DATE - 29 GROUP BY ALL'
    ),
)
TWO_BOUND_METADATA = {"orders": {"columns": [{"name": "a"}, {"name": "b"}]}, "items": {"columns": [{"name": "a"}, {"name": "c"}]}}


def _probeAnswer(*pairs):
    """The combined probe's table: one row with (last_value_n, ended_before_n) for each (last value, ended) pair."""
    row = {}
    for number, (lastValue, endedBefore) in enumerate(pairs, start=1):
        row[f"last_value_{number}"] = lastValue
        row[f"ended_before_{number}"] = endedBefore
    return {"chartType": "table", "title": "Period check", "columns": list(row), "data": [row]}


def _periodQuestion(clause):
    return f"There is no data for the period you asked about: {clause}. Which period should the chart cover?"


def _withProbe(harness, chart, *answers, request=SIMPLE_PERIOD_REQUEST):
    """The chart call returns `chart`; each probe call returns (or raises) the next answer."""
    harness.generator.invoke.return_value = json.dumps(request)
    pending = list(answers)

    def render(manager, projectId, chartRequest, tables, **kwargs):
        if "ended_before" in chartRequest.sql:
            answer = pending.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return chart

    harness.render.side_effect = render


def _probeCalls(harness):
    return [call for call in harness.render.call_args_list if "ended_before" in call.args[2].sql]


def _probeLines(harness):
    return [line for line in harness.logs if line.startswith("chart_period_probe ")]


def test_dataThatEndsBeforeThePeriodAsksWhichPeriod(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-06-30 13:45:00", True)))
    state = harness.run()
    question = _periodQuestion("the data ends on 2019-06-30")
    assert state["finalOutput"] == {"response": question}
    assert state["rephrasedQuery"] == {"rephrasedOutput": "plan", "doubt": question}
    assert "generatedCode" not in state and "codeOutput" not in state
    assert set(state) <= OUTPUT_KEYS
    calls = _probeCalls(harness)
    assert len(calls) == 1
    probe = calls[0]
    assert probe.args[0] is harness.manager and probe.args[1] == "p1"
    assert probe.args[2].chartType == "table"
    assert probe.args[2].sql == PROBE_SQL
    assert list(probe.args[3]) == ["orders"]
    assert probe.kwargs["waitForSlot"] is True
    assert harness.logs == []
    harness.repair.invoke.assert_not_called()


def test_anEmptyChartWithDataThatEndsBeforeThePeriodAsksToo(harness):
    _withProbe(harness, EMPTY_CHART, _probeAnswer(("2021-01-31", True)))
    state = harness.run()
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2021-01-31")}


def test_everyBoundRunsInOneProbeAndOneEndedBoundThatEmptiesTheRowsAsks(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-01-01", True), ("2020-02-02 00:00:00", True)), request=TWO_BOUND_REQUEST)
    state = harness.run(metadata=TWO_BOUND_METADATA)
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2020-02-02")}
    calls = _probeCalls(harness)
    assert len(calls) == 1
    assert "AS p1 CROSS JOIN (" in calls[0].args[2].sql and calls[0].args[2].sql.endswith(") AS p2")

    # Either WHERE conjunct empties the joined rows by itself; the question quotes only the bounds that ended.
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-01-01", True), ("2030-01-01", False)), request=TWO_BOUND_REQUEST)
    state = harness.run(metadata=TWO_BOUND_METADATA)
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-01-01")}

    _withProbe(harness, ZERO_CHART, _probeAnswer(("2030-01-01", False), ("2030-01-01", False)), request=TWO_BOUND_REQUEST)
    state = harness.run(metadata=TWO_BOUND_METADATA)
    assert state["finalOutput"] == ZERO_CHART
    assert harness.logs == []


def test_anEndedBoundThatLeavesOtherDataKeepsTheChart(harness):
    request = dict(
        SIMPLE_PERIOD_REQUEST,
        sql=(
            'SELECT "a", count(*) AS total FROM (SELECT "a" FROM "orders" WHERE "b" >= CURRENT_DATE - 29 '
            'UNION ALL SELECT "a" FROM "items" WHERE "c" >= CURRENT_DATE - 29) AS u GROUP BY ALL'
        ),
    )
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-01-01", True), ("2030-01-01", False)), request=request)
    state = harness.run(metadata=TWO_BOUND_METADATA)
    assert state["finalOutput"] == ZERO_CHART
    assert len(_probeCalls(harness)) == 1
    assert harness.logs == []


def test_aLegitimateZeroKeepsTheChart(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2030-01-01", False)))
    state = harness.run()
    assert state["finalOutput"] == ZERO_CHART
    assert state["codeOutput"] == json.dumps(ZERO_CHART)
    assert decodeGeneratedCode(state["generatedCode"]).sql == SIMPLE_PERIOD_REQUEST["sql"]
    assert state["rephrasedQuery"] == {"rephrasedOutput": "plan", "doubt": None}
    assert len(_probeCalls(harness)) == 1
    assert harness.logs == []


def test_everyTableEmptySaysThereAreNoDatedRows(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer((None, True)))
    state = harness.run()
    assert state["finalOutput"] == {"response": _periodQuestion("the table has no dated rows")}


@pytest.mark.parametrize(
    "failure, stage",
    [
        (ChartFailure("check", "Table 'orders' is not available."), "check"),
        (ChartFailure("bind", 'Binder Error: column "b" not found'), "bind"),
        (ChartFailure("run", "Conversion Error: '2019-06-30'"), "run"),
        (ChartFailure("timeout", "Execution timed out after 35 seconds"), "timeout"),
        (ChartFailure("build", "bad"), "build"),
        (SandboxBusyError(), "busy"),
        (RuntimeError("No sandbox uid is free."), "exception"),
        ({"chartType": "table", "title": "Period check", "columns": [], "data": []}, "result"),
        (_probeAnswer(("2019-06-30", "yes")), "result"),
        ({"chartType": "table", "title": "Period check", "columns": ["x"], "data": [{"x": 1}]}, "result"),
    ],
)
def test_aFailedProbeKeepsTheChartAndLogsOneWarning(harness, failure, stage):
    _withProbe(harness, ZERO_CHART, failure)
    state = harness.run()
    assert state["finalOutput"] == ZERO_CHART
    assert decodeGeneratedCode(state["generatedCode"]).sql == SIMPLE_PERIOD_REQUEST["sql"]
    assert harness.failureRecords() == []
    lines = _probeLines(harness)
    assert len(harness.logs) == 1 and len(lines) == 1
    record = json.loads(lines[0][len("chart_period_probe "):])
    assert record["stage"] == stage
    assert record["projectId"] == "p1" and record["shadow"] is False
    assert "2019" not in lines[0] and "orders" not in lines[0] and '"b"' not in lines[0]
    harness.repair.invoke.assert_not_called()


def test_aMissingBoundInTheAnswerIsAFailedProbe(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-01-01", True)), request=TWO_BOUND_REQUEST)
    state = harness.run(metadata=TWO_BOUND_METADATA)
    assert state["finalOutput"] == ZERO_CHART
    assert [json.loads(line[len("chart_period_probe "):])["stage"] for line in _probeLines(harness)] == ["result"]


def test_anUndatedLastValueKeepsTheChartWithAnInfoLine(harness, monkeypatch):
    infos = []
    monkeypatch.setattr(module.logger, "info", lambda message, *a, **k: infos.append(message))
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019", True)))
    state = harness.run()
    assert state["finalOutput"] == ZERO_CHART
    assert harness.logs == []
    assert harness.failureRecords() == []
    lines = [line for line in infos if line.startswith("chart_period_probe ")]
    assert len(lines) == 1
    record = json.loads(lines[0][len("chart_period_probe "):])
    assert record["stage"] == "undated" and record["errorClass"] is None
    assert "2019" not in lines[0]


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "a", COUNT(*) AS total FROM "orders" GROUP BY ALL',
        'SELECT "a", COUNT(*) AS total FROM "orders" WHERE "b" <= CURRENT_DATE GROUP BY ALL',
        """SELECT "a", COUNT(*) AS total FROM "orders" WHERE "b" >= DATE '2024-01-01' GROUP BY ALL""",
        'SELECT "a", COUNT(*) AS total FROM "orders" WHERE "b" >= CURRENT_DATE - 29 OR "a" = 1 GROUP BY ALL',
    ],
)
def test_noBoundThatFiltersTheRowsRunsNoProbe(harness, sql):
    _withProbe(harness, ZERO_CHART, request=dict(SIMPLE_PERIOD_REQUEST, sql=sql))
    state = harness.run()
    assert state["finalOutput"] == ZERO_CHART
    assert harness.render.call_count == 1
    assert harness.logs == []


def test_aChartWithDataRunsNoProbe(harness):
    _withProbe(harness, CHART)
    state = harness.run()
    assert state["finalOutput"] == CHART
    assert harness.render.call_count == 1


def test_theParallelWorkflowNeverProbes(monkeypatch):
    harness = Harness(monkeypatch, parallel=True)
    harness.rephraser.invoke.return_value = {"rephrasedOutput": "plan"}
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-06-30", True)))
    state = harness.run()
    assert state["finalOutput"] == ZERO_CHART
    assert harness.render.call_count == 1


def test_aShadowRunProbesWithoutWaitingAndCountsNoOutcome(harness):
    outcome = {}
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-06-30", True)))
    state = harness.run(config={"configurable": {"shadow": True, "outcome": outcome}})
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-06-30")}
    assert _probeCalls(harness)[0].kwargs["waitForSlot"] is False
    assert outcome == {}
    assert harness.failureRecords() == []


def test_aFailedShadowProbeIsLoggedAsShadow(harness):
    _withProbe(harness, ZERO_CHART, SandboxBusyError())
    state = harness.run(config={"configurable": {"shadow": True}})
    assert state["finalOutput"] == ZERO_CHART
    assert json.loads(_probeLines(harness)[0][len("chart_period_probe "):])["shadow"] is True


def test_aProbeAfterARepairUsesTheRepairedQuery(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-06-30", True)))
    chartCalls = []
    probeOrChart = harness.render.side_effect

    def render(manager, projectId, chartRequest, tables, **kwargs):
        if "ended_before" not in chartRequest.sql:
            chartCalls.append(chartRequest.sql)
            if len(chartCalls) == 1:
                raise ChartFailure("bind", "Binder Error: nope")
        return probeOrChart(manager, projectId, chartRequest, tables, **kwargs)

    harness.render.side_effect = render
    harness.generator.invoke.return_value = MODEL_TEXT
    harness.repair.invoke.return_value = json.dumps(SIMPLE_PERIOD_REQUEST)
    state = harness.run()
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-06-30")}
    assert state["debugAttempts"] == 1
    assert _probeCalls(harness)[0].args[2].sql == PROBE_SQL


def test_aWrappedColumnIsProbedThroughItsExpressionAndQuotesTheBareDate(harness):
    _withProbe(harness, ZERO_CHART, _probeAnswer(("2019-06-30", True)), request=PERIOD_REQUEST)
    state = harness.run()
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-06-30")}
    side = "DATE_TRUNC('MONTH', \"b\")"
    assert _probeCalls(harness)[0].args[2].sql == (
        "SELECT p1.last_value AS last_value_1, p1.ended_before AS ended_before_1 FROM ("
        f'SELECT CAST(max("b") AS VARCHAR) AS last_value, (max({side}) IS NULL OR max({side}) < '
        "(DATE_TRUNC('MONTH', CURRENT_DATE) - INTERVAL '11' MONTH)) AS ended_before FROM \"orders\") AS p1"
    )


# The review's repro on a real DuckDB: open orders from long ago with no refunds.

OLD_ORDERS = {
    "orders": pa.table(
        {"d": pa.array([date(2019, 1, 1), date(2019, 2, 1)], pa.date32()), "status": ["open", "open"], "refund": pa.array([0, 0], pa.int64())}
    )
}


def _duckdbRender(tables):
    """Check, run on an in-process DuckDB and build, as renderChartRequest does with the sandbox."""
    import duckdb

    from nubrix.components.chartBuilder import buildChart
    from nubrix.components.sqlCheck import checkSql

    def render(manager, projectId, chartRequest, activeTables, **kwargs):
        checked = checkSql(chartRequest.sql, activeTables)
        connection = duckdb.connect(":memory:")
        try:
            for name, table in tables.items():
                connection.register(name, table)
            result = connection.execute(checked.sql).arrow()
        finally:
            connection.close()
        return buildChart(chartRequest, result)

    return render


@pytest.mark.parametrize(
    "sql, x",
    [
        ("SELECT status, sum(refund) AS total FROM orders WHERE d >= CURRENT_DATE - 30 OR status = 'open' GROUP BY 1", "status"),
        ("SELECT CASE WHEN d >= CURRENT_DATE - 30 THEN 'recent' ELSE 'older' END AS bucket, sum(refund) AS total FROM orders GROUP BY 1", "bucket"),
    ],
)
def test_aLegitimateZeroBehindAnOrOrACaseKeepsTheChartOnDuckDb(harness, sql, x):
    harness.generator.invoke.return_value = json.dumps({"chartType": "bar", "title": "Refunds", "sql": sql, "encoding": {"x": x, "y": ["total"]}})
    harness.render.side_effect = _duckdbRender(OLD_ORDERS)
    state = harness.run()
    assert state["finalOutput"]["data"]["datasets"][0]["data"] == [0]
    assert "generatedCode" in state
    assert harness.render.call_count == 1
    assert harness.logs == []


def test_dataThatEndsBeforeAConjunctiveBoundAsksOnDuckDb(harness):
    sql = "SELECT status, sum(refund) AS total FROM orders WHERE d >= CURRENT_DATE - 30 GROUP BY 1"
    harness.generator.invoke.return_value = json.dumps({"chartType": "bar", "title": "Refunds", "sql": sql, "encoding": {"x": "status", "y": ["total"]}})
    harness.render.side_effect = _duckdbRender(OLD_ORDERS)
    state = harness.run()
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-02-01")}
    assert harness.render.call_count == 2
    assert harness.logs == []


# The prompt's pattern for a relative period: list every period, LEFT JOIN the filtered data to it, COALESCE to 0.

FILLED_IN_SQL = (
    "WITH months AS (SELECT unnest(generate_series(date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH, "
    "date_trunc('month', CURRENT_DATE), INTERVAL 1 MONTH)) AS month_start), "
    "per_month AS (SELECT date_trunc('month', \"ts\") AS month_start, COUNT(\"id\") AS n FROM \"events\" "
    "WHERE \"ts\" >= date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH AND \"ts\" < CURRENT_DATE + INTERVAL 1 DAY GROUP BY ALL) "
    "SELECT strftime(m.month_start, '%Y-%m') AS month, COALESCE(p.n, 0) AS n FROM months AS m "
    "LEFT JOIN per_month AS p ON p.month_start = m.month_start ORDER BY m.month_start"
)
EVENTS_METADATA = {"events": {"columns": [{"name": "id"}, {"name": "ts"}]}}


def _events(*moments):
    return {"events": pa.table({"id": list(range(1, len(moments) + 1)), "ts": pa.array(list(moments), pa.timestamp("us"))})}


def _askForMonthlyEvents(harness, tables):
    request = {"chartType": "line", "title": "Monthly events", "sql": FILLED_IN_SQL, "encoding": {"x": "month", "y": ["n"]}}
    harness.generator.invoke.return_value = json.dumps(request)
    harness.render.side_effect = _duckdbRender(tables)
    return harness.run(metadata=EVENTS_METADATA)


def test_aFilledInPeriodWithDataThatEndedAsksOnDuckDb(harness):
    from datetime import datetime

    state = _askForMonthlyEvents(harness, _events(datetime(2019, 3, 1, 8, 0), datetime(2019, 3, 15, 10, 30)))
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-03-15")}
    assert "generatedCode" not in state and "codeOutput" not in state
    assert harness.render.call_count == 2
    assert harness.logs == []


def test_aFilledInPeriodWithDataThisMonthKeepsTheChartOnDuckDb(harness):
    from datetime import datetime

    now = datetime.now()
    state = _askForMonthlyEvents(harness, _events(datetime(2019, 3, 1, 8, 0), datetime(now.year, now.month, 1, 0, 0, 1)))
    assert state["finalOutput"]["chartType"] == "line"
    values = state["finalOutput"]["data"]["datasets"][0]["data"]
    assert len(values) == 12 and values[-1] == 1 and sum(values) == 1
    assert harness.render.call_count == 1


# Round 3 on a real DuckDB: events that ended long ago, refunds inside the period whose every amount is 0 (a legitimate
# zero), and customers.

MONTHS_SQL = (
    "months AS (SELECT unnest(generate_series(date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH, "
    "date_trunc('month', CURRENT_DATE), INTERVAL 1 MONTH)) AS month_start)"
)
EVENTS_PER_MONTH_SQL = (
    "per_month AS (SELECT date_trunc('month', ts) AS month_start, count(id) AS n FROM events "
    "WHERE ts >= date_trunc('month', CURRENT_DATE) - INTERVAL 11 MONTH GROUP BY ALL)"
)
MIXED_METADATA = {
    "events": {"columns": [{"name": "id"}, {"name": "ts"}, {"name": "cid"}, {"name": "amount"}]},
    "refunds": {"columns": [{"name": "r_at"}, {"name": "amount"}, {"name": "status"}, {"name": "cid"}]},
    "customers": {"columns": [{"name": "id"}, {"name": "region"}]},
}


def _mixedTables():
    today = date.today()
    recent = [datetime.combine(today - timedelta(days=days), datetime.min.time()) for days in (2, 20, 40)]
    return {
        "events": pa.table(
            {
                "id": [1, 2],
                "ts": pa.array([datetime(2019, 3, 1, 8, 0), datetime(2019, 3, 15, 10, 30)], pa.timestamp("us")),
                "cid": [1, 2],
                "amount": pa.array([5, 7], pa.int64()),
            }
        ),
        "refunds": pa.table(
            {"r_at": pa.array(recent, pa.timestamp("us")), "amount": pa.array([0, 0, 0], pa.int64()), "status": ["done"] * 3, "cid": [1, 2, 1]}
        ),
        "customers": pa.table({"id": [1, 2], "region": ["north", "south"]}),
    }


def _chartOnDuckDb(harness, sql, x, y):
    request = {"chartType": "bar", "title": "Chart", "sql": sql, "encoding": {"x": x, "y": y}}
    harness.generator.invoke.return_value = json.dumps(request)
    harness.render.side_effect = _duckdbRender(_mixedTables())
    return harness.run(metadata=MIXED_METADATA)


def test_anInnerJoinWithEndedDataAsksOnDuckDb(harness):
    sql = "SELECT c.region, count(*) AS n FROM events e JOIN customers c ON c.id = e.cid WHERE e.ts >= CURRENT_DATE - 30 GROUP BY 1"
    state = _chartOnDuckDb(harness, sql, "region", ["n"])
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-03-15")}
    assert "generatedCode" not in state
    assert harness.render.call_count == 2
    assert harness.logs == []


def test_theFilledInPatternAsksOnDuckDb(harness):
    sql = (
        f"WITH {MONTHS_SQL}, {EVENTS_PER_MONTH_SQL} SELECT strftime(m.month_start, '%Y-%m') AS month, COALESCE(p.n, 0) AS n "
        "FROM months m LEFT JOIN per_month p ON p.month_start = m.month_start ORDER BY 1"
    )
    state = _chartOnDuckDb(harness, sql, "month", ["n"])
    assert state["finalOutput"] == {"response": _periodQuestion("the data ends on 2019-03-15")}
    assert harness.render.call_count == 2


@pytest.mark.parametrize(
    "sql, x, y, probed",
    [
        # Refunds inside the period: the probe runs and finds data.
        ("SELECT status, sum(amount) AS total FROM refunds WHERE r_at >= CURRENT_DATE - 90 GROUP BY 1", "status", ["total"], True),
        (
            "SELECT c.region, sum(r.amount) AS total FROM refunds r JOIN customers c ON c.id = r.cid "
            "WHERE r.r_at >= CURRENT_DATE - 90 GROUP BY 1",
            "region",
            ["total"],
            True,
        ),
        # A refunds measure joined after the events were filled in, or read in a scalar subquery (review I1).
        (
            f"WITH {MONTHS_SQL}, {EVENTS_PER_MONTH_SQL}, "
            "filled AS (SELECT m.month_start, COALESCE(p.n, 0) AS n FROM months m LEFT JOIN per_month p ON p.month_start = m.month_start), "
            "rpm AS (SELECT date_trunc('month', r_at) AS month_start, sum(amount) AS total FROM refunds GROUP BY ALL) "
            "SELECT strftime(f.month_start, '%Y-%m') AS month, f.n AS events, COALESCE(r.total, 0) AS refunds "
            "FROM filled f LEFT JOIN rpm r ON r.month_start = f.month_start ORDER BY 1",
            "month",
            ["events", "refunds"],
            False,
        ),
        (
            f"WITH {MONTHS_SQL}, {EVENTS_PER_MONTH_SQL} SELECT strftime(m.month_start, '%Y-%m') AS month, COALESCE(p.n, 0) AS events, "
            "(SELECT COALESCE(sum(amount), 0) FROM refunds r WHERE date_trunc('month', r.r_at) = m.month_start) AS refunds "
            "FROM months m LEFT JOIN per_month p ON p.month_start = m.month_start ORDER BY 1",
            "month",
            ["events", "refunds"],
            False,
        ),
        # Refunds beside a summary of no events that still gives a row (review I2).
        (
            "SELECT r.status, sum(r.amount) AS total FROM refunds r "
            "CROSS JOIN (SELECT count(*) AS k FROM events WHERE ts >= CURRENT_DATE - 30 GROUP BY ()) e GROUP BY 1",
            "status",
            ["total"],
            False,
        ),
    ],
)
def test_aLegitimateZeroInsideThePeriodNeverAsksOnDuckDb(harness, sql, x, y, probed):
    state = _chartOnDuckDb(harness, sql, x, y)
    assert state["finalOutput"]["chartType"] == "bar"
    values = [value for dataset in state["finalOutput"]["data"]["datasets"] for value in dataset["data"]]
    assert values and all(value in (0, None) for value in values)
    assert "generatedCode" in state
    assert harness.render.call_count == (2 if probed else 1)
    assert harness.logs == []


# Round 4 on a real DuckDB: the bound's column is bound by DuckDB to something other than the ended table column. Events
# ended long ago on `ts`, but `shipped` is today; refunds are inside the period; every amount is 0.

RECENT_SQL = "CURRENT_DATE - INTERVAL 30 DAY"
BINDING_METADATA = {
    "events": {"columns": [{"name": "id"}, {"name": "ts"}, {"name": "shipped"}, {"name": "kind"}, {"name": "amount"}]},
    "refunds": {"columns": [{"name": "eid"}, {"name": "r_at"}, {"name": "amount"}, {"name": "status"}]},
}


def _bindingTables():
    today = datetime.combine(date.today(), datetime.min.time())
    return {
        "events": pa.table(
            {
                "id": [1, 2],
                "ts": pa.array([datetime(2019, 3, 15)] * 2, pa.timestamp("us")),
                "shipped": pa.array([today] * 2, pa.timestamp("us")),
                "kind": ["a", "b"],
                "amount": pa.array([0, 0], pa.int64()),
            }
        ),
        "refunds": pa.table(
            {
                "eid": [1, 2, 3],
                "r_at": pa.array([today - timedelta(days=days) for days in (1, 2, 3)], pa.timestamp("us")),
                "amount": pa.array([0, 0, 0], pa.int64()),
                "status": ["done"] * 3,
            }
        ),
    }


@pytest.mark.parametrize(
    "sql, x",
    [
        # A star with REPLACE, or EXCLUDE plus RENAME (review 2, P1).
        (f"WITH x AS (SELECT * REPLACE (coalesce(shipped, ts) AS ts) FROM events) SELECT kind, sum(amount) AS t FROM x WHERE ts >= {RECENT_SQL} GROUP BY 1", "kind"),
        (f"WITH x AS (SELECT * REPLACE (CAST(CURRENT_DATE AS TIMESTAMP) AS ts) FROM events) SELECT kind, sum(amount) AS t FROM x WHERE ts >= {RECENT_SQL} GROUP BY 1", "kind"),
        (f"WITH x AS (SELECT * EXCLUDE (ts) RENAME (shipped AS ts) FROM events) SELECT kind, sum(amount) AS t FROM x WHERE ts >= {RECENT_SQL} GROUP BY 1", "kind"),
        # A star that already supplies the name of a later projection (review 2, P2).
        (
            f"WITH x AS (SELECT r.*, e.ts AS r_at FROM refunds r JOIN events e ON e.id = r.eid) "
            f"SELECT status, sum(amount) AS t FROM x WHERE r_at >= {RECENT_SQL} GROUP BY 1",
            "status",
        ),
        # A CTE defined later, or quoted in another case, that DuckDB reads instead of the table (review 2, P3).
        (
            f"WITH a AS (SELECT status, sum(amount) AS t FROM events WHERE ts >= {RECENT_SQL} GROUP BY 1), "
            "events AS (SELECT r_at AS ts, amount, status FROM refunds) SELECT * FROM a",
            "status",
        ),
        (
            f'WITH "EVENTS" AS (SELECT r_at AS ts, amount, status FROM refunds) '
            f"SELECT status, sum(amount) AS t FROM events WHERE ts >= {RECENT_SQL} GROUP BY 1",
            "status",
        ),
    ],
)
def test_aBoundWhoseColumnDuckDbBindsElsewhereKeepsTheChartOnDuckDb(harness, sql, x):
    request = {"chartType": "bar", "title": "Chart", "sql": sql, "encoding": {"x": x, "y": ["t"]}}
    harness.generator.invoke.return_value = json.dumps(request)
    harness.render.side_effect = _duckdbRender(_bindingTables())
    state = harness.run(metadata=BINDING_METADATA)
    assert state["finalOutput"]["chartType"] == "bar"
    values = state["finalOutput"]["data"]["datasets"][0]["data"]
    assert values and all(value == 0 for value in values)
    assert "generatedCode" in state
    assert harness.render.call_count == 1
    assert harness.logs == []
