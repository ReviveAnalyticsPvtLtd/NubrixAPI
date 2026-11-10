"""The CHART_ENGINE switch in the reporting service: off, shadow and on, with the shadow run's lifecycle."""

import asyncio
import copy
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.callbacks import BaseCallbackHandler

import api.services.reportingService as rs
from api.models import GenerateChartInput
from nubrix.components import chartFailureLog
from nubrix.workflows import sqlChartWorkflow
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError

METADATA = {"orders": {"columns": [{"name": "a"}]}}
CARD = {"chartType": "card", "title": "Total", "label": "Total", "data": 5}
PY_RESPONSE = {
    "projectId": "p1", "inputQuery": "q", "metadata": METADATA, "rephrasedQuery": {"rephrasedOutput": "r"},
    "generatedCode": "print(1)", "codeOutput": "{}", "finalOutput": CARD, "debugAttempts": 0,
}
SQL_RESPONSE = {
    "projectId": "p1", "inputQuery": "q", "metadata": METADATA, "rephrasedQuery": {"rephrasedOutput": "r"},
    "generatedCode": '{"nubrixChart":1}', "codeOutput": "{}", "finalOutput": CARD, "debugAttempts": 0,
}
SHADOW_KEYS = {"traceId", "projectId", "parallel", "pythonOk", "sqlOk", "sqlStage", "agreement", "contractErrors"}


class SharedHandler(BaseCallbackHandler):
    """Stands in for the process-wide Langfuse handler."""

    last_trace_id = "shared"


class FakeWorkflow:
    def __init__(self, response=None, error=None, name="workflow"):
        self.response = response
        self.error = error
        self.name = name
        self.calls = []
        self.gate = None
        self.started = threading.Event()

    def invoke(self, inputs, config=None):
        self.calls.append((inputs, config))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.response)


class FakeCredit(BaseCallbackHandler):
    made = []

    def __init__(self, userId, operationType):
        super().__init__()
        self.userId = userId
        self.operationType = operationType
        FakeCredit.made.append(self)


