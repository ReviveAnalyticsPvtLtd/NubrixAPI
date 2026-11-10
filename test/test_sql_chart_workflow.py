"""The SQL chart workflow: rephrase, ground, generate, evaluate, repair (at most twice), format."""

import json
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
