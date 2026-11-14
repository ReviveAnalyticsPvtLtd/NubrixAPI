"""
sqlChartWorkflow.py

The SQL chart workflow: rephrase the question, describe the project's tables, have the model write a chart request
(one DuckDB SELECT plus an encoding), check and run it in the sandbox, build the chart, and repair the request
with the exact error (at most twice) when a step fails.

The response keeps the keys of the Python chart workflows. Everything else stays in internal state.

When the served workflow's chart shows no data and its query filters on dates counted back from today (see
periodProbe.py for which bounds count), the probe node checks, in one sandbox run, whether the data each bound
filters on ends before its period. Only when at least one bound ended and, given the bounds that ended, the chart
provably holds no project data does the response become a question about the period, in the shape of the
rephraser's doubt. Anything else, including a probe that fails, keeps the chart.

    START -> rephrase -> ground -> generate -> (evaluate | repair | format)
                  \\-> format (doubt)
    ground -> format (a busy sandbox while reading table schemas)
    evaluate -> (format | probe | repair)
    probe -> format (served workflow, a chart without data: the chart, or a question about the period)
    repair -> (evaluate | repair | format)
    format -> END
"""

__all__ = ["buildSqlChartWorkflow"]

import json
import re
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError
from typing_extensions import TypedDict

from nubrix.components.chartEngine import ChartFailure, renderChartRequest
from nubrix.components.chartFailureLog import logChartFailure, traceIdOf
from nubrix.components.chartRequest import ChartRequest, encodeGeneratedCode
from nubrix.components.llmChainFactory import buildLlmChain
from nubrix.components.periodProbe import PeriodProbe, chartHasNoData, combinedProbeSql, findPeriodProbes, isDataFree
from nubrix.components.queryRephraser import ParallelQueryRephaser, QueryRephaser
from nubrix.components.sqlGrounding import groundingText
from utils.codeExecutor import REPLManager
from utils.initMethods import serializer
from utils.logger import logger
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError, isBusy
from utils.tableSchemas import tableSchemas

MAX_REPAIRS = 2
RUN_TIMEOUT_SECONDS = 35
_MODEL_LOG_MESSAGE = "The model reply is not a valid chart request."
_TERMINAL_STAGES = ("timeout", "busy")
_UNEXPECTED_MESSAGE = "The chart query could not be run."

# The question asked when the data a chart's period filters on ends before that period: `question` with one `reason`.
PERIOD_QUESTION = {
    "question": "There is no data for the period you asked about: {reason}. Which period should the chart cover?",
    "ended": "the data ends on {date}",
    "empty": "the table has no dated rows",
}
_PROBE_TITLE = "Period check"
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class _ProbeFailed(Exception):
    """A probe that gave no answer; `stage` is all the warning line says about it."""

    def __init__(self, stage: str, error: BaseException | None = None):
        super().__init__(stage)
        self.stage = stage
        self.error = error


def periodQuestion(lastDate: str | None) -> str:
    """The period question, naming the latest date the data reaches, or saying there is none."""
    reason = PERIOD_QUESTION["ended"].format(date=lastDate) if lastDate else PERIOD_QUESTION["empty"]
    return PERIOD_QUESTION["question"].format(reason=reason)


class OutputState(TypedDict, total=False):
    """What the workflow returns: today's response keys, nothing else."""

    projectId: str
    inputQuery: str
    metadata: dict
    rephrasedQuery: Any
    generatedCode: str
    codeOutput: str
    finalOutput: dict
    debugAttempts: int


class State(OutputState, total=False):
    """The response keys plus the internal state that never reaches the response."""

    chartRequest: dict | None
    # The last request the model produced that validated, kept through later invalid replies.
    lastRequest: dict | None
    chart: dict | None
    # {stage, message, error, repairable?}: `error` is the exception behind the failure, kept for the failure log.
    failure: dict | None
    grounding: str
    tables: list[str]
    rawText: str


def activeTableNames(metadata: Any) -> list[str]:
    """The project's active table names: every metadata entry not marked `isActive: false`."""
    if not isinstance(metadata, dict):
        return []
    return [name for name, entry in metadata.items() if not (isinstance(entry, dict) and entry.get("isActive") is False)]


