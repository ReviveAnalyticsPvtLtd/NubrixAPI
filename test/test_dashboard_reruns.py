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


METADATA = {"orders": {}, "old": {"isActive": False}}


def _setup(monkeypatch, widgets, run=None, render=None, metadata=None):
    from api.services import dashboardService as module

    store = {"pg": {"name": "n", "widgets": copy.deepcopy(widgets)}}
    files = {"dashboardConfig.json": store, "metadata.json": METADATA if metadata is None else metadata}
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
    assert args[1] == "p1" and args[3] == ("orders", "old")
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


FAILURE_CARD = {"id": "F", "chartType": "card", "title": "Chart: q", "label": "Status", "xLabels": "Status",
                "yLabels": "Value", "data": "Error/No data", "layout": {},
                "generatedCode": "# Failed to generate code for: q\n# Error: x"}


def _noType(widgetId="N", code=PYTHON) -> dict:
    return {"id": widgetId, "title": "x", "data": "whatever", "layout": {"y": 2}, "generatedCode": code}


@pytest.mark.parametrize("refresh", [False, True])
def test_failureCardAndWidgetsWithoutCodeAreNotRun(monkeypatch, refresh):
    noCode = {"id": "C", "title": "x", "data": "whatever", "layout": {}}
    emptyCode = {"id": "E", "title": "x", "data": "whatever", "layout": {}, "generatedCode": "  "}
    service, manager, renderer = _setup(monkeypatch, [FAILURE_CARD, noCode, emptyCode])

    page = service.getData(GetData(projectId="p1", page="pg", filters=None if refresh else FILTERS, refresh=refresh))

    assert page["widgets"] == [FAILURE_CARD, noCode, emptyCode]
    manager.run.assert_not_called()
    renderer.assert_not_called()
    service.client.storage.from_.assert_not_called()


def test_noChartTypeWidgetWithCodeIsMergedUnderAFilter(monkeypatch):
    saved = _noType()
    service, manager, _ = _setup(monkeypatch, [saved])

    widget = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))["widgets"][0]

    assert manager.run.call_args.kwargs["filters"] == FILTERS
    assert widget["data"] == "new" and widget["id"] == "N" and widget["layout"] == {"y": 2}
    assert widget["generatedCode"] == PYTHON
    service.client.storage.from_.assert_not_called()


def test_noChartTypeWidgetWithCodeIsMergedAndSavedOnRefresh(monkeypatch):
    saved = _noType()
    service, _, _ = _setup(monkeypatch, [saved])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    uploaded = _uploaded(service)["pg"]["widgets"][0]
    assert uploaded["data"] == "new" and page["widgets"][0]["data"] == "new"
    assert uploaded["id"] == "N" and uploaded["layout"] == {"y": 2} and uploaded["generatedCode"] == PYTHON


def test_noChartTypeWidgetThatGainsAChartTypeKeepsIt(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_noType()],
                           run=lambda code, projectId=None, filters=None: json.dumps({"chartType": "card", "data": 7}))

    widget = service.getData(GetData(projectId="p1", page="pg", refresh=True))["widgets"][0]

    assert widget["chartType"] == "card" and widget["data"] == 7
    assert _uploaded(service)["pg"]["widgets"][0]["chartType"] == "card"


def test_failedNoChartTypeWidgetIsUnchangedUnderAFilter(monkeypatch):
    saved = _noType()
    service, manager, _ = _setup(monkeypatch, [saved], run=lambda code, projectId=None, filters=None: "Traceback")

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert page["widgets"] == [saved]
    assert "chartType" not in page["widgets"][0]
    manager.run.assert_called_once()


def test_failedNoChartTypeWidgetIsUnchangedAndNotSavedOnRefresh(monkeypatch):
    saved = _noType()
    service, manager, _ = _setup(monkeypatch, [saved], run=lambda code, projectId=None, filters=None: "Traceback")

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert page["widgets"] == [saved]
    manager.run.assert_called_once()
    service.client.storage.from_.assert_not_called()


@pytest.mark.parametrize("output", [
    '{"error": "x"}', "Traceback (most recent call last):\n  File ...", "Execution timed out after 35 seconds.", "[1, 2]",
])
def test_pythonErrorOrNonJsonOutputIsAFailure(monkeypatch, output):
    service, _, _ = _setup(monkeypatch, [_widget("A")], run=lambda code, projectId=None, filters=None: output)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.assert_not_called()
    assert page["widgets"][0]["data"] == "old"


