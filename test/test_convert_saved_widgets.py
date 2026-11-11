"""The verified conversion of saved dashboard widgets (`scripts/convertSavedWidgets.py`).

Most tests replace the model, the workflow and the chart run with fakes and use an in-memory storage client, so they
are fast. Two tests run the real sandbox on the synthetic chart evaluation tables, with recorded model replies: one dry
run and one write. Everything is synthetic.
"""

import copy
import datetime as dt
import json
import math
from pathlib import Path

import pytest

from nubrix.components.chartEngine import ChartFailure
from nubrix.components.chartRequest import ChartRequest, decodeGeneratedCode, encodeGeneratedCode
from scripts import chartEval
from scripts import convertSavedWidgets as conv
from utils.sandboxSlots import BUSY_MESSAGE, SandboxBusyError

FIXTURES = Path(__file__).parent / "fixtures" / "chart_eval"
DATA = FIXTURES / "data"
STUB = FIXTURES / "modelStub.jsonl"
PROJECT = "11111111-2222-4333-8444-555555555555"
FIXTURE_KEY = "chart-eval-fixture-key"
CONFIG = "dashboardConfig.json"
NOW = dt.datetime(2026, 11, 11, 9, 0, 0, tzinfo=dt.timezone.utc)
BACKUP = "dashboardConfig.backup-20261111T090000Z.json"
METADATA = {"orders": {"shape": [4, 2], "columns": [{"name": "region", "type": "object"}, {"name": "revenue", "type": "float64"}]}}
PYTHON = "import polars as pl\ndf = fetch_data_pl(projectId, 'orders')\nprint(json.dumps(chart))"
PANEL = (
    "```python\ndef panelChart(\n    projectId: str,\n    chartType: str,\n    **kwargs\n    ):\n    print(1)\n"
    "panelChart(\n    projectId = \"p\",\n    chartType = \"bar\",\n    xAxis = \"region\",\n)\n```"
)


# Fakes


