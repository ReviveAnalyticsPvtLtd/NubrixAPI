"""getData re-runs saved widgets in their own form, and a failed re-run never shows or saves wrong numbers."""

import copy
import json
from unittest.mock import MagicMock

import pytest

from api.models import GetData
from nubrix.components.chartEngine import ChartFailure
from nubrix.components.chartRequest import ChartRequest, encodeGeneratedCode
from utils.exceptionHandler import CustomException
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError

PYTHON = "df = fetch_data('p1', 'orders')\nprint(1)"
FILTERS = [{"region": "north"}]


def _request() -> str:
    return encodeGeneratedCode(ChartRequest.model_validate({
        "chartType": "bar", "title": "t", "sql": "select 1 as a", "encoding": {"x": "a", "y": ["a"]},
    }))


def _widget(widgetId, code=PYTHON, chartType="card", **extra) -> dict:
    widget = {"id": widgetId, "chartType": chartType, "title": "T", "label": "Saved", "xLabels": "X", "yLabels": "Y",
              "data": "old", "layout": {"x": 1}, "generatedCode": code}
    widget.update(extra)
    return widget


def _setup(monkeypatch, widgets, run=None, render=None):
    from api.services import dashboardService as module

    store = {"pg": {"name": "n", "widgets": copy.deepcopy(widgets)}}
    files = {"dashboardConfig.json": store, "metadata.json": {"orders": {}, "old": {"isActive": False}}}
    monkeypatch.setattr(module, "readJson", lambda projectId, name: files[name])
    manager = MagicMock()
    manager.run.side_effect = run or (lambda code, projectId=None, filters=None: json.dumps({"data": "new"}))
    monkeypatch.setattr(module, "replManager", manager)
    renderer = MagicMock(side_effect=render or (lambda *a, **k: {"chartType": "bar", "title": "t", "data": "new"}))
    monkeypatch.setattr(module, "renderChartRequest", renderer)
    service = module.DashboardService.__new__(module.DashboardService)
    service.client = MagicMock()
    return service, manager, renderer


def _uploaded(service) -> dict:
    upload = service.client.storage.from_.return_value.upload
    return json.loads(upload.call_args.kwargs["file"].decode("utf-8"))


def test_filterRunsPythonWidgetWithFiltersAndKeepsItsCode(monkeypatch):
    fenced = "```python\n" + PYTHON + "\n```"
    service, manager, _ = _setup(monkeypatch, [_widget("w1", fenced)])

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert manager.run.call_args.kwargs["filters"] == FILTERS
    assert page["widgets"][0]["generatedCode"] == fenced
    assert page["widgets"][0]["data"] == "new"
    assert page["widgets"][0]["layout"] == {"x": 1}
    service.client.storage.from_.assert_not_called()


def test_filterRunsChartRequestWidgetLeniently(monkeypatch):
    code = _request()
    service, manager, renderer = _setup(monkeypatch, [_widget("w1", code, "bar")])

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    args, kwargs = renderer.call_args
    assert args[1] == "p1" and args[3] == frozenset({"orders"})
    assert kwargs["lenient"] is True and kwargs["filters"] == FILTERS
    manager.run.assert_not_called()
    widget = page["widgets"][0]
    assert widget["data"] == "new" and widget["generatedCode"] == code
    assert widget["id"] == "w1" and widget["layout"] == {"x": 1}


@pytest.mark.parametrize("chartType, expectedData", [
    ("bar", {"labels": [], "datasets": []}), ("table", []), ("pivot", {}), ("geoMap", None), ("card", 0),
])
@pytest.mark.parametrize("form", ["python", "request"])
def test_failedFilterRunGivesEmptyChartOfItsType(monkeypatch, chartType, expectedData, form):
    def failRender(*args, **kwargs):
        raise ChartFailure("run", "boom")

    code = PYTHON if form == "python" else _request()
    saved = _widget("w1", code, chartType, data={"datasets": [{"data": [1]}]} if chartType == "bar" else "old")
    service, _, _ = _setup(monkeypatch, [saved],
                           run=lambda code, projectId=None, filters=None: "Traceback (most recent call last)",
                           render=failRender)

    widget = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))["widgets"][0]

    assert widget["chartType"] == chartType and widget["id"] == "w1"
    assert widget["layout"] == {"x": 1} and widget["generatedCode"] == code
    if chartType == "geoMap":
        assert widget["map"]["data"]["points"] == []
    else:
        assert widget["data"] == expectedData
    if chartType == "card":
        assert widget["label"] == "Saved"