def _unreadableMetadata(monkeypatch, service_module_name="api.services.dashboardService"):
    import importlib

    module = importlib.import_module(service_module_name)
    original = module.readJson

    def read(projectId, name):
        if name == "metadata.json":
            raise RuntimeError("storage said no secret-table-name")
        return original(projectId, name)

    monkeypatch.setattr(module, "readJson", read)


@pytest.fixture
def logLines(monkeypatch):
    """Messages the service logs. Loguru does not reach caplog, and other tests replace `utils.logger.logger`."""
    from api.services import dashboardService as module

    class Recorder:
        def __init__(self):
            self.lines: list[str] = []

        def warning(self, message, *args, **kwargs):
            self.lines.append(str(message))

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    recorder = Recorder()
    monkeypatch.setattr(module, "logger", recorder)
    return recorder.lines


STORED_FILES = [
    {"name": "orders.parquet"}, {"name": "old.parquet"}, {"name": "metadata.json"},
    {"name": "dashboardConfig.json"}, {"name": "someFolder"}, {"name": None},
]


def _storedFiles(monkeypatch, files=STORED_FILES, error=None):
    """Replace the project folder listing; returns the projects it was asked for."""
    from api.services import dashboardService as module

    asked = []

    def listing(client, projectId):
        asked.append(projectId)
        if error is not None:
            raise error
        return copy.deepcopy(files)

    monkeypatch.setattr(module, "listProjectFiles", listing)
    return asked


def test_unreadableMetadataRerunsChartRequestWidgetsFromTheStoredTablesUnderAFilter(monkeypatch, logLines):
    code = _request()
    saved = [_widget("R", code, "bar", data={"datasets": [{"data": [1]}]}), _widget("P")]
    service, manager, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    asked = _storedFiles(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    chartRequest, python = page["widgets"]
    assert chartRequest["data"] == "new" and chartRequest["generatedCode"] == code and chartRequest["id"] == "R"
    assert python["data"] == "new"
    args, kwargs = renderer.call_args
    assert args[3] == ("old", "orders") and kwargs["filters"] == FILTERS
    assert asked == ["p1"]
    manager.run.assert_called_once()
    assert any("table list" in line for line in logLines)
    assert not any("secret-table-name" in line for line in logLines)


def test_unreadableMetadataRefreshSavesChartRequestDataFromTheStoredTables(monkeypatch):
    saved = [_widget("R", _request(), "bar"), _widget("P")]
    service, _, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["data"] for w in page["widgets"]] == ["new", "new"]
    assert [w["data"] for w in _uploaded(service)["pg"]["widgets"]] == ["new", "new"]
    assert renderer.call_args.args[3] == ("old", "orders")


@pytest.mark.parametrize("metadata", [["orders"], "orders", None])
def test_metadataThatIsNotAnObjectFallsBackToTheStoredTables(monkeypatch, metadata):
    from api.services import dashboardService as module

    service, _, renderer = _setup(monkeypatch, [_widget("R", _request(), "bar")])
    original = module.readJson
    monkeypatch.setattr(module, "readJson", lambda projectId, name: metadata if name == "metadata.json" else original(projectId, name))
    _storedFiles(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert page["widgets"][0]["data"] == "new"
    assert renderer.call_args.args[3] == ("old", "orders")


def test_unreadableMetadataAndListingFailsOnlyTheChartRequestWidgetsUnderAFilter(monkeypatch, logLines):
    code = _request()
    saved = [_widget("R", code, "bar", data={"datasets": [{"data": [1]}]}), _widget("P")]
    service, manager, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch, error=RuntimeError("listing said no secret-table-name"))

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    chartRequest, python = page["widgets"]
    assert chartRequest["chartType"] == "bar" and chartRequest["data"] == {"labels": [], "datasets": []}
    assert chartRequest["generatedCode"] == code and chartRequest["id"] == "R"
    assert python["data"] == "new"
    renderer.assert_not_called()
    manager.run.assert_called_once()
    assert any("table list" in line for line in logLines)
    assert not any("secret-table-name" in line for line in logLines)


def test_unreadableMetadataAndListingKeepsSavedChartRequestDataOnRefresh(monkeypatch):
    saved = [_widget("R", _request(), "bar"), _widget("P")]
    service, _, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch, error=RuntimeError("listing said no"))

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["data"] for w in page["widgets"]] == ["old", "new"]
    assert [w["data"] for w in _uploaded(service)["pg"]["widgets"]] == ["old", "new"]
    renderer.assert_not_called()


