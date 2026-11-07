"""A busy sandbox is a retry-later answer, never a code failure: reports and dashboards must not debug or empty on it."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from utils.exceptionHandler import CustomException
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError

REPORT_MODULES = [
    ("nubrix.workflows.parallelReportingToolWorkflow", "ParallelReportingToolWorkflow", "ParallelQueryRephaser"),
    ("nubrix.workflows.reportingToolWorkflow", "ReportingToolWorkflow", "QueryRephaser"),
]


def _redisFake() -> MagicMock:
    redis = MagicMock()
    redis.zcard.return_value = 0
    return redis


@pytest.mark.parametrize("moduleName, className, rephraserName", REPORT_MODULES)
def test_busySandboxReachesTheChartWithoutDebuggingOrRerunning(monkeypatch, moduleName, className, rephraserName):
    import importlib

    from utils import codeExecutor

    module = importlib.import_module(moduleName)
    debugger = MagicMock()
    generator = MagicMock()
    generator.invoke.return_value = "print(1)"
    chains = {"codeGeneratorAgentPrompt": generator, "codeDebuggerAgentPrompt": debugger}
    monkeypatch.setattr(module, "buildLlmChain", lambda name, prompt: chains[prompt])
    rephraser = MagicMock()
    rephraser.return_value.getQueryRephraserChain.return_value.invoke.return_value = {"rephrasedOutput": "q"}
    monkeypatch.setattr(module, rephraserName, rephraser)
    redis = _redisFake()
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: redis)
    runSandbox = MagicMock(side_effect=SandboxBusyError())
    monkeypatch.setattr(codeExecutor, "runSandbox", runSandbox)

    workflow = getattr(module, className)().createWorkflow()
    state = workflow.invoke({"projectId": "p1", "inputQuery": "q", "metadata": "m", "debugAttempts": 0})

    assert debugger.invoke.call_count == 0
    assert runSandbox.call_count == 1
    assert state["finalOutput"] == {"error": BUSY_MESSAGE}
    redis.zrem.assert_called_once()


def _dashboard(monkeypatch, widgetRun):
    from api.services import dashboardService as module

    page = {"widgets": [{"id": "w1", "generatedCode": "x = 1", "chartType": "card", "data": 5},
                        {"id": "w2", "generatedCode": "x = 2", "chartType": "card", "data": 6}]}
    monkeypatch.setattr(module, "readJson", lambda projectId, name: {"pg": json.loads(json.dumps(page))})
    monkeypatch.setattr(module, "ProcessPoolExecutor", ThreadPoolExecutor)
    manager = MagicMock()
    manager.run.side_effect = widgetRun
    monkeypatch.setattr(module, "replManager", manager)
    service = module.DashboardService.__new__(module.DashboardService)
    service.client = MagicMock()
    return module, service


@pytest.mark.parametrize("filters, refresh", [([{"column": "a"}], False), (None, True)])
def test_dashboardWithABusyWidgetIsA503AndSavesNothing(monkeypatch, filters, refresh):
    from api.models import GetData

    def run(code, projectId=None):
        if "x = 2" in code:
            raise SandboxBusyError()
        return json.dumps({"data": 1})

    module, service = _dashboard(monkeypatch, run)
    monkeypatch.setattr(module, "updateProjectModifiedAt", MagicMock())

    with pytest.raises(CustomException) as caught:
        service.getData(GetData(projectId="p1", page="pg", filters=filters, refresh=refresh))

    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_MESSAGE)
    service.client.storage.from_.assert_not_called()


def test_dashboardRefreshStillSavesWhenNothingIsBusy(monkeypatch):
    from api.models import GetData

    module, service = _dashboard(monkeypatch, lambda code, projectId=None: json.dumps({"data": 1}))

    service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.return_value.upload.assert_called_once()