def _readRequest(text: str) -> ChartRequest:
    """The chart request in a model reply: the first JSON object that validates.

    The reply may hold reasoning text before the JSON (the chain only strips the think tags) or a code fence around it,
    so each '{' is tried as the start of an object. When none validates, the error raised is the one for the object
    most likely to be the request: the last one with a `chartType` key, else the longest. When a '{' that did not
    decode starts a longer stretch than that object, with no `chartType` object after it, the reply was cut off or is
    not JSON, and the error is the invalid-JSON one for that stretch. A '{' inside a decoded object (in one of its
    strings) starts nothing, and a stretch only outranks an object with a `chartType` key when it holds `"chartType"`
    itself, since only then can it be a cut-off request.
    """
    decoder = json.JSONDecoder()
    decoded: list[tuple[int, int, dict]] = []
    undecoded: list[int] = []
    errors: dict[int, ValidationError] = {}
    position = text.find("{")
    while position != -1:
        try:
            parsed, end = decoder.raw_decode(text, position)
        except ValueError:
            undecoded.append(position)
        else:
            if isinstance(parsed, dict):
                try:
                    return ChartRequest.model_validate(parsed)
                except ValidationError as error:
                    decoded.append((position, end, parsed))
                    errors[position] = error
        position = text.find("{", position + 1)
    undecoded = [cut for cut in undecoded if not any(start < cut < end for start, end, _ in decoded)]
    withType = [item for item in decoded if "chartType" in item[2]]
    best = withType[-1] if withType else max(decoded, key=lambda item: item[1] - item[0], default=None)
    span = best[1] - best[0] if best else 0
    after = best[0] if withType else -1
    for cut in undecoded:
        if len(text) - cut > span and cut > after and (not withType or '"chartType"' in text[cut:]):
            # Invalid JSON, for the stretch that could not be read.
            return ChartRequest.model_validate_json(text[cut:].strip())
    if best:
        raise errors[best[0]]
    return ChartRequest.model_validate_json(text.strip())


def _modelFailure(error: ValidationError) -> ChartFailure:
    """A stage-model failure whose message names the problems without echoing the reply."""
    items = error.errors(include_url=False, include_input=False, include_context=False)
    problems = []
    for item in items:
        where = ".".join(str(part) for part in item["loc"])
        problems.append(f"{where}: {item['msg']}" if where else item["msg"])
    types = ", ".join(dict.fromkeys(item["type"] for item in items))
    failure = ChartFailure(
        "model",
        "The reply is not a valid chart request: " + "; ".join(problems),
        logMessage=f"{_MODEL_LOG_MESSAGE} Error types: {types}.",
    )
    failure.__cause__ = error
    return failure


def _callbackHandlers(config: RunnableConfig | None) -> list:
    """The handlers of a config's callbacks, which LangGraph hands to a node as a manager rather than a list."""
    callbacks = (config or {}).get("callbacks")
    handlers = getattr(callbacks, "handlers", callbacks)
    return list(handlers or [])


def _isShadow(config: RunnableConfig | None) -> bool:
    return ((config or {}).get("configurable") or {}).get("shadow") is True


def _shadowOutcome(config: RunnableConfig | None) -> dict | None:
    """A shadow run's outcome sink (`configurable["outcome"]`), which the format node fills with the failure's stage."""
    outcome = ((config or {}).get("configurable") or {}).get("outcome")
    return outcome if _isShadow(config) and isinstance(outcome, dict) else None


def _endedBusy(failure: dict) -> bool:
    """Whether a failure is a busy sandbox, whichever node it came from, including a busy error wrapped as a run failure."""
    error = failure.get("error")
    return failure["stage"] == "busy" or isBusy(error) or isBusy(getattr(error, "__cause__", None))


# Slice charts share their roles (label, value) and their SQL rules, so the type is only a presentation choice. When the
# plan opens by naming one and the model answered with another, the plan's type is served.
_SLICE_TYPES = {"pie": "pie", "doughnut": "doughnut", "polararea": "polarArea"}
_PLAN_SLICE_TYPE = re.compile(r"^\W*(?:an?\s+)?(pie|doughnut|polar\s*area)\s+chart\b", re.IGNORECASE)


def _withPlanSliceType(update: dict, plan: str) -> dict:
    request = update.get("chartRequest")
    if not request or request.get("chartType") not in _SLICE_TYPES.values():
        return update
    match = _PLAN_SLICE_TYPE.match(plan or "")
    if match is None:
        return update
    named = _SLICE_TYPES[re.sub(r"\s+", "", match.group(1)).lower()]
    if named != request["chartType"]:
        request = {**request, "chartType": named}
        update.update(chartRequest=request, lastRequest=request)
    return update


