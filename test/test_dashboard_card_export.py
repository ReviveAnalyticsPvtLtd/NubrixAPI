"""An exported card keeps the type its query gives, so it looks the same before and after a re-run.

The client exports a card's value as text (`String(value)`), so a number and a text value that looks like one
arrive the same way. Export runs the card once to learn which it is; when it can't, the text is kept.
"""

import asyncio
import copy
import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.models import ExportToDashboard
from nubrix.components.chartEngine import ChartFailure
from nubrix.components.chartRequest import GENERATED_CODE_MARKER, ChartRequest, encodeGeneratedCode
from utils.sandboxSlots import SandboxBusyError

PYTHON = "df = fetch_data('p1', 'orders')\nprint(1)"
METADATA = {"orders": {}, "old": {"isActive": False}}


def _request(chartType="card") -> str:
    encoding = {"value": "n"} if chartType == "card" else {"x": "a", "y": ["a"]}
    return encodeGeneratedCode(ChartRequest.model_validate({
        "chartType": chartType, "title": "t", "sql": "select 1 as n", "encoding": encoding,
    }))


def _details(data, code=None, chartType="card") -> ExportToDashboard:
    return ExportToDashboard(projectId="p1", page="pg", chartType=chartType, title="Total orders", label="Orders",
                             data=data, layout={"x": 0}, generatedCode=_request(chartType) if code is None else code)


def _setup(monkeypatch, value=None, run=None, render=None, metadata=None):
    """A service whose card run gives `value`; `events` records the runs and the dashboard reads in order."""
    from api.services import dashboardService as module

    events: list[str] = []
    files = {"dashboardConfig.json": {"pg": {"name": "n", "widgets": []}},
             "metadata.json": METADATA if metadata is None else metadata}

    def readJson(projectId, name):
        events.append(f"read {name}")
        return copy.deepcopy(files[name])

    def defaultRun(code, projectId=None, filters=None, waitForSlot=True):
        events.append("run python")
        return json.dumps({"chartType": "card", "title": "t", "label": "l", "data": value})

    def defaultRender(*args, **kwargs):
        events.append("run request")
        return {"chartType": "card", "title": "t", "label": "l", "data": value}

    monkeypatch.setattr(module, "readJson", readJson)
    monkeypatch.setattr(module, "updateProjectModifiedAt", lambda *_args: None)
    manager = MagicMock()
    manager.run.side_effect = run or defaultRun
    monkeypatch.setattr(module, "cardCheckManager", manager)
    # The dashboard re-runs' own manager is never used by an export.
    monkeypatch.setattr(module, "replManager", MagicMock(side_effect=AssertionError, run=MagicMock(side_effect=AssertionError)))
    renderer = MagicMock(side_effect=render or defaultRender)
    monkeypatch.setattr(module, "renderChartRequest", renderer)
    service = module.DashboardService.__new__(module.DashboardService)
    service.client = MagicMock()
    return service, manager, renderer, events


def _saved(service) -> dict:
    upload = service.client.storage.from_.return_value.upload
    return json.loads(upload.call_args.kwargs["file"].decode("utf-8"))["pg"]["widgets"][-1]


@pytest.mark.parametrize("text, value", [("48213", 48213), ("-3", -3), ("0", 0), ("12.5", 12.5), ("0.1", 0.1),
                                         ("0.000001", 0.000001), ("1e+21", 1e21), ("1.5e-7", 1.5e-7),
                                         ("-1.5e+300", -1.5e300), ("123456789012345680000", 1.2345678901234568e20)])
def test_numberCardIsSavedAsTheNumberItsQueryGives(monkeypatch, text, value):
    service, manager, renderer, _ = _setup(monkeypatch, value)

    widgetId = service.exportToDashboard(_details(text))

    saved = _saved(service)
    assert saved["data"] == value and type(saved["data"]) is type(value)
    assert saved["id"] == widgetId
    args, kwargs = renderer.call_args
    assert args[0] is manager and args[1] == "p1" and args[3] == ("orders", "old")
    assert kwargs["lenient"] is True and kwargs.get("filters") is None and kwargs["waitForSlot"] is False
    manager.run.assert_not_called()