class FakeStore:
    """The project's folder in the bucket. `hooks[(name, n)]` answers the n-th read of `name` instead of the file."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.log: list[tuple] = []
        self.hooks: dict = {}
        self.refuse: set[str] = set()

    def read(self, name: str) -> bytes:
        self.log.append(("read", name))
        count = sum(1 for entry in self.log if entry == ("read", name))
        hook = self.hooks.get((name, count))
        if hook is not None:
            return hook()
        if name not in self.files:
            raise FileNotFoundError(name)
        return self.files[name]

    def write(self, name: str, data: bytes, overwrite: bool) -> None:
        self.log.append(("write", name, overwrite))
        if name in self.refuse:
            raise RuntimeError("upload refused")
        if not overwrite and name in self.files:
            raise RuntimeError("already exists")
        self.files[name] = bytes(data)

    def writes(self) -> list:
        return [entry for entry in self.log if entry[0] == "write"]


def _bar(labels, values, datasetLabel="Total Revenue", xLabels="Region", yLabels="Total Revenue", title="t"):
    return {"chartType": "bar", "title": title, "xLabels": xLabels, "yLabels": yLabels,
            "data": {"labels": list(labels), "datasets": [{"label": datasetLabel, "data": list(values)}]}}


def _request(sql, chartType="bar", title="Model title"):
    encoding = {"x": "region", "y": ["revenue"]} if chartType == "bar" else {"value": "n", "label": "Orders"}
    return ChartRequest.model_validate({"chartType": chartType, "title": title, "sql": sql, "encoding": encoding})


def _widget(widgetId, chart, code=PYTHON, query=None, **extra):
    widget = {"id": widgetId, "chartType": chart.get("chartType"), "title": chart.get("title"), "label": chart.get("label"),
              "xLabels": chart.get("xLabels"), "yLabels": chart.get("yLabels"), "data": chart.get("data")}
    if "map" in chart:
        widget["map"] = chart["map"]
    if query is not None:
        widget["query"] = query
    widget.update(layout={"x": 0, "y": 0, "w": 6, "h": 10}, generatedCode=code, **extra)
    return widget


def _config(*widgets, page="page-1", more=None):
    config = {page: {"name": "Sales", "isAutomatic": True, "widgets": list(widgets)}}
    config.update(more or {})
    return config


def _bytes(config) -> bytes:
    return json.dumps(config, indent=4).encode("utf-8")


def _store(config):
    return FakeStore({CONFIG: _bytes(config), "metadata.json": json.dumps(METADATA).encode("utf-8")})


class _Fakes:
    """The workflow and the chart run: `plans[prompt]` is what the model makes of a description (a ChartRequest, a
    workflow response dict, or an exception) and `charts[sql]` what the run returns (a chart dict or an exception)."""

    def __init__(self, monkeypatch, plans=None, charts=None):
        self.plans = plans or {}
        self.charts = charts or {}
        self.prompts: list[str] = []
        self.renders: list[dict] = []
        fakes = self

        class Engine:
            def __init__(self, replay):
                self.failure = None

            def invoke(self, metadata, prompt, project):
                fakes.prompts.append(prompt)
                self.failure = None
                plan = fakes.plans[prompt]
                if isinstance(plan, Exception):
                    raise plan
                if isinstance(plan, ChartRequest):
                    return {"finalOutput": {"chartType": plan.chartType}, "generatedCode": encodeGeneratedCode(plan)}
                if isinstance(plan, tuple):
                    stage, response = plan
                    self.failure = {"stage": stage, "error": "detail", "repairs": 2}
                    return response
                return plan

        def render(manager, projectId, request, activeTables, filters=None, lenient=False, waitForSlot=True):
            fakes.renders.append({"sql": request.sql, "title": request.title, "filters": filters, "lenient": lenient,
                                  "waitForSlot": waitForSlot, "activeTables": set(activeTables), "projectId": projectId})
            chart = fakes.charts[request.sql]
            if isinstance(chart, Exception):
                raise chart
            return {**copy.deepcopy(chart), "title": request.title}

        monkeypatch.setattr(conv, "_Engine", Engine)
        monkeypatch.setattr(conv, "renderChartRequest", render)
        monkeypatch.setattr(conv, "_utcNow", lambda: NOW)


def _run(tmp_path, store, *extra):
    return conv.main(["--project", PROJECT, "--out", str(tmp_path / "report.json"), *extra], store=store)


def _report(tmp_path):
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


def _verdicts(report):
    return {entry["widgetId"]: entry["verdict"] for entry in report["widgets"]}


def _stdout(capsys):
    return capsys.readouterr().out.splitlines()


SAVED = _bar(["East", "North"], [10.5, 20])


# The brief's four


def test_dryRunWritesNothing(tmp_path, monkeypatch, capsys):
    fakes = _Fakes(monkeypatch, plans={"Revenue by region": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    config = _config(_widget("w1", SAVED, query="Revenue by region"))
    store = _store(config)
    before = dict(store.files)

    assert _run(tmp_path, store) == 0

    assert store.writes() == [] and store.files == before
    report = _report(tmp_path)
    assert report["mode"] == "dryRun" and report["backup"] is None and report["written"] == 0
    [entry] = report["widgets"]
    assert entry["verdict"] == "match" and entry["page"] == "page-1" and entry["widgetId"] == "w1"
    # The report carries the request a write would store, with the saved title.
    assert entry["chartRequest"]["title"] == "t" and decodeGeneratedCode(entry["generatedCode"]).sql == "SELECT 1"
    assert report["counts"] == {"widgets": 1, "candidates": 1, "match": 1, "differ": 0, "failed": 0, "skipped": 0}
    assert _stdout(capsys) == ["widgets 1", "candidates 1", "match 1", "differ 0", "failed 0", "skipped 0", "written 0", "backup none"]
    # The run was unfiltered and lenient, like a dashboard refresh of a saved chart request.
    assert fakes.renders[0]["filters"] is None and fakes.renders[0]["lenient"] is True and fakes.renders[0]["waitForSlot"] is True


def test_onlyExactMatchesAreReplaced(tmp_path, monkeypatch, capsys):
    charts = {
        "exact": SAVED,
        "close": _bar(["East", "North"], [10.5 * (1 + 1e-12), 20]),
        "value": _bar(["East", "North"], [10.5, 21]),
        "order": _bar(["North", "East"], [20, 10.5]),
        "legend": _bar(["East", "North"], [10.5, 20], datasetLabel="Revenue"),
        "axis": _bar(["East", "North"], [10.5, 20], xLabels="Sales Region"),
    }
    plans = {f"q-{name}": _request(name) for name in charts}
    plans["q-fails"] = ("check", {"finalOutput": {"error": "The query names an unknown table."}, "generatedCode": "{}"})
    plans["q-busy-run"] = _request("busy")
    charts["busy"] = SandboxBusyError(BUSY_MESSAGE)
    _Fakes(monkeypatch, plans=plans, charts=charts)
    widgets = [_widget(f"w-{prompt[2:]}", SAVED, query=prompt) for prompt in plans]
    config = _config(*widgets, more={"page-2": {"name": "Other", "widgets": [_widget("w-panel", SAVED, code=PANEL)]}})
    store = _store(config)

    assert _run(tmp_path, store, "--write") == 0

    report = _report(tmp_path)
    assert _verdicts(report) == {
        "w-exact": "match", "w-close": "match", "w-value": "differ", "w-order": "differ", "w-legend": "differ",
        "w-axis": "differ", "w-fails": "failed", "w-busy-run": "failed", "w-panel": "skipped",
    }
    fields = {entry["widgetId"]: entry.get("field") for entry in report["widgets"] if entry["verdict"] == "differ"}
    assert fields == {"w-value": "data.datasets[0].data[1]", "w-order": "data.labels[0]",
                      "w-legend": "data.datasets[0].label", "w-axis": "xLabels"}
    stages = {entry["widgetId"]: (entry["step"], entry["stage"]) for entry in report["widgets"] if entry["verdict"] == "failed"}
    assert stages == {"w-fails": ("generate", "check"), "w-busy-run": ("render", "busy")}
    # A failed or differing widget carries no request for review to mistake for a conversion.
    assert all("generatedCode" not in entry for entry in report["widgets"] if entry["verdict"] != "match")

    after = json.loads(store.files[CONFIG])
    changed = _changedGeneratedCode(config, after)
    assert changed == {"w-exact", "w-close"}
    for page in after.values():
        for widget in page["widgets"]:
            if widget["id"] in changed:
                request = decodeGeneratedCode(widget["generatedCode"])
                assert request.sql == widget["id"][2:] and request.title == "t"
    # The service's own JSON settings.
    assert store.files[CONFIG] == json.dumps(after, indent=4).encode("utf-8")
    assert report["written"] == 2 and report["backup"] == BACKUP
    assert _stdout(capsys)[-2:] == ["written 2", f"backup {BACKUP}"]


def test_backupBeforeWrite(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]

    assert _run(tmp_path, store, "--write") == 0

    steps = [entry for entry in store.log if entry != ("read", "metadata.json")]
    assert steps == [
        ("read", CONFIG),
        ("write", BACKUP, False),
        ("read", BACKUP),
        ("read", CONFIG),
        ("write", CONFIG, True),
        ("read", CONFIG),
    ]
    assert store.files[BACKUP] == original and store.files[CONFIG] != original
    assert _report(tmp_path)["outcome"] == "written"


def test_panelAndFailureWidgetsAreSkipped(tmp_path, monkeypatch):
    request = _request("SELECT 1")
    fakes = _Fakes(monkeypatch, plans={"q": request}, charts={"SELECT 1": SAVED})
    with open(Path(conv.__file__).resolve().parent.parent / "codeTemplates.yaml", encoding="utf-8") as handle:
        import yaml

        templates = yaml.safe_load(handle)
    import string

    template = string.Template(templates["panelChartWithoutBlend"]).substitute(
        projectId=PROJECT, chartType="bar", xAxis="region", yAxis="revenue", aggregationMetric="sum",
        dataSourceName="orders", tablesUsed="orders", index=None, columns=None, values=None, selectedColumns=None,
        mapType="None", isFilterApplied=False, filters=None, geoCodeColumn="None",
    )
    failedCard = {"chartType": "card", "title": "Chart: q", "label": "Status", "xLabels": "Status", "yLabels": "Value", "data": "Error/No data"}
    widgets = [
        _widget("w-template", SAVED, code=template),
        _widget("w-panel", SAVED, code=PANEL),
        _widget("w-failed", failedCard, code="# Failed to generate code for: q\n# Error: boom"),
        _widget("w-failed-indented", failedCard, code="\n  # Failed to generate code for: q"),
        _widget("w-empty", SAVED, code="   "),
        _widget("w-none", SAVED, code=None),
        _widget("w-request", SAVED, code=encodeGeneratedCode(request)),
        _widget("w-bad-version", SAVED, code='{"nubrixChart": 9, "chartType": "bar"}'),
        "not a widget",
        _widget("w-python", SAVED, query="q"),
    ]
    store = _store(_config(*widgets))
    assert _run(tmp_path, store, "--write") == 0

    report = _report(tmp_path)
    reasons = {entry["widgetId"]: entry.get("reason") for entry in report["widgets"] if entry["verdict"] == "skipped"}
    assert reasons == {
        "w-template": "panelChart", "w-panel": "panelChart", "w-failed": "failedCard", "w-failed-indented": "failedCard",
        "w-empty": "emptyCode", "w-none": "emptyCode", "w-request": "chartRequest", "w-bad-version": "chartRequest",
        None: "notAWidget",
    }
    assert _verdicts(report)["w-python"] == "match"
    # Only the Python widget went to the model.
    assert fakes.prompts == ["q"]
    assert report["counts"]["skipped"] == 9 and report["counts"]["candidates"] == 1
    after = json.loads(store.files[CONFIG])
    assert _changedGeneratedCode(_config(*widgets), after) == {"w-python"}


# The write: only the matches' generatedCode changes, and every abort leaves the live config alone


def _changedGeneratedCode(before: dict, after: dict) -> set:
    """The ids of the widgets whose generatedCode differs; fails if anything else in the config differs."""
    assert list(before) == list(after)
    changed = set()
    for pageId in before:
        pageBefore, pageAfter = before[pageId], after[pageId]
        assert {k: v for k, v in pageBefore.items() if k != "widgets"} == {k: v for k, v in pageAfter.items() if k != "widgets"}
        assert len(pageBefore.get("widgets", [])) == len(pageAfter.get("widgets", []))
        for old, new in zip(pageBefore.get("widgets", []), pageAfter.get("widgets", [])):
            if not isinstance(old, dict):
                assert old == new
                continue
            assert list(old) == list(new)
            assert {k: v for k, v in old.items() if k != "generatedCode"} == {k: v for k, v in new.items() if k != "generatedCode"}
            if old.get("generatedCode") != new.get("generatedCode"):
                changed.add(old["id"])
    return changed


def test_theWriteChangesOnlyTheMatchedGeneratedCode(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1"), "other": _request("SELECT 2")},
           charts={"SELECT 1": SAVED, "SELECT 2": _bar(["x"], [1])})
    # Keys the service never writes, unicode, a float that only repr keeps, and a page without widgets all survive.
    config = _config(
        _widget("w1", SAVED, query="q", extraKey={"nested": [1, 2.000000000000001, None, True]}),
        _widget("w2", SAVED, query="other"),
        more={"page-2": {"name": "Über café", "widgets": []}, "page-3": {"name": "No widgets"}},
    )
    store = _store(config)
    assert _run(tmp_path, store, "--write") == 0
    after = json.loads(store.files[CONFIG])
    assert _changedGeneratedCode(config, after) == {"w1"}
    expected = copy.deepcopy(config)
    expected["page-1"]["widgets"][0]["generatedCode"] = after["page-1"]["widgets"][0]["generatedCode"]
    assert store.files[CONFIG] == json.dumps(expected, indent=4).encode("utf-8")


def test_aConcurrentEditAbortsWithoutWriting(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]
    moved = _widget("w1", SAVED, query="q")
    moved["layout"] = {"x": 6, "y": 0, "w": 6, "h": 10}
    edited = _bytes(_config(moved))
    store.hooks[(CONFIG, 2)] = lambda: edited

    assert _run(tmp_path, store, "--write") == 1

    assert ("write", CONFIG, True) not in store.log and store.files[CONFIG] == original
    report = _report(tmp_path)
    assert report["outcome"] == "aborted" and report["abortReason"] == "concurrentEdit" and report["written"] == 0
    # The backup was made and is named, so nothing is lost either way.
    assert report["backup"] == BACKUP and store.files[BACKUP] == original
    out = capsys.readouterr()
    assert out.out.splitlines()[-2:] == ["written 0", f"backup {BACKUP}"]
    assert "concurrent" in out.err


def test_aConfigThatCannotBeReadAgainAborts(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]

    def gone():
        raise RuntimeError("storage down")

    store.hooks[(CONFIG, 2)] = gone
    assert _run(tmp_path, store, "--write") == 1
    assert ("write", CONFIG, True) not in store.log and store.files[CONFIG] == original
    assert _report(tmp_path)["abortReason"] == "concurrentEdit"


@pytest.mark.parametrize("failure", ["upload", "readBack", "mismatch"])
def test_aBackupThatCannotBeVerifiedAborts(tmp_path, monkeypatch, failure):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]
    if failure == "upload":
        store.refuse.add(BACKUP)
    elif failure == "readBack":
        def broken():
            raise RuntimeError("not found")

        store.hooks[(BACKUP, 1)] = broken
    else:
        store.hooks[(BACKUP, 1)] = lambda: original + b" "

    assert _run(tmp_path, store, "--write") == 1

    assert ("write", CONFIG, True) not in store.log and store.files[CONFIG] == original
    # The live config was never read again: nothing goes on after a failed backup.
    assert store.log.count(("read", CONFIG)) == 1
    report = _report(tmp_path)
    assert report["outcome"] == "aborted" and report["abortReason"] == "backupNotVerified" and report["written"] == 0


@pytest.mark.parametrize("failure", ["upload", "mismatch"])
def test_aWriteThatCannotBeVerifiedExitsOne(tmp_path, monkeypatch, failure):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    if failure == "upload":
        store.refuse.add(CONFIG)
    else:
        store.hooks[(CONFIG, 3)] = lambda: b"{}"

    assert _run(tmp_path, store, "--write") == 1

    report = _report(tmp_path)
    assert report["outcome"] == "aborted" and report["abortReason"] == "writeNotVerified"
    assert report["backup"] == BACKUP and report["written"] == 0


def test_nothingIsWrittenWhenNothingMatched(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": _bar(["x"], [1])})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store, "--write") == 0
    assert store.writes() == []
    report = _report(tmp_path)
    assert report["outcome"] == "nothingToWrite" and report["backup"] is None
    assert _stdout(capsys)[-2:] == ["written 0", "backup none"]


def test_aConfigThatCannotBeReadAtTheStartExitsOneAndWritesNothing(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch)
    store = FakeStore({"metadata.json": b"{}"})
    assert _run(tmp_path, store, "--write") == 1
    assert store.writes() == []
    assert _report(tmp_path)["abortReason"] == "configUnreadable"


# Generation


def test_theStoredQueryIsTheDescriptionElseTheCode(tmp_path, monkeypatch):
    fakes = _Fakes(monkeypatch, plans={"Revenue by region": _request("SELECT 1"), PYTHON: _request("SELECT 1")},
                   charts={"SELECT 1": SAVED})
    store = _store(_config(_widget("w1", SAVED, query="Revenue by region"), _widget("w2", SAVED), _widget("w3", SAVED, query="  ")))
    assert _run(tmp_path, store) == 0
    assert fakes.prompts == ["Revenue by region", PYTHON, PYTHON]
    # The run is against the project's active tables, like the refresh of a saved chart request.
    assert fakes.renders[0]["activeTables"] == {"orders"} and fakes.renders[0]["projectId"] == PROJECT


def test_aDoubtAFailureAndAnExceptionAreReportedPerWidget(tmp_path, monkeypatch):
    plans = {
        "doubt": {"finalOutput": {"response": "Which period?"}, "rephrasedQuery": {"doubt": "Which period?"}},
        "timeout": ("timeout", {"finalOutput": {"error": "Execution timed out after 35 seconds."}}),
        "busy": ("busy", {"finalOutput": {"error": BUSY_MESSAGE}}),
        "boom": RuntimeError("model unreachable"),
        "renderFails": _request("bad"),
        "renderBreaks": _request("broken"),
        "ok": _request("SELECT 1"),
    }
    charts = {"bad": ChartFailure("timeout", "Execution timed out after 35 seconds."), "broken": OSError("disk"), "SELECT 1": SAVED}
    _Fakes(monkeypatch, plans=plans, charts=charts)
    store = _store(_config(*[_widget(name, SAVED, query=name) for name in plans]))
    assert _run(tmp_path, store, "--write") == 0
    entries = {entry["widgetId"]: entry for entry in _report(tmp_path)["widgets"]}
    assert {name: (entry["verdict"], entry.get("step"), entry.get("stage")) for name, entry in entries.items()} == {
        "doubt": ("failed", "generate", "doubt"),
        "timeout": ("failed", "generate", "timeout"),
        "busy": ("failed", "generate", "busy"),
        "boom": ("failed", "generate", "exception"),
        "renderFails": ("failed", "render", "timeout"),
        "renderBreaks": ("failed", "render", "run"),
        "ok": ("match", None, None),
    }
    assert "RuntimeError" in entries["boom"]["detail"] and "OSError" in entries["renderBreaks"]["detail"]
    after = json.loads(store.files[CONFIG])
    assert _changedGeneratedCode(_config(*[_widget(name, SAVED, query=name) for name in plans]), after) == {"ok"}


def test_theSavedTitleIsKeptAndATitleThatCannotBeKeptDiffers(tmp_path, monkeypatch):
    fakes = _Fakes(monkeypatch, plans={"q": _request("SELECT 1")}, charts={"SELECT 1": SAVED})
    widgets = [_widget("kept", SAVED, query="q"), _widget("none", SAVED, query="q"), _widget("long", SAVED, query="q")]
    widgets[1]["title"] = None
    widgets[2]["title"] = "x" * 201
    store = _store(_config(*widgets))
    assert _run(tmp_path, store) == 0
    entries = {entry["widgetId"]: entry for entry in _report(tmp_path)["widgets"]}
    assert entries["kept"]["verdict"] == "match" and entries["kept"]["chartRequest"]["title"] == "t"
    # A refresh shows the request's title, so a title a request cannot hold would change: no model call is spent on it.
    assert entries["none"]["verdict"] == "differ" and entries["none"]["field"] == "title"
    assert entries["long"]["verdict"] == "differ" and entries["long"]["field"] == "title"
    assert fakes.prompts == ["q"]


def test_noCreditsAreChargedAndTheTraceIsTheReportingOne(monkeypatch):
    """A real model run gets only the Langfuse handler: never the credit callback of a served request."""
    import utils.llm

    seen = {}

    class Workflow:
        def invoke(self, state, config=None):
            seen["config"] = config
            return {"finalOutput": {"response": "?"}}

    handler = object()
    monkeypatch.setattr(utils.llm, "getLangfuseConfig", lambda **kwargs: seen.setdefault("langfuse", kwargs) and {"callbacks": [handler]})
    engine = conv._Engine.__new__(conv._Engine)
    engine.engine, engine.replay, engine.failure, engine.knownMetadata = "sql", None, None, None
    from nubrix.workflows import sqlChartWorkflow

    engine.module, engine.workflow = sqlChartWorkflow, Workflow()
    engine.invoke({}, "q", PROJECT)
    assert seen["config"] == {"callbacks": [handler]}
    assert seen["langfuse"]["trace_name"] == "ReportingWorkflow" and "userId" not in seen["langfuse"]


# The strict comparison


def _table(rows, columns=None):
    chart = {"chartType": "table", "title": "t", "data": rows}
    if columns is not None:
        chart["columns"] = columns
    return chart


def _pivot(data):
    return {"chartType": "pivot", "title": "t", "data": data}


def _card(value, label="Orders"):
    return {"chartType": "card", "title": "t", "label": label, "data": value}


def _geo(points):
    return {"chartType": "geoMap", "title": "t", "map": {"mapType": "scatterMap", "data": {"points": points}}}


POINT_A = {"id": "0", "lat": 1.5, "long": 2.5, "label": "A"}
POINT_B = {"id": "1", "lat": 3.5, "long": 4.5, "label": "B"}


@pytest.mark.parametrize("saved,new,field", [
    (SAVED, SAVED, None),
    (SAVED, {**SAVED, "title": "other"}, None),
    (SAVED, _bar(["East", "North"], [10.5 * (1 + 5e-10), 20]), None),
    (SAVED, _bar(["East", "North"], [10.5 * (1 + 1e-8), 20]), "data.datasets[0].data[0]"),
    (_bar(["a"], [20]), _bar(["a"], [20.0]), None),
    (_bar(["a"], [20]), _bar(["a"], [20.000000001]), "data.datasets[0].data[0]"),
    (_bar(["a"], [1]), _bar(["a"], [True]), "data.datasets[0].data[0]"),
    (_bar(["a"], [True]), _bar(["a"], [True]), None),
    (_bar(["a"], [None]), _bar(["a"], [0]), "data.datasets[0].data[0]"),
    (_bar(["a"], [math.nan]), _bar(["a"], [math.nan]), "data.datasets[0].data[0]"),
    (_bar(["2024"], [1]), _bar([2024], [1]), "data.labels[0]"),
    (SAVED, _bar(["East", "North", "South"], [10.5, 20, 1]), "data.labels"),
    (SAVED, {**SAVED, "chartType": "line"}, "chartType"),
    (SAVED, {**SAVED, "yLabels": None}, "yLabels"),
    ({**SAVED, "label": None}, SAVED, None),
    ({**SAVED, "map": None, "columns": None}, SAVED, None),
    (SAVED, {**SAVED, "label": "x"}, "label"),
    (SAVED, {**SAVED, "colors": ["red"]}, None),
    ({**SAVED, "data": {**SAVED["data"], "datasets": [{"label": "Total Revenue", "data": [10.5, 20], "backgroundColor": "red"}]}},
     SAVED, "data.datasets[0]"),
    ({"chartType": "bar", "title": "t", "data": {"labels": ["a"], "datasets": [{"label": "x", "data": [1]}, {"label": "y", "data": [2]}]}},
     {"chartType": "bar", "title": "t", "data": {"labels": ["a"], "datasets": [{"label": "y", "data": [2]}, {"label": "x", "data": [1]}]}},
     "data.datasets[0].label"),
    (_table([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), _table([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], ["a", "b"]), None),
    (_table([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]), _table([{"a": 2, "b": "y"}, {"a": 1, "b": "x"}], ["a", "b"]), "data[0].a"),
    (_table([{"a": 1, "b": "x"}]), _table([{"b": "x", "a": 1}], ["b", "a"]), "columns[0]"),
    (_table([{"a": 1, "b": "x"}]), _table([{"a": 1, "c": "x"}], ["a", "c"]), "columns[1]"),
    (_table([{"a": 1, "b": "x"}], ["a", "b"]), _table([{"a": 1, "b": "x"}], ["a", "b"]), None),
    (_table([]), _table([], []), None),
    (_pivot({"c1": {"r1": 1, "r2": 2}}), _pivot({"c1": {"r1": 1, "r2": 2}}), None),
    (_pivot({"c1": {"r1": 1, "r2": 2}}), _pivot({"c1": {"r2": 2, "r1": 1}}), "data.c1"),
    (_pivot({"c1": {"r1": 1}, "c2": {"r1": 2}}), _pivot({"c2": {"r1": 2}, "c1": {"r1": 1}}), "data"),
    (_card(36.0), _card(36), None),
    (_card(36.46), _card(36.456), "data"),
    (_card(36), _card(36, label="Orders placed"), "label"),
    (_card("true"), _card(True), "data"),
    (_geo([POINT_A, POINT_B]), _geo([POINT_A, POINT_B]), None),
    (_geo([POINT_A, POINT_B]), _geo([POINT_B, POINT_A]), "map.data.points[0].id"),
    (_geo([{k: POINT_A[k] for k in ("lat", "long", "label", "id")}]), _geo([POINT_A]), None),
    (_geo([{**POINT_A, "label": ""}]), _geo([{**POINT_A, "label": "1.5, 2.5"}]), None),
    (_geo([{**POINT_A, "properties": {"city": "A"}}]), _geo([POINT_A]), "map.data.points[0]"),
])
def test_theStrictComparison(saved, new, field):
    assert conv.visibleDifference(saved, new) == field


def test_aMatchNeedsCompareChartsToo(monkeypatch):
    monkeypatch.setattr(conv, "compareCharts", lambda a, b, relTol: "differ")
    assert conv.verdictOf(SAVED, SAVED) == ("differ", "compareCharts")
    monkeypatch.undo()
    assert conv.verdictOf(SAVED, SAVED) == ("match", None)


def test_compareChartsIsCalledWithTheStrictTolerance(monkeypatch):
    calls = []
    monkeypatch.setattr(conv, "compareCharts", lambda a, b, relTol: calls.append(relTol) or "match")
    conv.verdictOf(SAVED, SAVED)
    assert calls == [1e-9]


# Usage


@pytest.mark.parametrize("argv", [
    [],
    ["--project", PROJECT],
    ["--project", "../other", "--out", "{out}"],
    ["--project", PROJECT, "--out", "{missing}"],
    ["--project", PROJECT, "--out", "{out}", "--model-stub", "{badStub}"],
    ["--project", PROJECT, "--out", "{out}", "--model-stub", "{noStub}"],
])
def test_usageErrorsExitTwoAndTouchNothing(tmp_path, monkeypatch, argv):
    _Fakes(monkeypatch)
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    paths = {"out": str(tmp_path / "report.json"), "missing": str(tmp_path / "no" / "report.json"),
             "badStub": str(bad), "noStub": str(tmp_path / "absent.jsonl")}
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert conv.main([arg.format(**paths) for arg in argv], store=store) == 2
    assert store.log == []


def test_stdoutCarriesNoDataValues(tmp_path, monkeypatch, capsys):
    secret = "Secret Region"
    _Fakes(monkeypatch, plans={f"q {secret}": _request(f"SELECT '{secret}'")},
           charts={f"SELECT '{secret}'": _bar([secret], [1])})
    store = _store(_config(_widget("w1", _bar([secret], [2]), query=f"q {secret}")))
    assert _run(tmp_path, store, "--write") == 0
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err and "orders" not in out.out


# The real sandbox, on the synthetic chart evaluation tables


def _stubFor(tmp_path, replies: dict[str, str]) -> Path:
    """A model stub keyed by widget id, holding the recorded replies of the chart evaluation cases."""
    recorded = {json.loads(line)["id"]: json.loads(line) for line in STUB.read_text(encoding="utf-8").splitlines() if line.strip()}
    path = tmp_path / "stub.jsonl"
    path.write_text("".join(json.dumps({**recorded[case], "id": widgetId}) + "\n" for widgetId, case in replies.items()), encoding="utf-8")
    return path


BAR_SAVED = {
    "chartType": "bar", "title": "Revenue by region (saved)", "label": None, "xLabels": "Region", "yLabels": "Total Revenue",
    "data": {"labels": ["East", "North", "South", "West"], "datasets": [{"label": "Total Revenue", "data": [1407.75, 1070.25, 993.0, 1636.5]}]},
}
CARD_SAVED = {"chartType": "card", "title": "Orders", "label": "Orders placed", "xLabels": None, "yLabels": None, "data": 36.0}


def _syntheticStore():
    files = {"metadata.json": (DATA / PROJECT / "metadata.json").read_bytes()}
    wrong = copy.deepcopy(BAR_SAVED)
    wrong["data"]["datasets"][0]["data"][3] = 1636.0
    config = _config(
        _widget("w-bar", BAR_SAVED, query="Bar chart of total revenue by region"),
        _widget("w-card", CARD_SAVED, query="How many orders were placed?"),
        _widget("w-wrong", wrong, query="Bar chart of total revenue by region"),
        _widget("w-panel", BAR_SAVED, code=PANEL),
        _widget("w-failed", CARD_SAVED, code="# Failed to generate code for: q\n# Error: boom"),
    )
    files[CONFIG] = _bytes(config)
    return FakeStore(files), config


@pytest.fixture
def synthetic(monkeypatch):
    monkeypatch.setenv("CHART_EVAL_STORAGE_KEY", FIXTURE_KEY)
    monkeypatch.setattr(conv, "_utcNow", lambda: NOW)
    with chartEval._environment(True, str(DATA)):
        yield


def test_endToEndDryRunOnTheSyntheticTables(tmp_path, synthetic, capsys):
    store, _ = _syntheticStore()
    before = dict(store.files)
    stub = _stubFor(tmp_path, {"w-bar": "bar-region", "w-card": "card-order-count", "w-wrong": "bar-region"})
    assert _run(tmp_path, store, "--model-stub", str(stub)) == 0
    assert store.writes() == [] and store.files == before
    report = _report(tmp_path)
    assert _verdicts(report) == {"w-bar": "match", "w-card": "match", "w-wrong": "differ", "w-panel": "skipped", "w-failed": "skipped"}
    entries = {entry["widgetId"]: entry for entry in report["widgets"]}
    assert entries["w-wrong"]["field"] == "data.datasets[0].data[3]"
    assert entries["w-bar"]["chartRequest"]["title"] == "Revenue by region (saved)"
    assert _stdout(capsys) == ["widgets 5", "candidates 3", "match 2", "differ 1", "failed 0", "skipped 2", "written 0", "backup none"]


def test_endToEndWriteOnTheSyntheticTables(tmp_path, synthetic):
    store, config = _syntheticStore()
    stub = _stubFor(tmp_path, {"w-bar": "bar-region", "w-card": "card-order-count", "w-wrong": "bar-region"})
    assert _run(tmp_path, store, "--write", "--model-stub", str(stub)) == 0
    assert store.files[BACKUP] == _bytes(config)
    after = json.loads(store.files[CONFIG])
    assert _changedGeneratedCode(config, after) == {"w-bar", "w-card"}
    converted = {widget["id"]: decodeGeneratedCode(widget["generatedCode"]) for widget in after["page-1"]["widgets"] if widget["id"] in ("w-bar", "w-card")}
    assert converted["w-bar"].title == "Revenue by region (saved)" and converted["w-card"].chartType == "card"
    assert _report(tmp_path)["outcome"] == "written"
