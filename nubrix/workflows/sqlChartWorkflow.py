"""
sqlChartWorkflow.py

The SQL chart workflow: rephrase the question, describe the project's tables, have the model write a chart request
(one DuckDB SELECT plus an encoding), check and run it in the sandbox, build the chart, and repair the request
with the exact error (at most twice) when a step fails.

The response keeps the keys of the Python chart workflows. Everything else stays in internal state.

    START -> rephrase -> ground -> generate -> (evaluate | repair | format)
                  \\-> format (doubt)
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
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError
from utils.tableSchemas import tableSchemas

MAX_REPAIRS = 2
RUN_TIMEOUT_SECONDS = 35
_MODEL_LOG_MESSAGE = "The model reply is not a valid chart request."
_TERMINAL_STAGES = ("timeout", "busy")


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
    chart: dict | None
    # {stage, message, error}: `error` is the exception behind the failure, kept for the failure log.
    failure: dict | None
    grounding: str
    tables: list[str]
    rawText: str


def _activeTableNames(metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    return [name for name, entry in metadata.items() if not (isinstance(entry, dict) and entry.get("isActive") is False)]


def _cleanModelText(text: str) -> str:
    """Drops <think> blocks and a surrounding code fence."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).replace("<think>", "").replace("</think>", "")
    text = text.strip()
    text = re.sub(r"^```[A-Za-z]*[ \t]*\n?", "", text)
    text = re.sub(r"\n?[ \t]*```\s*$", "", text)
    return text.strip()


def _modelFailure(error: ValidationError) -> ChartFailure:
    """A stage-model failure whose message names the problems without echoing the reply."""
    problems = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        where = ".".join(str(part) for part in item["loc"])
        problems.append(f"{where}: {item['msg']}" if where else item["msg"])
    failure = ChartFailure("model", "The reply is not a valid chart request: " + "; ".join(problems), logMessage=_MODEL_LOG_MESSAGE)
    failure.__cause__ = error
    return failure


def _callbackHandlers(config: RunnableConfig | None) -> list:
    """The handlers of a config's callbacks, which LangGraph hands to a node as a manager rather than a list."""
    callbacks = (config or {}).get("callbacks")
    handlers = getattr(callbacks, "handlers", callbacks)
    return list(handlers or [])


def _isShadow(config: RunnableConfig | None) -> bool:
    return ((config or {}).get("configurable") or {}).get("shadow") is True


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

    def _ground(self, state: State):
        tables = _activeTableNames(state["metadata"])
        try:
            schemas = tableSchemas(state["projectId"], tables)
        except Exception as error:
            # Types come back as UNKNOWN; the model can still write the query from names and samples.
            logger.warning(f"Table schemas unavailable for grounding: {type(error).__name__}")
            schemas = {}
        return {"tables": tables, "grounding": groundingText(state["metadata"], schemas)}

    @staticmethod
    def _plan(state: State) -> str:
        rephrased = state.get("rephrasedQuery")
        plan = rephrased.get("rephrasedOutput") if isinstance(rephrased, dict) else None
        return plan or state["inputQuery"]

    @staticmethod
    def _parse(text: str) -> dict:
        """The state update for a model reply: a valid request, or a stage-model failure."""
        try:
            request = ChartRequest.model_validate_json(_cleanModelText(text))
        except ValidationError as error:
            return {"rawText": text, "chartRequest": None, "chart": None, "failure": {"stage": "model", "message": "", "error": _modelFailure(error)}}
        return {"rawText": text, "chartRequest": request.model_dump(exclude_none=True), "chart": None, "failure": None}

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
        return self._withMessage(self._parse(str(text)))

    def _repair(self, state: State, config: RunnableConfig):
        previous = json.dumps(state["chartRequest"], ensure_ascii=False) if state.get("chartRequest") else state.get("rawText", "")
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
        update = self._withMessage(self._parse(str(text)))
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
        return {"chart": chart, "failure": None}

    def _next(self, state: State):
        """After generate, evaluate or repair: run a fresh request, repair a failed one, or finish."""
        if state.get("chart") is not None:
            return "format"
        failure = state.get("failure")
        if failure is None:
            return "evaluate"
        if failure["stage"] in _TERMINAL_STAGES or state.get("debugAttempts", 0) >= MAX_REPAIRS:
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

        chartRequest = state.get("chartRequest")
        logChartFailure(
            stage=failure["stage"],
            error=failure["error"],
            traceId=traceIdOf({"callbacks": _callbackHandlers(config)}),
            projectId=state["projectId"],
            chartType=chartRequest["chartType"] if chartRequest else None,
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
        """The request when the model produced a valid one, else its raw text, which the data endpoint treats as failing code."""
        if state.get("chartRequest"):
            return encodeGeneratedCode(ChartRequest.model_validate(state["chartRequest"]))
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
        workflow.add_edge("ground", "generate")
        route = {"evaluate": "evaluate", "repair": "repair", "format": "format"}
        for node in ("generate", "evaluate", "repair"):
            workflow.add_conditional_edges(node, self._next, route)
        workflow.add_edge("format", END)
        return workflow.compile()


def buildSqlChartWorkflow(parallel: bool = False):
    """Builds the SQL chart workflow. `parallel` picks the rephraser that never raises a doubt."""
    return SqlChartWorkflow(parallel=parallel).createWorkflow()
