"""A prompt chart's generated code gets the same time as a dashboard widget's.

Each run receives its tables from the parent and starts a fresh child, which on
production takes about 5 to 7 seconds for a chart over a million-row table. At
7 seconds those charts failed now and then with "Execution timed out".
"""

import importlib
from unittest.mock import MagicMock

import pytest

CHART_WORKFLOWS = (
    ("nubrix.workflows.reportingToolWorkflow", "ReportingToolWorkflow", "QueryRephaser"),
    ("nubrix.workflows.parallelReportingToolWorkflow", "ParallelReportingToolWorkflow", "ParallelQueryRephaser"),
)
CHART = '{"chartType": "bar", "data": []}'


@pytest.mark.parametrize("moduleName, className, rephraserName", CHART_WORKFLOWS)
def test_chartRunGetsThirtyFiveSeconds(monkeypatch, moduleName, className, rephraserName):
    from utils import codeExecutor

    module = importlib.import_module(moduleName)
    generator = MagicMock()
    generator.invoke.return_value = "print(1)"
    chains = {"codeGeneratorAgentPrompt": generator, "codeDebuggerAgentPrompt": MagicMock()}
    monkeypatch.setattr(module, "buildLlmChain", lambda name, prompt: chains[prompt])
    rephraser = MagicMock()
    rephraser.return_value.getQueryRephraserChain.return_value.invoke.return_value = {"rephrasedOutput": "q"}
    monkeypatch.setattr(module, rephraserName, rephraser)
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    runSandbox = MagicMock(return_value=(CHART, "", 0, None, None, None))
    monkeypatch.setattr(codeExecutor, "runSandbox", runSandbox)

    state = getattr(module, className)().createWorkflow().invoke({"projectId": "p1", "inputQuery": "q", "metadata": "m", "debugAttempts": 0})

    timeoutSeconds = runSandbox.call_args.args[3]
    assert timeoutSeconds == 35
    assert state["finalOutput"]["chartType"] == "bar"
