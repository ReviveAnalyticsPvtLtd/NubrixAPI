"""
parallelReportingToolWorkflow.py

This module defines the ParallelReportingToolWorkflow class, which orchestrates a multi-agent workflow
specifically optimized for parallel chart generation without query interruptions/doubts.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["buildParallelReportingWorkflow"]


from nubrix.components.queryRephraser import ParallelQueryRephaser
from nubrix.components.llmChainFactory import buildLlmChain
from langgraph.graph import StateGraph, START, END
from utils.codeExecutor import REPLManager, _remove_code_fences
from typing_extensions import TypedDict
from utils.logger import logger
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError
from utils.mapPointLabels import ensureScatterMapPointLabels
import json
import os

class State(TypedDict):
    """
    State defines the structure for workflow state, holding all intermediate and data for parallel execution.
    """
    projectId: str
    inputQuery: str
    metadata: str
    rephrasedQuery: str
    generatedCode: str
    codeOutput: str
    finalOutput: dict
    debugAttempts: int

class ParallelReportingToolWorkflow:
    """
    ParallelReportingToolWorkflow manages parallel query generation without doubt interruptions.
    """
    MAX_DEBUG_ATTEMPTS = 2

    def __init__(self):
        """Initializes the ParallelReportingToolWorkflow with its own chain instances."""
        logger.info("Initializing parallel agentic reporting workflow.")
        self.queryRephraseChain = ParallelQueryRephaser().getQueryRephraserChain()
        self.codeGeneratorChain = buildLlmChain("CODEGENERATOR", "codeGeneratorAgentPrompt")
        self.codeDebuggerChain = buildLlmChain("CODEDEBUGGER", "codeDebuggerAgentPrompt")
        self.replManager = REPLManager(timeoutSeconds=35)

    def _rephraseQuery(self, state: State):
        """
        Rephrases the query using the parallel rephraser (no doubt field in output).
        Falls back to the raw input query if anything goes wrong.
        """
        try:
            response = self.queryRephraseChain.invoke({
                "query": state["inputQuery"],
                "metadata": state["metadata"]
            })
            if not response.get("rephrasedOutput"):
                response["rephrasedOutput"] = state["inputQuery"]
        except Exception as e:
            logger.warning(f"Query rephrase failed: {e}. Falling back to input query.")
            response = {
                "rephrasedOutput": state["inputQuery"],
            }
        return {
            "rephrasedQuery": response
        }

    def _generateCode(self, state: State):
        """Generates code for the rephrased query, injecting projectId into all fetch calls."""
        response = self.codeGeneratorChain.invoke({
            "query": state["rephrasedQuery"],
            "metadata": state["metadata"]
        })
        pid = state["projectId"]
        response = self._injectProjectId(response, pid)
        response = self._route_large_tables_to_scan(response, pid)
        # Normalize every json.dumps() call to have exactly one default=serializer
        # (LLM sometimes emits default=str or duplicate default= kwargs → SyntaxError).
        import re as _re
        def _fix_dumps(m):
            inner = m.group(1)
            inner = _re.sub(r',?\s*default=\w+\s*,?', ',', inner)
            inner = _re.sub(r',\s*,', ',', inner)
            inner = _re.sub(r'^\s*,\s*', '', inner)
            inner = _re.sub(r',\s*$', '', inner).rstrip()
            if inner and not inner.endswith(','):
                inner += ','
            return f'json.dumps({inner} default=serializer)'
        response = _re.sub(r'json\.dumps\(([^)]*)\)', _fix_dumps, response)
        return {
            "generatedCode": response,
        }

    @staticmethod
    def _route_large_tables_to_scan(code: str, projectId: str) -> str:
        """Rewrite eager ``fetch_data_pl("<pid>", "<table>")`` -> lazy ``scan_data(...)``.
        See ReportingToolWorkflow for full docstring.
        """
        import re
        from utils.initMethods import classify_table_size
        try:
            threshold = int(os.getenv("LAZY_FETCH_ROW_THRESHOLD", "1000000"))
        except Exception:
            threshold = 100000
        if threshold <= 0:
            return code
        def _maybe_swap(match: re.Match) -> str:
            table = match.group(1)
            size = classify_table_size(projectId, table)
            rows = size.get("rows_estimate")
            if isinstance(rows, int) and rows >= threshold:
                return f'scan_data("{projectId}", "{table}"'
            return match.group(0)
        return re.sub(r'fetch_data_pl\("' + re.escape(projectId) + r'",\s*"([^"]+)"', _maybe_swap, code)

    @staticmethod
    def _injectProjectId(code: str, projectId: str) -> str:
        import re
        # Strip double-injections first (e.g. fn("pid", "pid", "table")) by deduplicating projectIds in arguments
        cleaned_code = re.sub(rf'\b(fetch_data|fetch_data_pl|scan_data)\(\s*["\']{re.escape(projectId)}["\'],\s*["\']{re.escape(projectId)}["\'],\s*', rf'\1("{projectId}", ', code)
        
        # Inject to single-argument calls
        pattern = r'\b(fetch_data|fetch_data_pl|scan_data)\(\s*(["\'])([^"\'\s]+)\2'
        def replace_fn(match):
            fn_name = match.group(1)
            quote = match.group(2)
            first_arg = match.group(3)
            if first_arg == projectId:
                return match.group(0)
            return f'{fn_name}("{projectId}", {quote}{first_arg}{quote}'
        return re.sub(pattern, replace_fn, cleaned_code)

    def _runInPythonSandbox(self, state: State):
        """
        Executes the generated code in Python sandbox.
        """
        code = _remove_code_fences(state["generatedCode"])
        try:
            response = self.replManager.run(code, projectId=state["projectId"])
        except SandboxBusyError:
            # A full sandbox is not a code failure: skip the debugger and the rerun, and show the retry copy as this chart's error.
            response = json.dumps({"error": BUSY_MESSAGE})
        return {
            "codeOutput": response
        }

    def _outputEvaluationRouter(self, state: State):
        """
        Determines if the code output is valid JSON.
        """
        try:
            _ = json.loads(state["codeOutput"])
            return "pass"
        except (json.JSONDecodeError, TypeError):
            return "fail"

    def _codeDebugger(self, state: State):
        """
        Invokes the code debugger chain.
        """
        response = self.codeDebuggerChain.invoke({
            "user_query": state["rephrasedQuery"],
            "metadata_context": state["metadata"],
            "code_with_errors": state["generatedCode"],
            "error_message": state["codeOutput"]
        })
        pid = state["projectId"]
        response = self._injectProjectId(response, pid)
        response = self._route_large_tables_to_scan(response, pid)
        return {
            "generatedCode": response,
            "debugAttempts": state.get("debugAttempts", 0) + 1,
        }

    def _debugRouter(self, state: State):
        """After a debug attempt, route to format if output is valid JSON, else retry up to budget."""
        try:
            json.loads(state.get("codeOutput", ""))
            return "formatJsonResponse"
        except (json.JSONDecodeError, TypeError):
            pass
        if state.get("debugAttempts", 0) >= self.MAX_DEBUG_ATTEMPTS:
            return "formatJsonResponse"
        return "debugger"

    def _formatJsonResponse(self, state: State):
        """
        Formats the final output as a JSON response.
        """
        try:
            response = json.loads(state["codeOutput"])
        except Exception as e:
            response = {"error": f"Endpoint says: {e}"}
        ensureScatterMapPointLabels(response)
        return {
            "finalOutput": response
        }

    def createWorkflow(self):
        """
        Constructs and compiles the parallel reporting tool workflow.
        """
        logger.info("Compiling parallel reporting workflow.")
        workflow = StateGraph(State)
        workflow.add_node("rephraseQuery", self._rephraseQuery)
        workflow.add_node("generateCode", self._generateCode)
        workflow.add_node("runInPythonSandbox", self._runInPythonSandbox)
        workflow.add_node("debugger", self._codeDebugger)
        workflow.add_node("debuggerPythonSandbox", self._runInPythonSandbox)
        workflow.add_node("formatJsonResponse", self._formatJsonResponse)

        # Connect directly to generateCode (no conditional routing/interrupt on doubt)
        workflow.add_edge(START, "rephraseQuery")
        workflow.add_edge("rephraseQuery", "generateCode")
        workflow.add_edge("generateCode", "runInPythonSandbox")
        workflow.add_conditional_edges("runInPythonSandbox", self._outputEvaluationRouter, {"pass": "formatJsonResponse", "fail": "debugger"})
        workflow.add_edge("debugger", "debuggerPythonSandbox")
        workflow.add_conditional_edges("debuggerPythonSandbox", self._debugRouter, {"debugger": "debugger", "formatJsonResponse": "formatJsonResponse"})
        workflow.add_edge("formatJsonResponse", END)

        workflow = workflow.compile()
        logger.info("Parallel reporting workflow compilation successful.")
        return workflow


def buildParallelReportingWorkflow():
    """
    Builds the parallel reporting workflow.
    """
    graph = ParallelReportingToolWorkflow()
    return graph.createWorkflow()