class RecordingExecutor:
    """A real two-thread pool that remembers its futures so a test can wait for the shadow run."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chart-shadow")
        self.futures = []

    def submit(self, fn, *args, **kwargs):
        future = self.pool.submit(fn, *args, **kwargs)
        self.futures.append(future)
        return future

    def wait(self):
        for future in list(self.futures):
            future.result(timeout=20)


class Env:
    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        FakeCredit.made = []
        self.python = FakeWorkflow(PY_RESPONSE, name="python")
        self.sql = FakeWorkflow(SQL_RESPONSE, name="sql")
        self.built = []
        self.configCalls = []
        self.logs = []
        self.executor = RecordingExecutor()
        self.service = object.__new__(rs.ReportingService)
        self.service.reportingToolWorkflow = self.python
        self.service._getProjectMetadata = AsyncMock(return_value=METADATA)
        monkeypatch.setattr(rs, "updateProjectModifiedAt", lambda *_: None)
        monkeypatch.setattr(rs, "buildSqlChartWorkflow", self.buildSql)
        monkeypatch.setattr(rs, "_sqlWorkflows", {})
        monkeypatch.setattr(rs.threadLocal, "sqlParallelWorkflow", None, raising=False)
        monkeypatch.setattr(rs.threadLocal, "parallelWorkflow", self.python, raising=False)
        monkeypatch.setattr(rs, "CreditTrackingCallback", FakeCredit)
        monkeypatch.setattr(rs, "_shadowExecutor", self.executor)
        monkeypatch.setattr(rs, "_shadowSlots", threading.BoundedSemaphore(4))
        monkeypatch.setattr("utils.llm.getLangfuseConfig", self.getConfig)
        monkeypatch.setattr(rs.logger, "info", lambda message, *a, **k: self.logs.append(str(message)))
        monkeypatch.setattr(rs.logger, "warning", lambda message, *a, **k: self.logs.append(str(message)))

    def buildSql(self, parallel=False):
        self.built.append(parallel)
        return self.sql

    def getConfig(self, **kwargs):
        self.configCalls.append(kwargs)
        handler = SharedHandler()
        if kwargs.get("perRequestHandler"):
            handler.last_trace_id = f"own-{len(self.configCalls)}"
        return {"callbacks": [handler], "metadata": {"trace_name": kwargs["trace_name"], "langfuse_tags": kwargs.get("tags")}}

    def mode(self, value):
        self.monkeypatch.setenv("CHART_ENGINE", value)

    def chart(self, userId="u1"):
        return asyncio.run(self.service.generateChart(GenerateChartInput(projectId="p1", inputQuery="q"), userId=userId))

    def parallel(self, userId="u1", query="q"):
        return rs.ReportingService._generateSingleChartForParallel(METADATA, "p1", query, userId)

    def shadowLines(self):
        return [json.loads(line[len("chart_shadow "):]) for line in self.logs if line.startswith("chart_shadow ")]

    def free(self):
        """Whether all four shadow permits are free (and puts them back)."""
        taken = 0
        while rs._shadowSlots.acquire(blocking=False):
            taken += 1
        for _ in range(taken):
            rs._shadowSlots.release()
        return taken == 4


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


# Serving

def test_offUsesPythonOnly(env):
    env.mode("off")
    response = env.chart()
    assert response == PY_RESPONSE
    assert len(env.python.calls) == 1 and env.sql.calls == [] and env.built == []
    assert env.executor.futures == []


def test_missingAndUnknownModeAreOff(env, monkeypatch):
    monkeypatch.delenv("CHART_ENGINE", raising=False)
    env.chart()
    env.mode("bogus")
    env.chart()
    assert len(env.python.calls) == 2 and env.sql.calls == [] and env.executor.futures == []


def test_onUsesSql(env):
    env.mode("on")
    response = env.chart()
    assert response == SQL_RESPONSE
    assert env.python.calls == [] and len(env.sql.calls) == 1
    inputs, config = env.sql.calls[0]
    assert inputs == {"metadata": METADATA, "inputQuery": "q", "projectId": "p1"}
    assert "shadow" not in (config.get("configurable") or {})
    assert env.executor.futures == []


def test_modeIsReadPerRequest(env):
    env.mode("on")
    env.chart()
    env.mode("off")
    env.chart()
    env.mode("on")
    env.chart()
    assert [len(env.sql.calls), len(env.python.calls)] == [2, 1]
    assert env.built == [False]  # built once, on first use


def test_onParallelUsesAThreadLocalSqlWorkflow(env):
    env.mode("on")
    response = env.parallel()
    assert env.python.calls == [] and len(env.sql.calls) == 1 and env.built == [True]
    assert response["finalOutput"] == CARD
    assert not {"metadata", "rephrasedQuery", "codeOutput"} & set(response)
    env.parallel()
    assert env.built == [True]

    thread = threading.Thread(target=env.parallel)
    thread.start()
    thread.join()
    assert env.built == [True, True]


# Shadow

def test_shadowServesPythonAndQueuesSql(env):
    env.mode("shadow")
    response = env.chart()
    env.executor.wait()
    assert response == PY_RESPONSE
    assert len(env.python.calls) == 1 and len(env.sql.calls) == 1
    sqlInputs, sqlConfig = env.sql.calls[0]
    assert sqlInputs == {"metadata": METADATA, "inputQuery": "q", "projectId": "p1"}
    assert sqlConfig["configurable"]["shadow"] is True
    assert not any(isinstance(callback, FakeCredit) for callback in sqlConfig["callbacks"])
    shadowCall = env.configCalls[-1]
    assert shadowCall["trace_name"] == "ReportingWorkflowShadow"
    assert shadowCall["tags"] == ["reporting", "shadow"]
    assert shadowCall["perRequestHandler"] is True
    # The Python request keeps the credit callback, once; the shadow never makes one.
    assert len(FakeCredit.made) == 1
    assert any(isinstance(callback, FakeCredit) for callback in env.python.calls[0][1]["callbacks"])


def test_shadowRunsOnItsOwnThreadAndNeverBlocksTheRequest(env):
    env.mode("shadow")
    env.sql.gate = threading.Event()
    names = []
    original = env.sql.invoke

    def invoke(inputs, config=None):
        names.append(threading.current_thread().name)
        return original(inputs, config)

    env.sql.invoke = invoke
    response = env.chart()
    assert response == PY_RESPONSE
    assert env.sql.started.wait(10)
    assert env.shadowLines() == []
    env.sql.gate.set()
    env.executor.wait()
    assert names[0].startswith("chart-shadow")
    assert len(env.shadowLines()) == 1


def test_shadowPoolAndQueueAreBounded():
    assert rs._shadowExecutor._max_workers == 2
    assert rs._shadowExecutor._thread_name_prefix == "chart-shadow"
    semaphore = rs._shadowSlots
    taken = [semaphore.acquire(blocking=False) for _ in range(5)]
    for ok in taken:
        if ok:
            semaphore.release()
    assert taken == [True, True, True, True, False]


def test_shadowQueueFullIsDropped(env):
    env.mode("shadow")
    for _ in range(4):
        assert rs._shadowSlots.acquire(blocking=False)
    response = env.chart()
    assert response == PY_RESPONSE
    assert env.sql.calls == [] and env.executor.futures == []
    lines = env.shadowLines()
    assert len(lines) == 1 and lines[0]["result"] == "dropped_queue"
    assert set(lines[0]) == SHADOW_KEYS | {"result"}
    assert lines[0]["sqlOk"] is None and lines[0]["pythonOk"] is True and lines[0]["projectId"] == "p1"
    for _ in range(4):
        rs._shadowSlots.release()


def test_shadowLogLine(env):
    env.mode("shadow")
    env.chart()
    env.executor.wait()
    (line,) = env.shadowLines()
    assert set(line) == SHADOW_KEYS
    assert line == {
        "traceId": "own-2", "projectId": "p1", "parallel": False, "pythonOk": True, "sqlOk": True,
        "sqlStage": None, "agreement": "match", "contractErrors": 0,
    }
    assert env.free()


def test_shadowLogLineDisagreementAndContractErrors(env):
    env.mode("shadow")
    env.sql.response = {**SQL_RESPONSE, "finalOutput": {"chartType": "card", "title": "Total", "label": "L", "data": 6}}
    env.chart()
    env.executor.wait()
    env.sql.response = {**SQL_RESPONSE, "finalOutput": {"chartType": "card", "label": "L", "data": 5}}
    env.chart()
    env.executor.wait()
    first, second = env.shadowLines()
    assert first["agreement"] == "differ" and first["contractErrors"] == 0
    assert second["agreement"] == "match" and second["contractErrors"] == 1


def test_shadowLogLineWhenPythonFailed(env):
    env.mode("shadow")
    env.python.response = {**PY_RESPONSE, "finalOutput": {"error": "bad"}}
    env.chart()
    env.executor.wait()
    (line,) = env.shadowLines()
    assert line["pythonOk"] is False and line["sqlOk"] is True and line["agreement"] is None


def test_shadowSqlFailureIsLoggedWithItsStage(env):
    env.mode("shadow")
    env.sql.response = {**SQL_RESPONSE, "finalOutput": {"error": 'Binder Error: column "secret_column" not found'}}
    env.chart()
    env.executor.wait()
    (line,) = env.shadowLines()
    assert line["sqlOk"] is False and line["sqlStage"] == "failed" and line["agreement"] is None and line["contractErrors"] is None
    assert "secret_column" not in json.dumps(line)
    assert "result" not in line


@pytest.mark.parametrize("busy", ["response", "raised", "wrapped"])
def test_shadowBusyIsDropped(env, busy):
    env.mode("shadow")
    if busy == "response":
        env.sql.response = {**SQL_RESPONSE, "finalOutput": {"error": BUSY_MESSAGE}}
    elif busy == "raised":
        env.sql.error = SandboxBusyError()
    else:
        error = RuntimeError("503")
        error.errorCode = "SANDBOX_BUSY"
        env.sql.error = error
    response = env.chart()
    env.executor.wait()
    assert response == PY_RESPONSE
    (line,) = env.shadowLines()
    assert line["result"] == "dropped_busy" and line["sqlOk"] is None and line["sqlStage"] is None
    assert set(line) == SHADOW_KEYS | {"result"}
    assert env.free()


@pytest.mark.parametrize("error", [RuntimeError("quota for key 12345 exceeded"), ValueError("secret value"), KeyError("k")])
def test_shadowCatchesEveryErrorAndReleasesItsPermit(env, error):
    env.mode("shadow")
    env.sql.error = error
    response = env.chart()
    env.executor.wait()
    assert response == PY_RESPONSE
    (line,) = env.shadowLines()
    assert line["sqlOk"] is False and line["sqlStage"] == type(error).__name__ and line["agreement"] is None
    assert set(line) == SHADOW_KEYS
    assert "12345" not in json.dumps(line) and "secret" not in json.dumps(line)
    assert env.free()


def test_shadowCatchesAnErrorInTheConfig(env, monkeypatch):
    env.mode("shadow")

    def getConfig(**kwargs):
        if kwargs.get("perRequestHandler"):
            raise RuntimeError("langfuse is down")
        return {"callbacks": [SharedHandler()]}

    monkeypatch.setattr("utils.llm.getLangfuseConfig", getConfig)
    response = env.chart()
    env.executor.wait()
    assert response == PY_RESPONSE
    (line,) = env.shadowLines()
    assert line["sqlOk"] is False and line["sqlStage"] == "RuntimeError"
    assert env.free()


def test_shadowCannotSubmitReleasesItsPermit(env):
    env.mode("shadow")

    class Broken:
        def submit(self, *args, **kwargs):
            raise RuntimeError("cannot schedule new futures after shutdown")

    env.monkeypatch.setattr(rs, "_shadowExecutor", Broken())
    response = env.chart()
    assert response == PY_RESPONSE
    assert env.free()


def test_shadowLogFailureStillReleasesThePermit(env):
    env.mode("shadow")

    def broken(message, *a, **k):
        if str(message).startswith("chart_shadow"):
            raise OSError("log sink gone")
        env.logs.append(str(message))

    env.monkeypatch.setattr(rs.logger, "info", broken)
    response = env.chart()
    env.executor.wait()  # the pool thread swallowed the error; it never reaches the future either
    assert response == PY_RESPONSE
    assert env.free()


def test_shadowParallel(env):
    env.mode("shadow")
    response = env.parallel()
    env.executor.wait()
    assert response["finalOutput"] == CARD and len(env.python.calls) == 1
    assert env.built == [True]
    (line,) = env.shadowLines()
    assert line["parallel"] is True and line["agreement"] == "match" and set(line) == SHADOW_KEYS
    shadowConfig = env.sql.calls[0][1]
    assert shadowConfig["configurable"]["shadow"] is True
    assert env.configCalls[-1]["trace_name"] == "ReportingWorkflowShadow"
    assert len(FakeCredit.made) == 1  # the served Python request only


def test_shadowIsNotQueuedWhenThePythonRequestRaises(env):
    env.mode("shadow")
    env.python.error = RuntimeError("python broke")
    with pytest.raises(Exception):
        env.chart()
    assert env.executor.futures == [] and env.shadowLines() == []


# Credits and Langfuse handlers

def test_onAttachesCreditsOnceAndAPerRequestHandler(env):
    env.mode("on")
    env.chart(userId="u7")
    (credit,) = FakeCredit.made
    assert (credit.userId, credit.operationType) == ("u7", "reporting_query")
    callbacks = env.sql.calls[0][1]["callbacks"]
    assert sum(isinstance(callback, FakeCredit) for callback in callbacks) == 1
    call = env.configCalls[0]
    assert call["trace_name"] == "ReportingWorkflow" and call["tags"] == ["reporting", "generateChart"]
    assert call["projectId"] == "p1" and call["userId"] == "u7" and call["perRequestHandler"] is True


def test_onWithoutAUserAttachesNoCredits(env):
    env.mode("on")
    env.chart(userId=None)
    assert FakeCredit.made == []


def test_onParallelAttachesCreditsOnceAndAPerRequestHandler(env):
    env.mode("on")
    env.parallel(userId="u7")
    assert len(FakeCredit.made) == 1
    call = env.configCalls[0]
    assert call["trace_name"] == "ParallelReportingWorkflow" and call["tags"] == ["reporting", "parallelChart"]
    assert call["perRequestHandler"] is True


def test_pythonPathKeepsTheSharedHandler(env):
    env.mode("off")
    env.chart()
    env.parallel()
    assert all("perRequestHandler" not in call for call in env.configCalls)


def test_twoConcurrentSqlRunsLogTheirOwnTraceIds(monkeypatch):
    """Every chain start overwrites a handler's last_trace_id; two runs sharing one handler would log the same id."""
    import utils.llm as llm

    inFormat = threading.Barrier(2, timeout=10)
    inEvaluate = threading.Barrier(2, timeout=10)
    handlers = []
    counter = iter(range(1000))

    class OverwritingHandler(BaseCallbackHandler):
        def __init__(self):
            super().__init__()
            self.ids = []
            self.last_trace_id = None
            handlers.append(self)

        def on_chain_start(self, serialized, inputs, **kwargs):
            self.last_trace_id = f"trace-{next(counter)}"
            self.ids.append(self.last_trace_id)
            if kwargs.get("name") == "format":
                inFormat.wait()

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(llm, "CallbackHandler", OverwritingHandler)
    monkeypatch.setattr(llm, "_langfuse_handler", None)

    generator = MagicMock()
    generator.invoke.return_value = json.dumps(
        {"chartType": "bar", "title": "T", "sql": 'SELECT "a" FROM "orders"', "encoding": {"x": "a", "y": ["a"]}}
    )
    rephraser = MagicMock()
    rephraser.invoke.return_value = {"rephrasedOutput": "plan", "doubt": None}
    chains = {"sqlChartGeneratorPrompt": generator, "sqlChartRepairPrompt": MagicMock()}
    monkeypatch.setattr(sqlChartWorkflow, "buildLlmChain", lambda section, prompt, **kw: chains[prompt])
    rephraserClass = MagicMock()
    rephraserClass.return_value.getQueryRephraserChain.return_value = rephraser
    monkeypatch.setattr(sqlChartWorkflow, "QueryRephaser", rephraserClass)
    monkeypatch.setattr(sqlChartWorkflow, "tableSchemas", MagicMock(return_value={}))
    monkeypatch.setattr(sqlChartWorkflow, "REPLManager", MagicMock())

    # A full sandbox ends a run at once, with a single failure log.
    def busy(*args, **kwargs):
        inEvaluate.wait()
        raise SandboxBusyError()

    monkeypatch.setattr(sqlChartWorkflow, "renderChartRequest", busy)
    logs = []
    monkeypatch.setattr(chartFailureLog.logger, "warning", lambda message, *a, **k: logs.append(message))
    monkeypatch.setattr(rs, "updateProjectModifiedAt", lambda *_: None)
    monkeypatch.setattr(rs, "_sqlWorkflows", {})
    monkeypatch.setenv("CHART_ENGINE", "on")
    service = object.__new__(rs.ReportingService)
    service._getProjectMetadata = AsyncMock(return_value={"orders": {"columns": [{"name": "a"}]}})

    async def both():
        details = GenerateChartInput(projectId="p1", inputQuery="q")
        return await asyncio.gather(service.generateChart(details, userId=None), service.generateChart(details, userId=None))

    first, second = asyncio.run(both())
    assert first["finalOutput"] == {"error": BUSY_MESSAGE} and second["finalOutput"] == {"error": BUSY_MESSAGE}
    records = [json.loads(line[len("chart_failure "):]) for line in logs if line.startswith("chart_failure ")]
    assert len(records) == 2
    traceIds = [record["traceId"] for record in records]
    assert traceIds[0] != traceIds[1]
    assert len(handlers) == 2
    for traceId in traceIds:
        assert sum(traceId in handler.ids for handler in handlers) == 1