def test_busyWhileListingTheStoredTablesStillGives503(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("R", _request(), "bar")])
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch, error=SandboxBusyError())

    with pytest.raises(CustomException) as caught:
        service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert caught.value.statusCode == 503


def test_readableMetadataNeverListsTheStoredTables(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("R", _request(), "bar")])
    asked = _storedFiles(monkeypatch)

    service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert asked == []


def test_unreadableMetadataWithOnlyPythonWidgetsIsNeverRead(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("P")])
    _unreadableMetadata(monkeypatch)
    asked = _storedFiles(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert page["widgets"][0]["data"] == "new"
    assert asked == []


def test_busyWhileReadingMetadataStillGives503(monkeypatch):
    from api.services import dashboardService as module

    service, _, _ = _setup(monkeypatch, [_widget("R", _request(), "bar")])
    original = module.readJson

    def read(projectId, name):
        if name == "metadata.json":
            raise SandboxBusyError()
        return original(projectId, name)

    monkeypatch.setattr(module, "readJson", read)

    with pytest.raises(CustomException) as caught:
        service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert caught.value.statusCode == 503


def _failureLines(logLines, widgetId):
    return [line for line in logLines if f"Widget {widgetId} " in line]


@pytest.mark.parametrize("output, kind", [
    ("Traceback (most recent call last):\n  File \"x\", line 1, in <module>\nValueError: bad value 4242-secret\n", "ValueError"),
    ("Traceback (most recent call last):\n  File \"x\", line 1\nsqlalchemy.exc.OperationalError: (psycopg2) host=secret-host\n", "sqlalchemy.exc.OperationalError"),
    ("\nExecution timed out after 35 seconds.\n", "timed out"),
    ("Concurrency limit reached: Too many running code execution tasks for project 'p1'. Please try again in a few seconds.", "concurrency limit"),
    ('{"error": "secret message 4242"}', "error key"),
    ("revenue is 4242-secret", "not JSON"),
    ("[1, 2]", "not JSON"),
])
def test_failedPythonRerunLogsIdFormAndKindOnly(monkeypatch, logLines, output, kind):
    service, _, _ = _setup(monkeypatch, [_widget("W9")], run=lambda code, projectId=None, filters=None: output)

    service.getData(GetData(projectId="p1", page="pg", filters=[{"region": "north-secret"}]))

    lines = _failureLines(logLines, "W9")
    assert len(lines) == 1
    assert "python" in lines[0] and kind in lines[0]
    for forbidden in ("4242", "secret", "psycopg2", "north"):
        assert forbidden not in lines[0]


def test_failedChartRequestRerunLogsIdFormAndClassOnly(monkeypatch, logLines):
    def failRender(*args, **kwargs):
        raise ChartFailure("run", "boom 4242-secret")

    service, _, _ = _setup(monkeypatch, [_widget("W8", _request(), "bar")], render=failRender)

    service.getData(GetData(projectId="p1", page="pg", filters=[{"region": "north-secret"}]))

    lines = _failureLines(logLines, "W8")
    assert len(lines) == 1
    assert "chartRequest" in lines[0] and "ChartFailure" in lines[0]
    for forbidden in ("4242", "secret", "boom", "north"):
        assert forbidden not in lines[0]


def _secondReadDiffers(monkeypatch, service, latestWidgets):
    """The first dashboardConfig.json read returns the setup copy; later reads return `latestWidgets`."""
    from api.services import dashboardService as module

    original = module.readJson
    reads = []

    def read(projectId, name):
        if name != "dashboardConfig.json":
            return original(projectId, name)
        reads.append(name)
        if len(reads) == 1:
            return original(projectId, name)
        return {"pg": {"name": "renamed", "widgets": copy.deepcopy(latestWidgets)}}

    monkeypatch.setattr(module, "readJson", read)
    return reads


def test_refreshSavesIntoTheLatestDashboardNotTheCopyReadBeforeTheRuns(monkeypatch):
    a, b, c = _widget("A", PYTHON + "\n# a"), _widget("B", PYTHON + "\n# b"), _widget("C", PYTHON + "\n# c")
    service, _, _ = _setup(monkeypatch, [a, b, c],
                           run=lambda code, projectId=None, filters=None: json.dumps({"data": "newA"} if "# a" in code else {"data": "newC"} if "# c" in code else {"error": "x"}))
    movedA = dict(copy.deepcopy(a), layout={"x": 99})
    editedC = dict(copy.deepcopy(c), generatedCode="edited code")
    reads = _secondReadDiffers(monkeypatch, service, [movedA, editedC])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    saved = _uploaded(service)["pg"]
    assert len(reads) == 2
    assert saved["name"] == "renamed"
    assert [w["id"] for w in saved["widgets"]] == ["A", "C"]
    assert [w["data"] for w in saved["widgets"]] == ["newA", "newC"]
    assert saved["widgets"][0]["layout"] == {"x": 99}
    assert saved["widgets"][1]["generatedCode"] == "edited code"
    assert [w["id"] for w in page["widgets"]] == ["A", "C"] and page["id"] == "pg"
    assert [w["data"] for w in page["widgets"]] == ["newA", "newC"]


def test_refreshDoesNotBringBackAWidgetDeletedDuringTheRuns(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("A"), _widget("B")])
    _secondReadDiffers(monkeypatch, service, [_widget("B")])

    service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["id"] for w in _uploaded(service)["pg"]["widgets"]] == ["B"]


