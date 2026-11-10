"""
sqlChartWorkflow.py

The SQL chart workflow: rephrase the question, describe the project's tables, have the model write a chart request
(one DuckDB SELECT plus an encoding), check and run it in the sandbox, build the chart, and repair the request
with the exact error (at most twice) when a step fails.

The response keeps the keys of the Python chart workflows. Everything else stays in internal state.

    START -> rephrase -> ground -> generate -> (evaluate | repair | format)
                  \\-> format (doubt)
    ground -> format (a busy sandbox while reading table schemas)
    evaluate -> (format | repair)
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


def _activeTableNames(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    return [name for name, entry in metadata.items() if not (isinstance(entry, dict) and entry.get("isActive") is False)]


def _readRequest(text: str) -> ChartRequest:
    """The chart request in a model reply: the first JSON object that validates.

    The reply may hold reasoning text before the JSON (the chain only strips the think tags) or a code fence around it,
    so each '{' is tried as the start of an object. When none validates, the error raised is the one for the object
    most likely to be the request: the last one with a `chartType` key, else the longest. When a '{' that did not
    decode starts a longer stretch than that object, with no `chartType` object after it, the reply was cut off or is
    not JSON, and the error is the invalid-JSON one for that stretch.
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
    withType = [item for item in decoded if "chartType" in item[2]]
    best = withType[-1] if withType else max(decoded, key=lambda item: item[1] - item[0], default=None)
    span = best[1] - best[0] if best else 0
    after = best[0] if withType else -1
    for cut in undecoded:
        if len(text) - cut > span and cut > after:
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
        tables = _activeTableNames(state["metadata"])
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
        """After generate, evaluate or repair: run a fresh request, repair a failed one, or finish."""
        if state.get("chart") is not None:
            return "format"
        failure = state.get("failure")
        if failure is None:
            return "evaluate"
        if failure["stage"] in _TERMINAL_STAGES or failure.get("repairable") is False or state.get("debugAttempts", 0) >= MAX_REPAIRS:
            return "format"
        return "repair"

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

        lastRequest = state.get("lastRequest")
        logChartFailure(
            stage=failure["stage"],
            error=failure["error"],
            traceId=traceIdOf({"callbacks": _callbackHandlers(config)}),
            projectId=state["projectId"],
            chartType=lastRequest["chartType"] if lastRequest else None,
            repairs=state.get("debugAttempts", 0),
            shadow=_isShadow(config),
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
        for node in ("generate", "evaluate", "repair"):
            workflow.add_conditional_edges(node, self._next, route)
        workflow.add_edge("format", END)
        return workflow.compile()


def buildSqlChartWorkflow(parallel: bool = False):
    """Builds the SQL chart workflow. `parallel` picks the rephraser that never raises a doubt."""
    return SqlChartWorkflow(parallel=parallel).createWorkflow()