# Parallel failure card

@pytest.mark.parametrize("engine", ["off", "on"])
def test_parallelErrorBecomesFailureCard(env, engine):
    env.mode(engine)
    failing = {"finalOutput": {"error": "Conversion Error: Could not convert string 'zebra' to INT32\nLINE 2: SELECT 'zebra'"}, "generatedCode": "x"}
    (env.python if engine == "off" else env.sql).response = failing
    response = env.parallel(query="how many?")
    assert response == {
        "finalOutput": {
            "chartType": "card", "title": "Chart: how many?", "label": "Status",
            "xLabels": "Status", "yLabels": "Value", "data": "Error/No data",
        },
        "generatedCode": "# Failed to generate code for: how many?\n# Error: Conversion Error: Could not convert string '?' to INT32",
    }


@pytest.mark.parametrize("engine", ["off", "on"])
def test_parallelErrorWithoutDataKeepsItsText(env, engine):
    env.mode(engine)
    (env.python if engine == "off" else env.sql).response = {"finalOutput": {"error": BUSY_MESSAGE}}
    response = env.parallel(query="q")
    assert response["finalOutput"]["data"] == "Error/No data"
    assert response["generatedCode"] == f"# Failed to generate code for: q\n# Error: {BUSY_MESSAGE}"


def test_parallelExceptionCardIsUnchanged(env):
    env.mode("off")
    env.python.error = RuntimeError("boom")
    response = env.parallel(query="q")
    assert response["generatedCode"] == "# Failed to generate code for: q\n# Error: boom"
    assert response["finalOutput"]["data"] == "Error/No data"