def test_refreshUploadsNothingWhenEverySucceededWidgetWasDeleted(monkeypatch):
    def run(code, projectId=None, filters=None):
        return json.dumps({"data": "newA"}) if "marker_a" in code else "Traceback"

    service, _, _ = _setup(monkeypatch, [_widget("A", PYTHON + "\n# marker_a"), _widget("B")], run=run)
    _secondReadDiffers(monkeypatch, service, [_widget("B")])

    service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.assert_not_called()


def test_refreshSavesEveryKeyAPythonWidgetReturnsAndTheResponseMatches(monkeypatch):
    output = {"data": "new", "options": {"x": 1}, "id": "other", "layout": {"x": 99}, "generatedCode": "evil"}
    service, _, _ = _setup(monkeypatch, [_widget("A")], run=lambda code, projectId=None, filters=None: json.dumps(output))
    _secondReadDiffers(monkeypatch, service, [_widget("A")])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    saved = _uploaded(service)["pg"]["widgets"][0]
    assert saved["options"] == {"x": 1} and saved["data"] == "new"
    assert saved["id"] == "A" and saved["layout"] == {"x": 1} and saved["generatedCode"] == PYTHON
    assert page["widgets"][0] == saved
    assert page["widgets"] == _uploaded(service)["pg"]["widgets"]


def test_refreshKeepsANonOutputKeyEditedDuringTheRuns(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("A", note="stale")])
    _secondReadDiffers(monkeypatch, service, [_widget("A", note="edited meanwhile", title="Renamed")])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    saved = _uploaded(service)["pg"]["widgets"][0]
    assert saved["note"] == "edited meanwhile"
    assert saved["data"] == "new"
    assert page["widgets"][0]["note"] == "edited meanwhile"


def test_refreshOfAChartRequestWidgetSavesOnlyChartKeys(monkeypatch):
    service, _, _ = _setup(
        monkeypatch, [_widget("R", _request(), "bar", note="n")],
        render=lambda *a, **k: {"chartType": "bar", "title": "t", "data": "new", "sql": "select 1", "extra": 1},
    )
    _secondReadDiffers(monkeypatch, service, [_widget("R", _request(), "bar", note="n")])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    saved = _uploaded(service)["pg"]["widgets"][0]
    assert saved["data"] == "new" and saved["note"] == "n"
    assert "sql" not in saved and "extra" not in saved
    assert page["widgets"][0] == saved


def _traceback(raiser) -> str:
    import traceback

    try:
        raiser()
    except Exception:
        return traceback.format_exc()
    raise AssertionError("did not raise")


def _loggedKind(monkeypatch, logLines, output) -> str:
    service, _, _ = _setup(monkeypatch, [_widget("K1")], run=lambda code, projectId=None, filters=None: output)
    service.getData(GetData(projectId="p1", page="pg", refresh=True))
    (line,) = _failureLines(logLines, "K1")
    return line.split("(python): ", 1)[1]


def test_multiLineValueErrorLogsOnlyTheClass(monkeypatch, logLines):
    def raiser():
        raise ValueError("bad row\nAlice_Smith owes 4242")

    kind = _loggedKind(monkeypatch, logLines, _traceback(raiser))

    assert kind == "ValueError"
    assert not any("Alice_Smith" in line or "4242" in line for line in logLines)