def test_failedFilterRunOfCardWithoutSavedLabelGetsEmptyLabel(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("w1", chartType="card", label="")],
                           run=lambda code, projectId=None, filters=None: "nope")

    widget = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))["widgets"][0]

    assert widget["label"] == "" and widget["data"] == 0


def test_refreshKeepsFailedWidgetsAndSavesOnlySucceeded(monkeypatch):
    def run(code, projectId=None, filters=None):
        return json.dumps({"data": "newA"}) if "marker_a" in code else "Execution timed out after 35 seconds."

    widgets = [_widget("A", PYTHON + "\n# marker_a"), _widget("B", PYTHON + "\n# marker_b")]
    service, manager, _ = _setup(monkeypatch, widgets, run=run)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    saved = _uploaded(service)["pg"]["widgets"]
    assert [w["data"] for w in saved] == ["newA", "old"]
    assert [w["data"] for w in page["widgets"]] == ["newA", "old"]
    assert manager.run.call_args.kwargs["filters"] is None
    assert [w["generatedCode"] for w in saved] == [w["generatedCode"] for w in widgets]


def test_refreshWithNoSuccessUploadsNothing(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("A"), _widget("B")],
                           run=lambda code, projectId=None, filters=None: "Traceback")

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.assert_not_called()
    assert [w["data"] for w in page["widgets"]] == ["old", "old"]


@pytest.mark.parametrize("filters, refresh", [(FILTERS, False), (None, True)])
@pytest.mark.parametrize("form", ["python", "request"])
def test_busyGives503AndSavesNothing(monkeypatch, filters, refresh, form):
    def busy(*args, **kwargs):
        raise SandboxBusyError()

    code = PYTHON if form == "python" else _request()
    service, _, _ = _setup(monkeypatch, [_widget("A", code, "bar")], run=busy, render=busy)

    with pytest.raises(CustomException) as caught:
        service.getData(GetData(projectId="p1", page="pg", filters=filters, refresh=refresh))

    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_MESSAGE)
    service.client.storage.from_.assert_not_called()


def test_filtersAndRefreshTogetherStayAnError(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("A")])

    with pytest.raises(CustomException):
        service.getData(GetData(projectId="p1", page="pg", filters=FILTERS, refresh=True))


@pytest.mark.parametrize("refresh", [False, True])
def test_widgetWithoutChartTypeIsLeftAlone(monkeypatch, refresh):
    failure = {"id": "F", "chartType": "card", "title": "Chart: q", "label": "Status", "xLabels": "Status",
               "yLabels": "Value", "data": "Error/No data", "layout": {},
               "generatedCode": "# Failed to generate code for: q\n# Error: x"}
    noType = {"id": "N", "title": "x", "data": "whatever", "layout": {}, "generatedCode": "boom ("}
    service, manager, renderer = _setup(monkeypatch, [failure, noType])

    page = service.getData(GetData(projectId="p1", page="pg", filters=None if refresh else FILTERS, refresh=refresh))

    assert page["widgets"] == [failure, noType]
    manager.run.assert_not_called()
    renderer.assert_not_called()
    service.client.storage.from_.assert_not_called()


@pytest.mark.parametrize("output", [
    '{"error": "x"}', "Traceback (most recent call last):\n  File ...", "Execution timed out after 35 seconds.", "[1, 2]",
])
def test_pythonErrorOrNonJsonOutputIsAFailure(monkeypatch, output):
    service, _, _ = _setup(monkeypatch, [_widget("A")], run=lambda code, projectId=None, filters=None: output)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.assert_not_called()
    assert page["widgets"][0]["data"] == "old"