def test_parallelFailureStillQueuesTheShadow(env):
    env.mode("shadow")
    env.python.response = {"finalOutput": {"error": "bad"}}
    response = env.parallel()
    env.executor.wait()
    assert response["finalOutput"]["data"] == "Error/No data"
    (line,) = env.shadowLines()
    assert line["pythonOk"] is False and line["parallel"] is True


# Off is today's behaviour

def _legacyGenerateChart(service, chartDetails, userId):
    """generateChart as it was before the switch (6539c2f), kept here to compare calls and responses."""
    from utils.llm import getLangfuseConfig

    reportConfig = getLangfuseConfig(
        trace_name="ReportingWorkflow", projectId=chartDetails.projectId, userId=userId,
        tags=["reporting", "generateChart"],
    )
    if userId:
        reportConfig.setdefault("callbacks", []).append(rs.CreditTrackingCallback(userId=userId, operationType="reporting_query"))
    metadata = asyncio.run(service._getProjectMetadata(chartDetails.projectId))
    response = service.reportingToolWorkflow.invoke(
        {"metadata": metadata, "inputQuery": chartDetails.inputQuery, "projectId": chartDetails.projectId},
        config=reportConfig or None,
    )
    rs.updateProjectModifiedAt(chartDetails.projectId)
    return response