def test_polarsColumnNotFoundLogsItsDottedClass(monkeypatch, logLines):
    import polars as pl

    def raiser():
        pl.DataFrame({"a": [1]}).select("nope_secret")

    text = _traceback(raiser)
    assert text.rstrip().splitlines()[-1] != text.rstrip().splitlines()[0]

    assert _loggedKind(monkeypatch, logLines, text) == "polars.exceptions.ColumnNotFoundError"
    assert not any("nope_secret" in line for line in logLines)


def test_polarsMultiLineCastErrorLogsItsClass(monkeypatch, logLines):
    import polars as pl

    def raiser():
        pl.DataFrame({"v": ["x_secret"]}).select(pl.col("v").cast(pl.Int64))

    kind = _loggedKind(monkeypatch, logLines, _traceback(raiser))

    assert kind.startswith("polars.exceptions.") and kind.endswith("Error")
    assert not any("x_secret" in line for line in logLines)


def test_chainedExceptionLogsTheFinalClass(monkeypatch, logLines):
    def raiser():
        try:
            raise KeyError("first_secret")
        except KeyError:
            raise RuntimeError("second_secret")

    text = _traceback(raiser)
    assert "During handling of the above exception" in text

    assert _loggedKind(monkeypatch, logLines, text) == "RuntimeError"
    assert not any("secret" in line for line in logLines)


@pytest.mark.parametrize("tail", [
    "Alice_Smith owes 4242",
    "\tcol(\"v\").strict_cast(Int64)",
    "revenue: 4242",
    "secret_thing",
    "secret.module.Thing: message",
    "",
])
def test_garbageAfterTheFrameLogsUnknownException(monkeypatch, logLines, tail):
    text = 'Traceback (most recent call last):\n  File "x", line 1, in <module>\n    boom()\n' + tail

    kind = _loggedKind(monkeypatch, logLines, text)

    assert kind == "unknown exception"
    assert not any("Alice" in line or "secret" in line or "4242" in line or "revenue" in line for line in logLines)


def test_exceptionWithoutMessageAndBuiltinNamesAreAccepted(monkeypatch, logLines):
    text = 'Traceback (most recent call last):\n  File "x", line 1, in <module>\nStopIteration'
    assert _loggedKind(monkeypatch, logLines, text) == "StopIteration"


def test_refreshReturnsTheLatestPageWhenEverySucceededWidgetWasDeleted(monkeypatch):
    def run(code, projectId=None, filters=None):
        return json.dumps({"data": "newA"}) if "marker_a" in code else "Traceback"

    service, _, _ = _setup(monkeypatch, [_widget("A", PYTHON + "\n# marker_a"), _widget("B")], run=run)
    _secondReadDiffers(monkeypatch, service, [_widget("B", data="latest")])

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    service.client.storage.from_.assert_not_called()
    assert page["name"] == "renamed"
    assert [(w["id"], w["data"]) for w in page["widgets"]] == [("B", "latest")]
    assert page["id"] == "pg"



# Saved SQL charts: the chart the SQL workflow served (or failed), saved as the client and the automatic dashboard save it


def _sqlRender(manager, projectId, request, activeTables, filters=None, **options):
    """A deterministic stand-in for the SQL engine: the chart depends only on the request and the filters."""
    total = 7 if not filters else 2
    return {"chartType": request.chartType, "title": request.title,
            "data": {"labels": ["x"], "datasets": [{"label": "total", "data": [total]}]}}


def _savedFiles(monkeypatch, service):
    """Dashboard files the service reads, kept as the JSON its uploads wrote."""
    from api.services import dashboardService as module
    from test.test_sql_chart_workflow import METADATA

    files = {"dashboardConfig.json": {"pg": {"name": "n", "widgets": []}}, "metadata.json": copy.deepcopy(METADATA)}
    monkeypatch.setattr(module, "readJson", lambda projectId, name: copy.deepcopy(files[name]))
    monkeypatch.setattr(module, "updateProjectModifiedAt", lambda projectId: None)

    def saved():
        files["dashboardConfig.json"] = _uploaded(service)
        service.client.reset_mock()

    return files, saved


