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


def test_unreadableMetadataFailsOnlyTheChartRequestWidgetsUnderAFilter(monkeypatch, logLines):
    code = _request()
    saved = [_widget("R", code, "bar", data={"datasets": [{"data": [1]}]}), _widget("P")]
    service, manager, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    chartRequest, python = page["widgets"]
    assert chartRequest["chartType"] == "bar" and chartRequest["data"] == {"labels": [], "datasets": []}
    assert chartRequest["generatedCode"] == code and chartRequest["id"] == "R"
    assert python["data"] == "new"
    renderer.assert_not_called()
    manager.run.assert_called_once()
    assert any("table list" in line for line in logLines)
    assert not any("secret-table-name" in line for line in logLines)


def test_unreadableMetadataKeepsSavedChartRequestDataOnRefresh(monkeypatch):
    saved = [_widget("R", _request(), "bar"), _widget("P")]
    service, manager, renderer = _setup(monkeypatch, saved)
    _unreadableMetadata(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", refresh=True))

    assert [w["data"] for w in page["widgets"]] == ["old", "new"]
    assert [w["data"] for w in _uploaded(service)["pg"]["widgets"]] == ["old", "new"]
    renderer.assert_not_called()


def test_unreadableMetadataWithOnlyPythonWidgetsIsNeverRead(monkeypatch):
    service, _, _ = _setup(monkeypatch, [_widget("P")])
    _unreadableMetadata(monkeypatch)

    page = service.getData(GetData(projectId="p1", page="pg", filters=FILTERS))

    assert page["widgets"][0]["data"] == "new"


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