class SqlChartWorkflow:
    """Builds the nodes; each workflow owns its chains and sandbox manager so parallel runs share no state."""

    def __init__(self, parallel: bool = False):
        rephraser = ParallelQueryRephaser(promptKey="parallelSqlQueryRephraserAgentPrompt") if parallel else QueryRephaser(promptKey="sqlQueryRephraserAgentPrompt")
        self.parallel = parallel
        self.rephraseChain = rephraser.getQueryRephraserChain()
        self.generatorChain = buildLlmChain("SQLCHARTGENERATOR", "sqlChartGeneratorPrompt", jsonOutput=True)
        self.repairChain = buildLlmChain("SQLCHARTREPAIR", "sqlChartRepairPrompt", jsonOutput=True)
        # The same limit as a dashboard widget's run.
        self.replManager = REPLManager(timeoutSeconds=RUN_TIMEOUT_SECONDS)

    # Nodes

    def _rephrase(self, state: State, config: RunnableConfig):
        try:
            response = self.rephraseChain.invoke({"query": state["inputQuery"], "metadata": state["metadata"]}, config)
            if self.parallel and not response.get("rephrasedOutput"):
                response["rephrasedOutput"] = state["inputQuery"]
        except Exception as error:
            if not self.parallel:
                raise
            logger.warning(f"Query rephrase failed: {type(error).__name__}. Falling back to input query.")
            response = {"rephrasedOutput": state["inputQuery"]}
        return {"rephrasedQuery": response}

    @staticmethod
    def _doubtOf(state: State):
        rephrased = state.get("rephrasedQuery")
        return rephrased.get("doubt") if isinstance(rephrased, dict) else None

    def _afterRephrase(self, state: State):
        return "format" if self._doubtOf(state) is not None else "ground"

    def _ground(self, state: State, config: RunnableConfig | None = None):
        tables = activeTableNames(state["metadata"])
        try:
            # A table that cannot be read is left out here and gets UNKNOWN types in the grounding.
            # A shadow run reads only while the sandbox has room to spare, else it ends as busy and is dropped.
            options = {"leaveHeadroom": True} if _isShadow(config) else {}
            schemas = tableSchemas(state["projectId"], tables, skipUnreadable=True, **options)
        except Exception as error:
            if not isBusy(error):
                raise
            # No model call is worth making when the sandbox that reads the tables is full.
            return {"tables": tables, "failure": {"stage": "busy", "message": BUSY_MESSAGE, "error": error}}
        return {"tables": tables, "grounding": groundingText(state["metadata"], schemas), "failure": None}

    @staticmethod
    def _afterGround(state: State):
        return "format" if state.get("failure") else "generate"

    @staticmethod
    def _plan(state: State) -> str:
        rephrased = state.get("rephrasedQuery")
        plan = rephrased.get("rephrasedOutput") if isinstance(rephrased, dict) else None
        return plan or state["inputQuery"]

    @staticmethod
    def _parse(text: str) -> dict:
        """The state update for a model reply: a valid request, or a stage-model failure."""
        try:
            request = _readRequest(text)
        except ValidationError as error:
            return {"rawText": text, "chartRequest": None, "chart": None, "failure": {"stage": "model", "message": "", "error": _modelFailure(error)}}
        valid = request.model_dump(exclude_none=True)
        return {"rawText": text, "chartRequest": valid, "lastRequest": valid, "chart": None, "failure": None}

    @staticmethod
    def _withMessage(update: dict) -> dict:
        failure = update["failure"]
        if failure is not None:
            failure["message"] = failure["error"].message
        return update

    def _generate(self, state: State, config: RunnableConfig):
        text = self.generatorChain.invoke(
            {"question": state["inputQuery"], "plan": self._plan(state), "grounding": state["grounding"]}, config
        )
        return self._withMessage(_withPlanSliceType(self._parse(str(text)), self._plan(state)))

    def _repair(self, state: State, config: RunnableConfig):
        previous = json.dumps(state["lastRequest"], ensure_ascii=False) if state.get("lastRequest") else state.get("rawText", "")
        text = self.repairChain.invoke(
            {
                "question": state["inputQuery"],
                "plan": self._plan(state),
                "grounding": state["grounding"],
                "previous_request": previous,
                "error": state["failure"]["message"],
            },
            config,
        )
        update = self._withMessage(_withPlanSliceType(self._parse(str(text)), self._plan(state)))
        update["debugAttempts"] = state.get("debugAttempts", 0) + 1
        return update

    def _evaluate(self, state: State, config: RunnableConfig):
        request = ChartRequest.model_validate(state["chartRequest"])
        try:
            chart = renderChartRequest(
                self.replManager,
                state["projectId"],
                request,
                state["tables"],
                waitForSlot=not _isShadow(config),
            )
        except ChartFailure as error:
            return {"chart": None, "failure": {"stage": error.stage, "message": error.message, "error": error}}
        except SandboxBusyError as error:
            # A full sandbox says nothing about the request, so the model is not asked to repair it.
            return {"chart": None, "failure": {"stage": "busy", "message": BUSY_MESSAGE, "error": error}}
        except Exception as error:
            # The sandbox or the result reader broke. That says nothing about the request, so nothing is repaired,
            # and the user's answer and the log carry fixed text; the log keeps the exception's class.
            failure = ChartFailure("run", _UNEXPECTED_MESSAGE, logMessage=_UNEXPECTED_MESSAGE)
            failure.__cause__ = error
            return {"chart": None, "failure": {"stage": "run", "message": _UNEXPECTED_MESSAGE, "error": failure, "repairable": False}}
        return {"chart": chart, "failure": None}

    def _next(self, state: State):
        """After generate, evaluate or repair: run a fresh request, repair a failed one, probe an empty chart, or finish."""
        chart = state.get("chart")
        if chart is not None:
            # The parallel (report) workflow never asks, so it never probes.
            return "probe" if not self.parallel and chartHasNoData(chart) else "format"
        failure = state.get("failure")
        if failure is None:
            return "evaluate"
        if failure["stage"] in _TERMINAL_STAGES or failure.get("repairable") is False or state.get("debugAttempts", 0) >= MAX_REPAIRS:
            return "format"
        return "repair"

    def _probe(self, state: State, config: RunnableConfig):
        """A chart without data: ask which period to chart when the data of at least one date bound ends before that
        bound and, given the bounds that ended, the chart provably holds no project data. Anything else, including a
        probe that fails, keeps the chart (an empty update)."""
        sql = state["chartRequest"]["sql"]
        try:
            probes = findPeriodProbes(sql, state["tables"])
        except Exception as error:
            self._logProbeFailure(_ProbeFailed("parse", error), state, config)
            return {}
        if not probes:
            return {}
        try:
            answers = self._runProbes(probes, state, config)
        except _ProbeFailed as failure:
            self._logProbeFailure(failure, state, config)
            return {}
        ended = [(probe, lastValue) for probe, (endedBefore, lastValue) in zip(probes, answers) if endedBefore]
        try:
            dataFree = bool(ended) and isDataFree(sql, state["tables"], [probe.sql for probe, _ in ended])
        except Exception as error:
            self._logProbeFailure(_ProbeFailed("parse", error), state, config)
            return {}
        if not dataFree:
            return {}
        lastValues = [lastValue for _, lastValue in ended if lastValue is not None]
        if not all(isinstance(value, str) and _ISO_DATE.match(value) for value in lastValues):
            # Not a date the question can quote (a date column of an unusual type): a known limit, not a failure.
            self._logProbeRecord("undated", None, state, config, logger.info)
            return {}
        rephrased = state.get("rephrasedQuery")
        # The same shape as a rephraser doubt: format answers {"response": doubt} with no code and no output.
        doubt = periodQuestion(max(value[:10] for value in lastValues) if lastValues else None)
        return {"rephrasedQuery": {**(rephrased if isinstance(rephrased, dict) else {}), "doubt": doubt}, "chart": None}

    def _runProbes(self, probes: list[PeriodProbe], state: State, config: RunnableConfig) -> list[tuple[bool, Any]]:
        """(ended before, last value) per probe, from one sandbox run the way the chart ran."""
        try:
            request = ChartRequest(chartType="table", title=_PROBE_TITLE, sql=combinedProbeSql(probes), encoding={})
            chart = renderChartRequest(
                self.replManager,
                state["projectId"],
                request,
                state["tables"],
                waitForSlot=not _isShadow(config),
            )
        except ChartFailure as error:
            raise _ProbeFailed(error.stage, error) from error
        except SandboxBusyError as error:
            raise _ProbeFailed("busy", error) from error
        except Exception as error:
            raise _ProbeFailed("exception", error) from error
        rows = chart.get("data") if isinstance(chart, dict) else None
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}
        answers = []
        for number in range(1, len(probes) + 1):
            endedBefore = row.get(f"ended_before_{number}")
            if not isinstance(endedBefore, bool):
                raise _ProbeFailed("result")
            answers.append((endedBefore, row.get(f"last_value_{number}")))
        return answers

    @classmethod
    def _logProbeFailure(cls, failure: _ProbeFailed, state: State, config: RunnableConfig | None) -> None:
        cls._logProbeRecord(failure.stage, failure.error, state, config, logger.warning)

    @staticmethod
    def _logProbeRecord(stage: str, error: BaseException | None, state: State, config: RunnableConfig | None, log) -> None:
        """One line with the stage and ids only: no SQL, table names or values."""
        record = {
            "stage": stage,
            "errorClass": type(error.__cause__ or error).__name__ if error is not None else None,
            "projectId": state["projectId"],
            "shadow": _isShadow(config),
            "traceId": traceIdOf({"callbacks": _callbackHandlers(config)}),
        }
        log("chart_period_probe " + json.dumps(record, sort_keys=True))

    def _format(self, state: State, config: RunnableConfig):
        return {**self._formatted(state, config), "debugAttempts": state.get("debugAttempts", 0)}

    def _formatted(self, state: State, config: RunnableConfig):
        chart = state.get("chart")
        failure = state.get("failure")
        if chart is not None:
            request = ChartRequest.model_validate(state["chartRequest"])
            return {
                "finalOutput": chart,
                "codeOutput": json.dumps(chart, default=serializer),
                "generatedCode": encodeGeneratedCode(request),
            }
        if failure is None:
            doubt = self._doubtOf(state) or "Query could not be processed."
            return {"finalOutput": {"response": doubt}}

        shadow = _isShadow(config)
        if shadow and _endedBusy(failure):
            # A shadow that found the sandbox busy is dropped, not failed: only its chart_shadow line says so.
            outcome = _shadowOutcome(config)
            if outcome is not None:
                outcome["stage"] = "busy"
            return {"finalOutput": {"error": BUSY_MESSAGE}, "codeOutput": json.dumps({"error": BUSY_MESSAGE}), "generatedCode": self._generatedCode(state)}
        outcome = _shadowOutcome(config)
        if outcome is not None:
            outcome["stage"] = failure["stage"]
        lastRequest = state.get("lastRequest")
        logChartFailure(
            stage=failure["stage"],
            error=failure["error"],
            traceId=traceIdOf({"callbacks": _callbackHandlers(config)}),
            projectId=state["projectId"],
            chartType=lastRequest["chartType"] if lastRequest else None,
            repairs=state.get("debugAttempts", 0),
            shadow=shadow,
            tableNames=state.get("tables"),
        )
        if failure["stage"] == "busy":
            return {"finalOutput": {"error": BUSY_MESSAGE}, "codeOutput": json.dumps({"error": BUSY_MESSAGE}), "generatedCode": self._generatedCode(state)}
        return {
            "finalOutput": {"error": failure["message"]},
            "codeOutput": failure["message"],
            "generatedCode": self._generatedCode(state),
        }

    @staticmethod
    def _generatedCode(state: State) -> str:
        """The last valid request the model produced; its latest raw text only when it never produced one (the data endpoint runs that as failing code)."""
        if state.get("lastRequest"):
            return encodeGeneratedCode(ChartRequest.model_validate(state["lastRequest"]))
        return state.get("rawText", "")

    def createWorkflow(self):
        workflow = StateGraph(State, output_schema=OutputState)
        workflow.add_node("rephrase", self._rephrase)
        workflow.add_node("ground", self._ground)
        workflow.add_node("generate", self._generate)
        workflow.add_node("evaluate", self._evaluate)
        workflow.add_node("repair", self._repair)
        workflow.add_node("format", self._format)
        workflow.add_edge(START, "rephrase")
        workflow.add_conditional_edges("rephrase", self._afterRephrase, {"ground": "ground", "format": "format"})
        workflow.add_conditional_edges("ground", self._afterGround, {"generate": "generate", "format": "format"})
        route = {"evaluate": "evaluate", "repair": "repair", "format": "format"}
        if not self.parallel:
            workflow.add_node("probe", self._probe)
            workflow.add_edge("probe", "format")
            route["probe"] = "probe"
        for node in ("generate", "evaluate", "repair"):
            workflow.add_conditional_edges(node, self._next, route)
        workflow.add_edge("format", END)
        return workflow.compile()


def buildSqlChartWorkflow(parallel: bool = False):
    """Builds the SQL chart workflow. `parallel` picks the rephraser that never raises a doubt."""
    return SqlChartWorkflow(parallel=parallel).createWorkflow()