def test_aSavedSqlChartReRunsThroughTheSqlPathAndGivesTheSameChart(monkeypatch):
    from api.models import ExportToDashboard
    from test.test_sql_chart_workflow import Harness

    harness = Harness(monkeypatch)
    harness.render.side_effect = _sqlRender
    served = harness.run()
    chart = served["finalOutput"]
    servedRequest = harness.render.call_args.args[2]

    service, manager, renderer = _setup(monkeypatch, [], render=_sqlRender)
    files, saved = _savedFiles(monkeypatch, service)
    widgetId = service.exportToDashboard(ExportToDashboard(
        projectId="p1", page="pg", chartType=chart["chartType"], title=chart["title"], data=chart["data"],
        layout={"x": 0, "y": 0, "w": 6, "h": 10}, generatedCode=served["generatedCode"]))
    saved()

    filtered = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))["widgets"][0]
    args, kwargs = renderer.call_args
    assert args[2] == servedRequest and args[3] == ("orders", "old_table")
    assert kwargs == {"filters": FILTERS, "lenient": True}
    assert filtered["id"] == widgetId and filtered["generatedCode"] == served["generatedCode"]
    assert filtered["data"] == _sqlRender(None, "p1", servedRequest, None, filters=FILTERS)["data"]
    service.client.storage.from_.assert_not_called()

    refreshed = service.getData(GetData(projectId="p1", page="pg", refresh=True))["widgets"][0]
    assert renderer.call_args.kwargs == {"filters": None, "lenient": True}
    for widget in (refreshed, _uploaded(service)["pg"]["widgets"][0]):
        assert {key: widget[key] for key in ("chartType", "title", "data")} == {key: chart[key] for key in ("chartType", "title", "data")}
        assert widget["generatedCode"] == served["generatedCode"] and widget["layout"] == {"x": 0, "y": 0, "w": 6, "h": 10}
    manager.run.assert_not_called()


_INVALID = json.dumps({"chartType": "bar", "title": "Totals", "sql": 'SELECT "a" FROM "orders"'})  # no encoding


def _python(code, projectId=None, filters=None):
    """What Python does with the text: the sandbox's output, or its error."""
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    return done.stdout if done.returncode == 0 else "Traceback (most recent call last):\n" + done.stderr


@pytest.mark.parametrize("reply", [_INVALID, "```json\n" + _INVALID + "\n```"], ids=["json-without-marker", "fenced"])
def test_aFailedSqlChartSavedWithItsRawTextIsAFailedReRunThatChangesNothing(monkeypatch, logLines, reply):
    from test.test_sql_chart_workflow import Harness

    harness = Harness(monkeypatch)
    harness.generator.invoke.return_value = reply
    harness.repair.invoke.return_value = reply
    failed = harness.run()
    assert "error" in failed["finalOutput"] and failed["generatedCode"] == reply
    harness.render.assert_not_called()

    # The automatic dashboard saves a failed chart as a widget without a chart type, keeping the raw text.
    widget = {"id": "F1", "chartType": failed["finalOutput"].get("chartType"), "title": None, "label": None,
              "xLabels": None, "yLabels": None, "data": None, "query": "Totals by a",
              "layout": {"x": 0, "y": 0, "w": 6, "h": 10}, "generatedCode": failed["generatedCode"]}
    service, manager, renderer = _setup(monkeypatch, [widget], run=_python)

    filtered = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))["widgets"][0]
    assert filtered == widget
    refreshed = service.getData(GetData(projectId="p1", page="pg", refresh=True))["widgets"][0]
    assert refreshed == widget
    service.client.storage.from_.assert_not_called()

    # It went down the old Python path (fences stripped as for any saved code), failed, and was logged by id and form only.
    renderer.assert_not_called()
    assert manager.run.call_count == 2
    assert manager.run.call_args.args[0] == reply.removesuffix("\n```")
    lines = _failureLines(logLines, "F1")
    assert len(lines) == 2 and all("(python)" in line for line in lines)
    assert not any("orders" in line or "SELECT" in line for line in lines)


# A table switched to inactive: saved widgets on it keep working, in both forms. The SQL check is the real one.

INACTIVE_PYTHON = "df = fetch_data('p1', 'old')\nprint(1)"


def _requestOn(sql: str) -> str:
    return encodeGeneratedCode(ChartRequest.model_validate({
        "chartType": "bar", "title": "t", "sql": sql, "encoding": {"x": "a", "y": ["a"]},
    }))