def test_pythonCardIsRunWithoutFiltersAndWithoutWaiting(monkeypatch):
    service, manager, renderer, _ = _setup(monkeypatch, 48213)

    service.exportToDashboard(_details("48213", PYTHON))

    assert _saved(service)["data"] == 48213
    assert manager.run.call_args.kwargs["filters"] is None
    assert manager.run.call_args.kwargs["waitForSlot"] is False
    renderer.assert_not_called()


def test_theCardCheckGetsThePanelsRunTime():
    """A card that ran in the Panel (35 s a run in the reporting workflows) fits; nothing runs for 300 s."""
    from api.services import dashboardService as module

    assert module.cardCheckManager.timeoutSeconds == 35


@pytest.mark.parametrize("form", ["python", "request"])
@pytest.mark.parametrize("value", ["2016", "9007199254740993", "0"])
def test_textCardThatLooksLikeANumberStaysText(monkeypatch, form, value):
    service, _, _, _ = _setup(monkeypatch, value)

    service.exportToDashboard(_details(value, PYTHON if form == "python" else None))

    assert _saved(service)["data"] == value


@pytest.mark.parametrize("value", [48214, 48213.5, True, None, [48213], {"n": 48213}, 10 ** 400])
def test_aRunThatGivesAnythingButTheSameNumberKeepsTheText(monkeypatch, value):
    service, _, _, _ = _setup(monkeypatch, value)

    service.exportToDashboard(_details("48213"))

    assert _saved(service)["data"] == "48213"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("form", ["python", "request"])
def test_aRunThatGivesNoFiniteNumberKeepsTheText(monkeypatch, form, value):
    service, _, _, _ = _setup(monkeypatch, value)

    service.exportToDashboard(_details("1e+21", PYTHON if form == "python" else None))

    assert _saved(service)["data"] == "1e+21"


def test_oneIsNotSavedAsTrue(monkeypatch):
    service, _, _, _ = _setup(monkeypatch, True)

    service.exportToDashboard(_details("1"))

    assert _saved(service)["data"] == "1"


@pytest.mark.parametrize("output", ["Traceback (most recent call last)", "Execution timed out after 35 seconds.",
                                    "Concurrency limit reached: Too many running code execution tasks for project 'p1'."])
def test_failedPythonRunKeepsTheTextAndStillExports(monkeypatch, output):
    service, _, _, _ = _setup(monkeypatch, run=lambda code, projectId=None, filters=None, waitForSlot=True: output)

    service.exportToDashboard(_details("48213", PYTHON))

    assert _saved(service)["data"] == "48213"


@pytest.mark.parametrize("stage", ["check", "run", "build", "timeout"])
def test_failedRequestRunKeepsTheTextAndStillExports(monkeypatch, stage):
    def failRender(*args, **kwargs):
        raise ChartFailure(stage, "boom")

    service, _, _, _ = _setup(monkeypatch, render=failRender)

    service.exportToDashboard(_details("48213"))

    assert _saved(service)["data"] == "48213"


def test_requestOfAnUnsupportedVersionKeepsTheText(monkeypatch):
    service, manager, renderer, _ = _setup(monkeypatch, 48213)
    code = json.dumps({GENERATED_CODE_MARKER: 99, "chartType": "card"})

    service.exportToDashboard(_details("48213", code))

    assert _saved(service)["data"] == "48213"
    manager.run.assert_not_called()
    renderer.assert_not_called()


@pytest.mark.parametrize("form", ["python", "request"])
def test_busySandboxKeepsTheTextAndStillExports(monkeypatch, form):
    def busy(*args, **kwargs):
        raise SandboxBusyError()

    service, _, _, _ = _setup(monkeypatch, run=busy, render=busy)

    widgetId = service.exportToDashboard(_details("48213", PYTHON if form == "python" else None))

    assert widgetId and _saved(service)["data"] == "48213"


def test_busyWhileReadingTheTableListKeepsTheText(monkeypatch):
    from api.services import dashboardService as module

    service, _, renderer, _ = _setup(monkeypatch, 48213)

    def busy(self, projectId):
        raise SandboxBusyError()

    monkeypatch.setattr(module.DashboardService, "_rerunTables", busy)

    service.exportToDashboard(_details("48213"))

    assert _saved(service)["data"] == "48213"
    renderer.assert_not_called()