def _legacyParallel(metadata, projectId, query, userId):
    """_generateSingleChartForParallel as it was before the switch."""
    from utils.llm import getLangfuseConfig

    try:
        workflow = rs.threadLocal.parallelWorkflow
        reportConfig = getLangfuseConfig(
            trace_name="ParallelReportingWorkflow", projectId=projectId, userId=userId, tags=["reporting", "parallelChart"],
        )
        if userId:
            reportConfig.setdefault("callbacks", []).append(rs.CreditTrackingCallback(userId=userId, operationType="reporting_query"))
        response = workflow.invoke({"metadata": metadata, "inputQuery": query, "projectId": projectId}, config=reportConfig or None)
        _ = response.pop("metadata", None)
        _ = response.pop("rephrasedQuery", None)
        _ = response.pop("codeOutput", None)
        return response
    except Exception as e:
        return {
            "finalOutput": {"chartType": "card", "title": f"Chart: {query}", "label": "Status", "xLabels": "Status", "yLabels": "Value", "data": "Error/No data"},
            "generatedCode": f"# Failed to generate code for: {query}\n# Error: {e}",
        }


def _trace(env):
    """What a run did: the config requests, the credit callbacks made, and each invoke's inputs and callback types."""
    return {
        "configs": copy.deepcopy(env.configCalls),
        "credits": [(c.userId, c.operationType) for c in FakeCredit.made],
        "invokes": [
            (inputs, [type(cb).__name__ for cb in (config or {}).get("callbacks", [])], (config or {}).get("metadata"))
            for inputs, config in env.python.calls
        ],
    }