def _realCheck(monkeypatch) -> list:
    """Re-run ChartRequest widgets through the real `renderChartRequest` and SQL check; only the run and build are fakes.

    Returns the (tables, filters) of every query that passed the check and reached the run.
    """
    from api.services import dashboardService as module
    from nubrix.components import chartEngine

    ran = []

    def run(manager, projectId, checked, request, filters, waitForSlot=True):
        ran.append((checked.tables, filters))
        return MagicMock(table="rows", truncated=False)

    monkeypatch.setattr(chartEngine, "runChartQuery", run)
    monkeypatch.setattr(chartEngine, "buildChart", lambda request, table, truncated, lenient: {"chartType": "bar", "title": "t", "data": "new"})
    monkeypatch.setattr(module, "renderChartRequest", chartEngine.renderChartRequest)
    return ran


def test_chartRequestOnAnInactiveTableRerunsLikeAPythonWidgetUnderAFilter(monkeypatch):
    saved = [_widget("R", _requestOn("select a from old"), "bar"), _widget("P", INACTIVE_PYTHON)]
    service, manager, _ = _setup(monkeypatch, saved)
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert [w["data"] for w in page["widgets"]] == ["new", "new"]
    assert ran == [(("old",), FILTERS)]
    assert manager.run.call_args.kwargs["filters"] == FILTERS
    service.client.storage.from_.assert_not_called()


def test_chartRequestOnAnInactiveTableRerunsLikeAPythonWidgetOnRefresh(monkeypatch):
    saved = [_widget("R", _requestOn("select a from OLD join orders using (a)"), "bar"), _widget("P", INACTIVE_PYTHON)]
    service, manager, _ = _setup(monkeypatch, saved)
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["data"] for w in page["widgets"]] == ["new", "new"]
    assert [w["data"] for w in _uploaded(service)["pg"]["widgets"]] == ["new", "new"]
    assert ran == [(("old", "orders"), None)]
    assert manager.run.call_args.kwargs["filters"] is None


def test_rerunTablesAreEveryTableEntryActiveOrNot(monkeypatch):
    metadata = {
        "orders": {"isActive": True}, "old": {"isActive": False}, "legacy": {},
        "_settings": {"isActive": True}, "notATable": "text", "alsoNot": ["x"],
    }
    service, _, renderer = _setup(monkeypatch, [_widget("R", _request(), "bar")], metadata=metadata)

    service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert renderer.call_args.args[3] == ("orders", "legacy", "old")


@pytest.mark.parametrize("sql", [
    "select a from ghost",
    "select a from _settings",
    "select a from 'old.parquet'",
    "select a from main.old",
    "select a from memory.main.old",
    "select a from read_parquet('old.parquet')",
])
@pytest.mark.parametrize("refresh", [False, True])
def test_rerunStillRefusesUnknownTablesFilesAndQualifiedNames(monkeypatch, sql, refresh):
    metadata = {"orders": {}, "old": {"isActive": False}, "_settings": {}}
    saved = _widget("R", _requestOn(sql), "bar", data={"datasets": [{"data": [1]}]})
    service, _, _ = _setup(monkeypatch, [saved], metadata=metadata)
    _storedFiles(monkeypatch, files=[{"name": "ghost.parquet"}])
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=None if refresh else FILTERS, refresh=refresh))

    assert ran == []
    if refresh:
        assert page["widgets"][0]["data"] == {"datasets": [{"data": [1]}]}
        service.client.storage.from_.assert_not_called()
    else:
        assert page["widgets"][0]["data"] == {"labels": [], "datasets": []}


def test_unreadableMetadataRerunsOnlyTheStoredTablesThroughTheRealCheck(monkeypatch):
    saved = [_widget("R", _requestOn("select a from old"), "bar"), _widget("G", _requestOn("select a from ghost"), "bar")]
    service, _, _ = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch)
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["data"] for w in page["widgets"]] == ["new", "old"]
    assert ran == [(("old",), None)]


def test_generationStillHidesAndRefusesAnInactiveTable():
    from api.services.reportingService import ReportingService
    from nubrix.components.sqlCheck import SqlCheckError, checkSql

    shown = ReportingService._filterActive(METADATA)

    assert set(shown) == {"orders"}
    checkSql("select a from orders", shown)
    with pytest.raises(SqlCheckError, match="not available"):
        checkSql("select a from old", shown)