def test_unreadableTableListKeepsTheText(monkeypatch):
    from api.services import dashboardService as module

    service, _, renderer, _ = _setup(monkeypatch, 48213)
    monkeypatch.setattr(module.DashboardService, "_rerunTables", lambda self, projectId: None)

    service.exportToDashboard(_details("48213"))

    assert _saved(service)["data"] == "48213"
    renderer.assert_not_called()


@pytest.mark.parametrize("text", ["Brazil", "", "N/A", "NaN", "Infinity", "-Infinity", "1,234", " 12", "12 ", "+12",
                                  "0x1f", "1_000", "-0", "00123", "01", "1.50", "1.0", "12.", ".5", "1e+05", "1E+21",
                                  "1e21", "0e+5", "12e+21", "1.5e-07"])
def test_textThatStringNeverGivesForANumberIsNotRun(monkeypatch, text):
    """Only the text `String(number)` gives is run, so a number is saved only where the user saw that number."""
    service, manager, renderer, _ = _setup(monkeypatch, 12)

    service.exportToDashboard(_details(text))

    assert _saved(service)["data"] == text
    manager.run.assert_not_called()
    renderer.assert_not_called()


@pytest.mark.parametrize("code", ["", "   ", "# Failed to generate code: no table"])
def test_cardWithoutCodeToRunKeepsTheText(monkeypatch, code):
    service, manager, renderer, _ = _setup(monkeypatch, 48213)

    service.exportToDashboard(_details("48213", code))

    assert _saved(service)["data"] == "48213"
    manager.run.assert_not_called()
    renderer.assert_not_called()


@pytest.mark.parametrize("chartType, data", [("bar", {"labels": ["a"], "datasets": [{"data": [1]}]}),
                                             ("table", [{"a": "1"}]), ("pivot", {"a": {"b": 1}}), ("line", "5")])
def test_otherChartsAreNotRun(monkeypatch, chartType, data):
    service, manager, renderer, _ = _setup(monkeypatch, 1)

    service.exportToDashboard(_details(data, PYTHON, chartType=chartType))

    assert _saved(service)["data"] == data
    manager.run.assert_not_called()
    renderer.assert_not_called()


def test_theCardRunsBeforeTheDashboardIsRead(monkeypatch):
    """The run can take seconds; reading the dashboard after it keeps the read-to-save window as short as before."""
    service, _, _, events = _setup(monkeypatch, 48213)

    service.exportToDashboard(_details("48213"))

    assert events == ["read metadata.json", "run request", "read dashboardConfig.json"]


def test_exportedWidgetKeepsEverythingElseItWasSent(monkeypatch):
    service, _, _, _ = _setup(monkeypatch, 48213)
    details = _details("48213")

    widgetId = service.exportToDashboard(details)

    saved = _saved(service)
    assert saved == {"id": widgetId, "chartType": "card", "title": "Total orders", "label": "Orders", "xLabels": None,
                     "yLabels": None, "data": 48213, "map": None, "layout": {"x": 0},
                     "generatedCode": details.generatedCode}


def test_exportRouteRunsOffTheEventLoop(monkeypatch):
    from api.routers import dashboard

    seen = {}

    def exportToDashboard(details):
        try:
            asyncio.get_running_loop()
            seen["onLoop"] = True
        except RuntimeError:
            seen["onLoop"] = False
        return "w1"

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[dashboard.requireProductAccess] = lambda: object()
    monkeypatch.setattr(dashboard, "requireProjectOwner", lambda projectId, user: None)
    monkeypatch.setattr(dashboard.dashboardService, "exportToDashboard", exportToDashboard)

    response = TestClient(app).post("/exportToDashboard", json={
        "projectId": "p1", "page": "pg", "chartType": "card", "title": "t", "data": "1", "layout": {"x": 0},
        "generatedCode": "x"})

    assert response.status_code == 200 and response.json() == {"status": "SUCCESS", "widgetId": "w1"}
    assert seen["onLoop"] is False