def _reset(env):
    env.configCalls.clear()
    env.python.calls.clear()
    FakeCredit.made = []


@pytest.mark.parametrize("userId", ["u1", None])
def test_offIsByteForByteTheOldGenerateChart(env, userId):
    env.mode("off")
    new = env.chart(userId=userId)
    newTrace = _trace(env)
    _reset(env)
    old = _legacyGenerateChart(env.service, GenerateChartInput(projectId="p1", inputQuery="q"), userId)
    assert json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True)
    assert newTrace == _trace(env)
    assert env.built == [] and env.executor.futures == []


@pytest.mark.parametrize("userId", ["u1", None])
@pytest.mark.parametrize("failure", [False, True])
def test_offIsByteForByteTheOldParallelPath(env, userId, failure):
    env.mode("off")
    if failure:
        env.python.error = RuntimeError("boom")
    new = env.parallel(userId=userId)
    newTrace = _trace(env)
    _reset(env)
    old = _legacyParallel(METADATA, "p1", "q", userId)
    assert json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True)
    assert newTrace == _trace(env)
    assert env.built == [] and env.executor.futures == []


# getLangfuseConfig: a handler of its own only when asked

def test_getLangfuseConfigPerRequestHandlerIsNewEachTime(monkeypatch):
    import utils.llm as llm

    class Handler(BaseCallbackHandler):
        pass

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setattr(llm, "CallbackHandler", Handler)
    monkeypatch.setattr(llm, "_langfuse_handler", None)
    shared = [llm.getLangfuseConfig(trace_name="ReportingWorkflow")["callbacks"][0] for _ in range(2)]
    own = [llm.getLangfuseConfig(trace_name="ReportingWorkflow", tags=["t"], perRequestHandler=True) for _ in range(2)]
    assert shared[0] is shared[1] is llm.getLangfuseHandler()
    assert own[0]["callbacks"][0] is not own[1]["callbacks"][0]
    assert own[0]["callbacks"][0] is not shared[0]
    assert own[0]["metadata"]["trace_name"] == "ReportingWorkflow" and own[0]["metadata"]["langfuse_tags"] == ["t"]