# Names that differ only in case: DuckDB folds them together, and the SQL check keeps the first spelling it is given
# for each folded name. The re-run set from metadata is ordered (active tables first, in metadata order, then inactive
# ones), so a name resolves to the same table on every worker, and to the active one on a clash. The fallback listing
# can't tell which table is active, so it leaves out every name that clashes and keeps the others, sorted.

CASE_METADATA = {"Sales": {"isActive": False}, "sales": {"isActive": True}}


@pytest.mark.parametrize("named", ['"sales"', '"Sales"', "SALES"])
def test_aCaseCollidingNameResolvesToTheActiveTable(monkeypatch, named):
    saved = [_widget("R", _requestOn(f"select a from {named}"), "bar")]
    service, _, _ = _setup(monkeypatch, saved, metadata=CASE_METADATA)
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert page["widgets"][0]["data"] == "new"
    assert ran == [(("sales",), FILTERS)]


CASE_FILES = [{"name": "sales.parquet"}, {"name": "orders.parquet"}, {"name": "Sales.parquet"}]


@pytest.mark.parametrize("refresh", [False, True])
def test_caseCollidingNamesInTheStoredTablesAreLeftOutAndTheOthersStillRun(monkeypatch, refresh):
    saved = [
        _widget("L", _requestOn('select a from "sales"'), "bar", data={"datasets": [{"data": [1]}]}),
        _widget("U", _requestOn('select a from "Sales"'), "bar", data={"datasets": [{"data": [2]}]}),
        _widget("O", _requestOn("select a from ORDERS"), "bar", data={"datasets": [{"data": [3]}]}),
    ]
    service, _, _ = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)
    _storedFiles(monkeypatch, files=CASE_FILES)
    ran = _realCheck(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=None if refresh else FILTERS, refresh=refresh))

    assert ran == [(("orders",), None if refresh else FILTERS)]
    lower, upper, orders = page["widgets"]
    assert orders["data"] == "new"
    if refresh:
        # Refused, so the widgets keep their saved data and only the one that ran is saved.
        assert (lower["data"], upper["data"]) == ({"datasets": [{"data": [1]}]}, {"datasets": [{"data": [2]}]})
        assert [w["data"] for w in _uploaded(service)["pg"]["widgets"]] == [lower["data"], upper["data"], "new"]
    else:
        assert lower["data"] == upper["data"] == {"labels": [], "datasets": []}


def test_storedTableNamesLeaveOutEveryNameInACaseCollidingGroup():
    from api.services.dashboardService import _storedTableNames

    files = [{"name": f"{name}.parquet"} for name in ("sales", "SALES", "Sales", "orders", "Old", "b", "x\u212a", "xk")]

    # ASCII letters fold together as DuckDB folds them; the Kelvin sign is not ASCII, so it does not clash with "k".
    assert _storedTableNames(files) == ("Old", "b", "orders", "xk", "x\u212a")


_CASE_PROBE = """
import json
from unittest.mock import MagicMock
from api.services import dashboardService as module
from nubrix.components.sqlCheck import SqlCheckError, checkSql


def resolved(tables):
    out = []
    for name in ('"sales"', '"Sales"', "orders"):
        try:
            out.append(list(checkSql(f"select a from {name}", tables).tables))
        except SqlCheckError:
            out.append("refused")
    return out


service =module.DashboardService.__new__(module.DashboardService)
service.client = MagicMock()
out = {}
module.readJson = lambda projectId, name: {"Sales": {"isActive": False}, "sales": {"isActive": True}, "orders": {}}
out["metadata"] = resolved(service._rerunTables("p1"))

def unreadable(projectId, name):
    raise RuntimeError("unreadable")

module.readJson = unreadable
module.listProjectFiles = lambda client, projectId: [{"name": n + ".parquet"} for n in ("sales", "orders", "Sales")]
out["listing"] = resolved(service._rerunTables("p1"))
print("RESULT " + json.dumps(out))
"""


@pytest.mark.parametrize("seed", ["0", "4", "7"])
def test_aCaseCollidingNameResolvesTheSameUnderEveryHashSeed(seed):
    """String hashing is randomised per process; seeds 0 and 4 iterate a set of the two spellings in opposite orders."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    done = subprocess.run(
        [sys.executable, "-c", _CASE_PROBE], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, timeout=300,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    (line,) = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert json.loads(line[len("RESULT "):]) == {
        "metadata": [["sales"], ["sales"], ["orders"]],
        "listing": ["refused", "refused", ["orders"]],
    }