def test_getLangfuseConfigWithoutKeysIsEmpty(monkeypatch):
    import utils.llm as llm

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(llm, "_langfuse_handler", None)
    assert llm.getLangfuseConfig(trace_name="x") == {}
    assert llm.getLangfuseConfig(trace_name="x", perRequestHandler=True) == {}


# Copy cost: nothing paid by a dropped run, nothing on the event loop

def _countCopies(env):
    """Records, per deepcopy call made by the reporting service, whether it ran inside an event loop."""
    calls = []
    original = copy.deepcopy

    def recording(value, *args, **kwargs):
        if sys._getframe(1).f_globals.get("__name__") != "api.services.reportingService":
            return original(value, *args, **kwargs)
        try:
            asyncio.get_running_loop()
            onLoop = True
        except RuntimeError:
            onLoop = False
        calls.append(onLoop)
        return original(value, *args, **kwargs)

    env.monkeypatch.setattr(rs.copy, "deepcopy", recording)
    return calls


def test_droppedShadowMakesNoCopy(env):
    env.mode("shadow")
    for _ in range(4):
        assert rs._shadowSlots.acquire(blocking=False)
    calls = _countCopies(env)
    env.chart()
    assert calls == []
    assert env.shadowLines()[0]["result"] == "dropped_queue"
    for _ in range(4):
        rs._shadowSlots.release()


def test_singleChartShadowCopyIsMadeOffTheEventLoop(env):
    env.mode("shadow")
    calls = _countCopies(env)
    env.chart()
    env.executor.wait()
    assert calls == [False]
    assert len(env.shadowLines()) == 1


def test_shadowRunsOnACopyOfTheServedChart(env):
    env.mode("shadow")
    seen = []
    env.python.response = copy.deepcopy(PY_RESPONSE)
    original = rs._runShadow

    def spy(parallel, projectId, userId, metadata, query, pythonOutput):
        seen.append(pythonOutput)
        return original(parallel, projectId, userId, metadata, query, pythonOutput)

    env.monkeypatch.setattr(rs, "_runShadow", spy)
    served = env.python.response["finalOutput"]
    env.python.invoke = lambda inputs, config=None: {"finalOutput": served}
    env.chart()
    env.executor.wait()
    assert seen[0] == served and seen[0] is not served
