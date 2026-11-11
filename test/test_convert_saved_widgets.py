"""The verified conversion of saved dashboard widgets (`scripts/convertSavedWidgets.py`).

Most tests replace the model, the workflow, the chart runs and the filtered re-runs with fakes and use an in-memory
storage client, so they are fast. Two tests run the real sandbox on the synthetic chart evaluation tables, with
recorded model replies and saved Python that really runs: one dry run (which also shows a filter spot check catching
a widget that only agrees unfiltered) and one write. Everything is synthetic.
"""

import copy
import datetime as dt
import json
import math
from pathlib import Path
from types import SimpleNamespace

import httpx
import pyarrow as pa
import pytest

from nubrix.components.chartEngine import ChartFailure
from nubrix.components.chartRequest import LABEL_CAP, ChartRequest, decodeGeneratedCode, encodeGeneratedCode
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
PANEL = (
    "```python\ndef panelChart(\n    projectId: str,\n    chartType: str,\n    **kwargs\n    ):\n    print(1)\n"
    "panelChart(\n    projectId = \"p\",\n    chartType = \"bar\",\n    xAxis = \"region\",\n)\n```"
)
CHART_KEYS = ("chartType", "title", "xLabels", "yLabels", "label", "data", "map", "columns")


def _python(title):
    """Saved model-written Python whose title is a literal, as automatic dashboards save it."""
    return f"import json\ndf = fetch_data_pl('{PROJECT}', 'orders')\nprint(json.dumps({{'title': '{title}'}}))"


# Fakes


class FakeStore:
    """The project's folder in the bucket. `hooks[(name, n)]` answers the n-th read of `name` instead of the file;
    `modifiedAt` lists the answers of the project's modifiedAt reads (an exception is raised)."""

    def __init__(self, files: dict[str, bytes]):
        self.files = dict(files)
        self.log: list[tuple] = []
        self.hooks: dict = {}
        self.refuse: set[str] = set()
        self.modifiedAt: list = ["2026-11-10T08:00:00+00:00"]

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

    def projectModifiedAt(self):
        self.log.append(("modifiedAt",))
        value = self.modifiedAt[0] if len(self.modifiedAt) == 1 else self.modifiedAt.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def writes(self) -> list:
        return [entry for entry in self.log if entry[0] == "write"]


def _bar(labels, values, datasetLabel="Total Revenue", xLabels="Region", yLabels="Total Revenue", title="t", chartType="bar"):
    return {"chartType": chartType, "title": title, "xLabels": xLabels, "yLabels": yLabels,
            "data": {"labels": list(labels), "datasets": [{"label": datasetLabel, "data": list(values)}]}}


def _sql(name):
    return f"SELECT region, SUM(revenue) AS revenue FROM orders WHERE tag = '{name}' GROUP BY region ORDER BY region"


def _request(name, chartType="bar", title="Model title", sql=None):
    encoding = {"x": "region", "y": ["revenue"]} if chartType != "card" else {"value": "n", "label": "Orders"}
    return ChartRequest.model_validate({"chartType": chartType, "title": title, "sql": sql or _sql(name), "encoding": encoding})


def _widget(widgetId, chart, code=None, query=None, **extra):
    widget = {"id": widgetId, "chartType": chart.get("chartType"), "title": chart.get("title"), "label": chart.get("label"),
              "xLabels": chart.get("xLabels"), "yLabels": chart.get("yLabels"), "data": chart.get("data")}
    if "map" in chart:
        widget["map"] = chart["map"]
    if query is not None:
        widget["query"] = query
    widget.update(layout={"x": 0, "y": 0, "w": 6, "h": 10}, generatedCode=_python(chart.get("title")) if code is None else code, **extra)
    return widget


def _config(*widgets, page="page-1", more=None):
    config = {page: {"name": "Sales", "isAutomatic": True, "widgets": list(widgets)}}
    config.update(more or {})
    return config


def _bytes(config) -> bytes:
    return json.dumps(config, indent=4).encode("utf-8")


def _store(config):
    return FakeStore({CONFIG: _bytes(config), "metadata.json": json.dumps(METADATA).encode("utf-8")})


def _rowsOf(chart):
    """The query result a chart was built from: region and revenue for a category chart, one value for a card."""
    if chart.get("chartType") == "card":
        return [{"n": chart["data"]}]
    data = chart.get("data")
    if isinstance(data, dict) and "labels" in data:
        return [{"region": label, "revenue": value} for label, value in zip(data["labels"], data["datasets"][0]["data"])]
    return []


def _visible(widget):
    return {key: copy.deepcopy(widget[key]) for key in CHART_KEYS if key in widget}


class _Fakes:
    """The workflow, the runs and the filtered re-runs.

    - `plans[prompt]`: what the workflow makes of a description: a ChartRequest, `(stage, response)` for a failure
      the workflow logged, a response dict, or an exception.
    - `charts[sql]`: what the refresh-style run returns (a chart or an exception); `finals[sql]` what the workflow's
      own run returned (default: the same chart); `rows[sql]` and `truncated[sql]` the query result behind it.
    - `filterSets`: the filter sets the spot check uses; `filtered(form, widget, filters)` what a filtered re-run of
      the saved Python ("python") or of the request ("sql") shows: a chart, or None for a failed run. By default both
      show the saved chart.
    """

    def __init__(self, monkeypatch, plans=None, charts=None, finals=None, rows=None, truncated=None, filterSets=None, filtered=None):
        self.plans = plans or {}
        self.charts = charts or {}
        self.finals = finals or {}
        self.rows = rows or {}
        self.truncated = truncated or {}
        self.filterSets = [[{"orders.region": "East"}]] if filterSets is None else filterSets
        self.filtered = filtered or (lambda form, widget, filters: _visible(widget))
        self.prompts: list[str] = []
        self.renders: list[dict] = []
        self.reruns: list[tuple] = []
        self.configs: list = []
        fakes = self

        class Engine:
            def __init__(self, replay):
                self.failure = None
                self.replay = replay

            def invoke(self, metadata, prompt, project):
                fakes.prompts.append(prompt)
                self.failure = None
                plan = fakes.plans[prompt]
                if isinstance(plan, Exception):
                    raise plan
                if isinstance(plan, ChartRequest):
                    chart = fakes.finals.get(plan.sql, fakes.charts.get(plan.sql))
                    output = copy.deepcopy(chart) if isinstance(chart, dict) else {"chartType": plan.chartType}
                    output["title"] = plan.title
                    return {"finalOutput": output, "generatedCode": encodeGeneratedCode(plan)}
                if isinstance(plan, tuple):
                    stage, response = plan
                    self.failure = {"stage": stage, "error": "detail", "repairs": 2}
                    return response
                return plan

        def render(manager, projectId, request, activeTables):
            fakes.renders.append({"sql": request.sql, "title": request.title, "activeTables": set(activeTables), "projectId": projectId})
            chart = fakes.charts[request.sql]
            if isinstance(chart, Exception):
                raise chart
            table = pa.Table.from_pylist(fakes.rows.get(request.sql, _rowsOf(chart)))
            return {**copy.deepcopy(chart), "title": request.title}, table, fakes.truncated.get(request.sql, False)

        def rerun(widget, filters, projectId, activeTables):
            form = "sql" if decodeGeneratedCode(widget.get("generatedCode")) is not None else "python"
            fakes.reruns.append((form, widget.get("id"), json.dumps(filters)))
            shown = fakes.filtered(form, widget, filters)
            if shown is None:
                return copy.deepcopy(widget), None
            fresh = {**copy.deepcopy(widget), **shown}
            return fresh, shown

        monkeypatch.setattr(conv, "_Engine", Engine)
        monkeypatch.setattr(conv, "_renderWithResult", render)
        monkeypatch.setattr(conv, "_filterSets", lambda job, pageId, widget, request: copy.deepcopy(fakes.filterSets))
        monkeypatch.setattr(conv, "_rerunUnderFilter", rerun)
        monkeypatch.setattr(conv, "_utcNow", lambda: NOW)


def _run(tmp_path, store, *extra):
    return conv.main(["--project", PROJECT, "--out", str(tmp_path / "report.json"), *extra], store=store)


def _report(tmp_path):
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))


def _verdicts(report):
    return {entry["widgetId"]: entry["verdict"] for entry in report["widgets"]}


def _entries(tmp_path):
    return {entry["widgetId"]: entry for entry in _report(tmp_path)["widgets"]}


def _stdout(capsys):
    return capsys.readouterr().out.splitlines()


SAVED = _bar(["East", "North"], [10.5, 20])


# The brief's four


def test_dryRunWritesNothing(tmp_path, monkeypatch, capsys):
    fakes = _Fakes(monkeypatch, plans={"Revenue by region": _request("one")}, charts={_sql("one"): SAVED})
    config = _config(_widget("w1", SAVED, query="Revenue by region"))
    store = _store(config)
    before = dict(store.files)

    assert _run(tmp_path, store) == 0

    assert store.writes() == [] and store.files == before
    # A dry run never reads the project row either.
    assert ("modifiedAt",) not in store.log
    report = _report(tmp_path)
    assert report["mode"] == "dryRun" and report["backup"] is None and report["written"] == 0
    [entry] = report["widgets"]
    assert entry["verdict"] == "match" and entry["page"] == "page-1" and entry["widgetId"] == "w1"
    assert entry["filtersChecked"] == 1
    # The report carries the request a write would store, with the saved title.
    assert entry["chartRequest"]["title"] == "t" and decodeGeneratedCode(entry["generatedCode"]).sql == _sql("one")
    assert report["counts"] == {"widgets": 1, "candidates": 1, "match": 1, "differ": 0, "failed": 0, "skipped": 0, "filtersChecked": 1}
    assert _stdout(capsys) == ["widgets 1", "candidates 1", "match 1", "differ 0", "failed 0", "skipped 0",
                               "filters 1", "written 0", "backup none"]
    # The Python and the request both re-ran under the filter, once each.
    assert [(form, widgetId) for form, widgetId, _ in fakes.reruns] == [("python", "w1"), ("sql", "w1")]


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
    sqlCharts = {_sql(name): chart for name, chart in charts.items()}
    sqlCharts[_sql("busy")] = SandboxBusyError(BUSY_MESSAGE)
    _Fakes(monkeypatch, plans=plans, charts=sqlCharts)
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
                assert request.sql == _sql(widget["id"][2:]) and request.title == "t"
    # The service's own JSON settings.
    assert store.files[CONFIG] == json.dumps(after, indent=4).encode("utf-8")
    assert report["written"] == 2 and report["backup"] == BACKUP
    assert _stdout(capsys)[-2:] == ["written 2", f"backup {BACKUP}"]


def test_backupBeforeWrite(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]

    assert _run(tmp_path, store, "--write") == 0

    steps = [entry for entry in store.log if entry != ("read", "metadata.json")]
    assert steps == [
        ("modifiedAt",),
        ("read", CONFIG),
        ("write", BACKUP, False),
        ("read", BACKUP),
        ("read", CONFIG),
        ("write", CONFIG, True),
        ("read", CONFIG),
        ("modifiedAt",),
    ]
    assert store.files[BACKUP] == original and store.files[CONFIG] != original
    report = _report(tmp_path)
    assert report["outcome"] == "written" and report["modifiedAtCheck"] == "unchanged"


def test_panelAndFailureWidgetsAreSkipped(tmp_path, monkeypatch):
    request = _request("one")
    fakes = _Fakes(monkeypatch, plans={"q": request}, charts={_sql("one"): SAVED})
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
        # Code that no longer parses still names the template it came from: the regex fallback.
        _widget("w-panel-broken", SAVED, code="def panelChart(projectId,\n    chartType\nprint(("),
        _widget("w-failed", failedCard, code="# Failed to generate code for: q\n# Error: boom"),
        _widget("w-failed-indented", failedCard, code="\n  # Failed to generate code for: q"),
        _widget("w-empty", SAVED, code="   "),
        _widget("w-none", SAVED, code=None, generatedCodeNone=True),
        _widget("w-request", SAVED, code=encodeGeneratedCode(request)),
        _widget("w-bad-version", SAVED, code='{"nubrixChart": 9, "chartType": "bar"}'),
        "not a widget",
        _widget("w-python", SAVED, query="q"),
    ]
    widgets[6]["generatedCode"] = None
    del widgets[6]["generatedCodeNone"]
    store = _store(_config(*widgets))
    assert _run(tmp_path, store, "--write") == 0

    report = _report(tmp_path)
    reasons = {entry["widgetId"]: entry.get("reason") for entry in report["widgets"] if entry["verdict"] == "skipped"}
    assert reasons == {
        "w-template": "panelChart", "w-panel": "panelChart", "w-panel-broken": "panelChart", "w-failed": "failedCard",
        "w-failed-indented": "failedCard", "w-empty": "emptyCode", "w-none": "emptyCode", "w-request": "chartRequest",
        "w-bad-version": "chartRequest", None: "notAWidget",
    }
    assert _verdicts(report)["w-python"] == "match"
    # Only the Python widget went to the model.
    assert fakes.prompts == ["q"]
    assert report["counts"]["skipped"] == 10 and report["counts"]["candidates"] == 1
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
    _Fakes(monkeypatch, plans={"q": _request("one"), "other": _request("two")},
           charts={_sql("one"): SAVED, _sql("two"): _bar(["x"], [1])})
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
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
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
    # The backup was verified before the check, so it is named, and it holds the original.
    assert report["backup"] == BACKUP and store.files[BACKUP] == original
    out = capsys.readouterr()
    assert out.out.splitlines()[-2:] == ["written 0", f"backup {BACKUP}"]
    assert "concurrent" in out.err


def test_aConfigThatCannotBeReadAgainAborts(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]

    def gone():
        raise RuntimeError("storage down")

    store.hooks[(CONFIG, 2)] = gone
    assert _run(tmp_path, store, "--write") == 1
    assert ("write", CONFIG, True) not in store.log and store.files[CONFIG] == original
    assert _report(tmp_path)["abortReason"] == "concurrentEdit"


@pytest.mark.parametrize("failure", ["upload", "readBack", "mismatch"])
def test_aBackupThatCannotBeVerifiedAborts(tmp_path, monkeypatch, capsys, failure):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
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
    # An unverified backup is never named as the backup: it may not exist, or belong to another run.
    assert report["backup"] is None and report["backupAttempted"] == BACKUP
    assert _stdout(capsys)[-1] == "backup none"


@pytest.mark.parametrize("failure,live,outcome,written", [
    ("upload", "original", "writeNotApplied", 0),
    ("readBack", "new", "writeLanded", 1),
    ("mismatch", "other", "changedBySomeoneElse", 0),
    ("mismatch", "unreadable", "unreadable", 0),
])
def test_aWriteThatCannotBeVerifiedIsClassifiedAndNeverRestored(tmp_path, monkeypatch, capsys, failure, live, outcome, written):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    original = store.files[CONFIG]
    if failure == "upload":
        store.refuse.add(CONFIG)
    elif failure == "readBack":
        def broken():
            raise RuntimeError("read timed out")

        store.hooks[(CONFIG, 3)] = broken
    else:
        store.hooks[(CONFIG, 3)] = lambda: b"{}"
    # The one classifying re-read after the failure.
    readNumber = 3 if failure == "upload" else 4
    if live == "other":
        store.hooks[(CONFIG, readNumber)] = lambda: b'{"edited": true}'
    elif live == "unreadable":
        def down():
            raise RuntimeError("down")

        store.hooks[(CONFIG, readNumber)] = down

    assert _run(tmp_path, store, "--write") == 1

    report = _report(tmp_path)
    assert report["outcome"] == "aborted" and report["abortReason"] == "writeNotVerified"
    assert report["writeOutcome"] == outcome and report["written"] == written
    assert report["backup"] == BACKUP and store.files[BACKUP] == original
    # No restore: the config is written at most once, and the backup is never copied back.
    assert [entry for entry in store.writes() if entry[1] == CONFIG] == [("write", CONFIG, True)]
    assert store.log.count(("read", CONFIG)) == readNumber
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == f"backup {BACKUP}"
    assert outcome in out.err and BACKUP in out.err


def test_theProjectChangingDuringTheJobIsReported(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    store.modifiedAt = ["2026-11-10T08:00:00+00:00", "2026-11-11T09:00:01+00:00"]

    assert _run(tmp_path, store, "--write") == 1

    report = _report(tmp_path)
    assert report["outcome"] == "written" and report["abortReason"] == "possibleConcurrentEdit"
    assert report["modifiedAtCheck"] == "changed" and report["written"] == 1 and report["backup"] == BACKUP
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == f"backup {BACKUP}" and "possibleConcurrentEdit" in out.err and BACKUP in out.err


def test_aProjectRowThatCannotBeReadAbortsBeforeAnything(tmp_path, monkeypatch):
    fakes = _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    store.modifiedAt = [RuntimeError("database down")]
    assert _run(tmp_path, store, "--write") == 1
    assert store.log == [("modifiedAt",)] and fakes.prompts == []
    assert _report(tmp_path)["abortReason"] == "modifiedAtUnreadable"


def test_nothingIsWrittenWhenNothingMatched(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): _bar(["x"], [1])})
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


def test_anUnexpectedErrorAbortsWithItsClassAndWritesNothing(tmp_path, monkeypatch, capsys):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    store.files["metadata.json"] = b"not json"
    assert _run(tmp_path, store, "--write") == 1
    assert store.writes() == []
    report = _report(tmp_path)
    assert report["outcome"] == "aborted" and report["abortReason"] == "error:JSONDecodeError" and report["abortDetail"]
    out = capsys.readouterr()
    assert out.out.splitlines()[-1] == "backup none" and "was not changed" in out.err


# Generation


def test_theStoredQueryIsTheDescriptionElseTheCode(tmp_path, monkeypatch):
    code = _python("t")
    fakes = _Fakes(monkeypatch, plans={"Revenue by region": _request("one"), code: _request("one")}, charts={_sql("one"): SAVED})
    store = _store(_config(_widget("w1", SAVED, query="Revenue by region"), _widget("w2", SAVED), _widget("w3", SAVED, query="  ")))
    assert _run(tmp_path, store) == 0
    assert fakes.prompts == ["Revenue by region", code, code]
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
        "ok": _request("one"),
    }
    charts = {_sql("bad"): ChartFailure("timeout", "Execution timed out after 35 seconds."), _sql("broken"): OSError("disk"),
              _sql("one"): SAVED}
    _Fakes(monkeypatch, plans=plans, charts=charts)
    widgets = [_widget(name, SAVED, query=name) for name in plans]
    store = _store(_config(*widgets))
    assert _run(tmp_path, store, "--write") == 0
    entries = _entries(tmp_path)
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
    assert _changedGeneratedCode(_config(*widgets), after) == {"ok"}


def test_aTitleThatCannotBeKeptDiffersBeforeAnyModelCall(tmp_path, monkeypatch):
    fakes = _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    widgets = [_widget("kept", SAVED, query="q"), _widget("none", SAVED, query="q"), _widget("long", SAVED, query="q"),
               _widget("dynamic", SAVED, query="q", code="title = f'Sales {year}'\nprint(title)")]
    widgets[1]["title"] = None
    widgets[2]["title"] = "x" * 201
    store = _store(_config(*widgets))
    assert _run(tmp_path, store) == 0
    entries = _entries(tmp_path)
    assert entries["kept"]["verdict"] == "match" and entries["kept"]["chartRequest"]["title"] == "t"
    # A refresh shows the request's title, so a title a request cannot hold, or one the Python builds at run time,
    # would change: no model call is spent on those.
    assert {name: (entries[name]["verdict"], entries[name]["field"]) for name in ("none", "long", "dynamic")} == {
        "none": ("differ", "title"), "long": ("differ", "title"), "dynamic": ("differ", "dynamicTitle"),
    }
    assert fakes.prompts == ["q"]


def test_pythonThatReadsTheClockDiffersBeforeAnyModelCall(tmp_path, monkeypatch):
    fakes = _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    clock = _python("t") + "\nimport datetime\nstart = datetime.date.today()"
    store = _store(_config(_widget("w1", SAVED, query="q", code=clock)))
    assert _run(tmp_path, store) == 0
    assert (_entries(tmp_path)["w1"]["verdict"], _entries(tmp_path)["w1"]["field"]) == ("differ", "relativeTime")
    assert fakes.prompts == []


def test_noCreditsAndNoLangfuseForTheWholeJob(tmp_path, monkeypatch):
    """A real model run gets no callbacks at all: no credit callback, and no Langfuse even with its keys set."""
    import utils.llm

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test-only")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test-only")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:1")
    seen = {}

    class Workflow:
        def invoke(self, state, config=None):
            seen["config"] = config
            # Anything inside the job that asks for a Langfuse config gets none.
            seen["inside"] = utils.llm.getLangfuseConfig(trace_name="x", perRequestHandler=True)
            seen["handler"] = utils.llm.getLangfuseHandler()
            return {"finalOutput": {"response": "?"}}

    real = conv._Engine

    def engine(replay):
        built = real.__new__(real)
        built.engine, built.replay, built.failure, built.knownMetadata = "sql", replay, None, None
        from nubrix.workflows import sqlChartWorkflow

        built.module, built.workflow = sqlChartWorkflow, Workflow()
        return built

    _Fakes(monkeypatch)
    monkeypatch.setattr(conv, "_Engine", engine)
    for mode in ([], ["--write"]):
        seen.clear()
        assert _run(tmp_path, _store(_config(_widget("w1", SAVED, query="q"))), *mode) == 0
        assert seen == {"config": None, "inside": {}, "handler": None}
    assert utils.llm.newLangfuseHandler is not None and utils.llm.getLangfuseHandler.__name__ == "getLangfuseHandler"


# I1: a match must hold beyond one unfiltered run


def test_aMatchNeedsTheWorkflowsOwnRunToAgree(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED},
           finals={_sql("one"): _bar(["North", "East"], [20, 10.5])})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store, "--write") == 0
    entry = _entries(tmp_path)["w1"]
    assert (entry["verdict"], entry["field"]) == ("differ", "nondeterministic") and store.writes() == []


@pytest.mark.parametrize("sql,columns,keys", [
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY region", ["region", "total"], ["region"]),
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY total DESC, region", ["region", "total"], ["total", "region"]),
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY 2 DESC, 1", ["region", "total"], ["total", "region"]),
    ("SELECT o.region, SUM(o.revenue) AS total FROM orders AS o GROUP BY o.region ORDER BY SUM(o.revenue) DESC, o.region",
     ["region", "total"], ["total", "region"]),
    ("WITH t AS (SELECT region, revenue FROM orders) SELECT region, SUM(revenue) AS s FROM t GROUP BY region ORDER BY region",
     ["region", "s"], ["region"]),
    ("SELECT 'a' AS k, 1 AS v UNION ALL SELECT 'b', 2 ORDER BY k", ["k", "v"], ["k"]),
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region", ["region", "total"], None),
    ("SELECT region, total FROM (SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY region) AS s",
     ["region", "total"], None),
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY lower(region)", ["region", "total"], None),
    ("SELECT region, SUM(revenue) AS total FROM orders GROUP BY region ORDER BY 3", ["region", "total"], None),
    ("SELECT * FROM orders ORDER BY region", ["region", "revenue"], ["region"]),
])
def test_theOuterOrderByKeysAreOutputColumns(sql, columns, keys):
    assert conv.orderKeys(sql, columns) == keys


@pytest.mark.parametrize("sql,rows,reason", [
    # No outer ORDER BY.
    ("SELECT region, SUM(revenue) AS revenue FROM orders GROUP BY region",
     [{"region": "East", "revenue": 1.0}, {"region": "North", "revenue": 2.0}], "unstableOrder"),
    # Ties on the key: the order of the tied rows is not fixed.
    ("SELECT region, revenue FROM orders ORDER BY revenue",
     [{"region": "East", "revenue": 1.0}, {"region": "North", "revenue": 1.0}], "unstableOrder"),
    # One row needs no order.
    ("SELECT region, SUM(revenue) AS revenue FROM orders GROUP BY region", [{"region": "East", "revenue": 1.0}], None),
    ("SELECT region, revenue FROM orders ORDER BY revenue, region",
     [{"region": "East", "revenue": 1.0}, {"region": "North", "revenue": 1.0}], None),
])
def test_anUnstableOrderIsRefused(sql, rows, reason):
    request = _request("x", sql=sql)
    chart = _bar([row["region"] for row in rows], [row["revenue"] for row in rows])
    assert conv.resultRefusal(request, chart, pa.Table.from_pylist(rows), False) == reason


@pytest.mark.parametrize("sql,reason", [
    ("SELECT region, SUM(revenue) AS s FROM orders GROUP BY region ORDER BY s DESC LIMIT 50", "truncated"),
    ("WITH top_x AS (SELECT region FROM orders GROUP BY region ORDER BY SUM(revenue) DESC LIMIT 5) "
     "SELECT region, SUM(revenue) AS s FROM orders WHERE region IN (SELECT region FROM top_x) GROUP BY region ORDER BY region", "truncated"),
    ("SELECT CASE WHEN rn <= 9 THEN region ELSE 'Other' END AS label, SUM(s) AS s FROM r GROUP BY ALL ORDER BY label", "truncated"),
    ("SELECT region, revenue FROM orders QUALIFY row_number() OVER (PARTITION BY region ORDER BY revenue) = 1 ORDER BY region", "truncated"),
    ("SELECT region, rank() OVER (ORDER BY revenue) AS r FROM orders ORDER BY r, region", "truncated"),
    ("SELECT region, revenue FROM orders USING SAMPLE 10 ORDER BY region", "truncated"),
    ("SELECT region, revenue FROM orders ORDER BY region OFFSET 2", "truncated"),
    ("SELECT region FROM orders WHERE order_date >= CURRENT_DATE - 29 ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date >= current_date ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date < now() ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date < today() ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date < CURRENT_TIMESTAMP ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date >= get_current_timestamp() ORDER BY region", "relativeTime"),
    ("SELECT region FROM orders WHERE order_date >= (SELECT max(order_date) FROM orders) - INTERVAL 30 DAY ORDER BY region", "relativeTime"),
    ("SELECT region, random() AS r FROM orders ORDER BY region", "nondeterministic"),
    ("SELECT string_agg(region, ', ') AS regions FROM orders", "nondeterministic"),
    ("SELECT any_value(region) AS region FROM orders", "nondeterministic"),
    ("SELECT region, SUM(revenue) AS s FROM orders WHERE region <> 'other place' GROUP BY region ORDER BY region", None),
    ("SELECT \"now\", \"today\" FROM orders ORDER BY \"now\"", None),
    ("SELECT region, order_date + INTERVAL 1 DAY AS d FROM orders WHERE order_date >= DATE '2025-01-01' ORDER BY region", None),
])
def test_sqlThatCutsMovesOrIsRandomIsRefused(sql, reason):
    assert conv.sqlRefusal(sql) == reason


def test_refusedSqlIsADifferWithoutARun(tmp_path, monkeypatch):
    limited = _request("one", sql=_sql("one") + " LIMIT 50")
    fakes = _Fakes(monkeypatch, plans={"q": limited}, charts={limited.sql: SAVED})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store) == 0
    assert (_entries(tmp_path)["w1"]["verdict"], _entries(tmp_path)["w1"]["field"]) == ("differ", "truncated")
    assert fakes.renders == []


def _labels(count):
    return [f"L{index:04d}" for index in range(count)]


@pytest.mark.parametrize("case", ["launcherTruncated", "rowCap", "labelCap", "scatterDropped", "geoDropped"])
def test_aResultAtACapIsRefused(case):
    if case == "launcherTruncated":
        request, chart, rows, truncated = _request("x"), _bar(["a", "b"], [1, 2]), [{"region": "a", "revenue": 1}, {"region": "b", "revenue": 2}], True
    elif case == "rowCap":
        request = ChartRequest.model_validate({"chartType": "table", "title": "t", "encoding": {}, "sql": "SELECT region FROM orders ORDER BY region"})
        rows = [{"region": label} for label in _labels(1000)]
        chart, truncated = {"chartType": "table", "title": "t", "columns": ["region"], "data": rows}, False
    elif case == "labelCap":
        labels = _labels(LABEL_CAP)
        rows = [{"region": label, "revenue": 1} for label in labels]
        request, chart, truncated = _request("x"), _bar(labels, [1] * LABEL_CAP), False
    elif case == "scatterDropped":
        request = ChartRequest.model_validate({"chartType": "scatter", "title": "t", "encoding": {"x": "a", "y": "b"},
                                               "sql": "SELECT a, b FROM orders ORDER BY a"})
        rows = [{"a": 1.0, "b": 2.0}, {"a": 2.0, "b": None}]
        chart, truncated = {"chartType": "scatter", "title": "t", "data": {"datasets": [{"label": "B", "data": [{"x": 1.0, "y": 2.0}]}]}}, False
    else:
        request = ChartRequest.model_validate({"chartType": "geoMap", "title": "t", "encoding": {"lat": "lat", "long": "long"},
                                               "sql": "SELECT lat, long FROM stores ORDER BY lat"})
        rows = [{"lat": 1.0, "long": 2.0}, {"lat": 95.0, "long": 2.0}]
        chart, truncated = {"chartType": "geoMap", "title": "t", "map": {"mapType": "scatterMap", "data": {"points": [
            {"id": "0", "lat": 1.0, "long": 2.0, "label": "1.0, 2.0"}]}}}, False
    assert conv.resultRefusal(request, chart, pa.Table.from_pylist(rows), truncated) == "truncated"


def test_aRefusedResultIsADifferAndNothingIsWritten(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED}, truncated={_sql("one"): True})
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store, "--write") == 0
    assert (_entries(tmp_path)["w1"]["verdict"], _entries(tmp_path)["w1"]["field"]) == ("differ", "truncated")
    assert store.writes() == []


# I1(e): filter spot checks


@pytest.mark.parametrize("python,sql,expected", [
    ("same", "same", ("match", None)),
    ("other", "same", ("differ", "filter")),
    ("fails", "same", ("differ", "filter")),
    ("same", "fails", ("differ", "filter")),
    ("fails", "fails", ("match", None)),
])
def test_everyFilteredPairMustAgree(tmp_path, monkeypatch, python, sql, expected):
    def filtered(form, widget, filters):
        behaviour = python if form == "python" else sql
        if behaviour == "fails":
            return None
        if behaviour == "other" and filters == [{"orders.region": "North"}]:
            return _visible({**widget, "data": {"labels": ["North"], "datasets": [{"label": "Total Revenue", "data": [21]}]}})
        return _visible(widget)

    fakes = _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED},
                   filterSets=[[{"orders.region": "East"}], [{"orders.region": "North"}]], filtered=filtered)
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store, "--write") == 0
    entry = _entries(tmp_path)["w1"]
    assert (entry["verdict"], entry.get("field")) == expected
    if expected[0] == "match":
        assert entry["filtersChecked"] == 2 and len(fakes.reruns) == 4
    else:
        assert store.writes() == [] and entry["filterIndex"] == (1 if python == "other" else 0)


def test_noFilterToCheckIsADiffer(tmp_path, monkeypatch):
    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED}, filterSets=[])
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store) == 0
    entry = _entries(tmp_path)["w1"]
    assert (entry["verdict"], entry["field"], entry["filtersChecked"]) == ("differ", "filter", 0)


def test_aBusySandboxDuringAFilterCheckFailsTheWidget(tmp_path, monkeypatch):
    def filtered(form, widget, filters):
        raise SandboxBusyError(BUSY_MESSAGE)

    _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED}, filtered=filtered)
    store = _store(_config(_widget("w1", SAVED, query="q")))
    assert _run(tmp_path, store, "--write") == 0
    entry = _entries(tmp_path)["w1"]
    assert (entry["verdict"], entry["step"], entry["stage"]) == ("failed", "filter", "busy") and store.writes() == []


def test_theFilterSetsComeFromTheConfigAndTheTablesTheWidgetReads(monkeypatch):
    schemas = {
        "orders": pa.schema([("order_id", pa.int64()), ("order_date", pa.date32()), ("region", pa.string()),
                             ("status", pa.string()), ("note", pa.large_string())]),
        "stores": pa.schema([("store_name", pa.string()), ("opened", pa.timestamp("us"))]),
        "products": pa.schema([("price", pa.float64())]),
    }
    probes = []

    def probe(job, sql):
        probes.append(sql)
        if "COUNT(DISTINCT" in sql and "FROM \"orders\"" in sql:
            # region is referenced by the request: tried first; status has fewer values but only one is too few.
            return [{"d0": 4, "d1": 1, "d2": 30, "lo": "2025-03-01", "hi": "2025-09-01"}]
        if "COUNT(DISTINCT" in sql and "FROM \"stores\"" in sql:
            return [{"d0": 5, "lo": None, "hi": None}]
        if "FROM \"orders\"" in sql:
            return [{"v": "East", "n": 9}]
        return [{"v": "Harbor Store", "n": 1}]

    monkeypatch.setattr(conv, "_probe", probe)
    monkeypatch.setattr(conv, "_schemasOf", lambda job, tables: {name: schemas[name] for name in tables if name in schemas})
    job = SimpleNamespace(project=PROJECT, tables=frozenset({"orders", "stores", "products"}),
                          config={"page-1": {"filters": [{"orders.region": ["East", "West"]}, "ignored"]}})
    request = _request("x", sql="SELECT region, SUM(revenue) AS s FROM orders GROUP BY region ORDER BY region")
    widget = {"id": "w1", "generatedCode": "df = fetch_data_pl(projectId, 'stores')\nother = 'products'"}
    sets = conv._filterSets(job, "page-1", widget, request)
    assert sets == [
        [{"orders.region": ["East", "West"]}],
        [{"orders.region": "East"}],
        [{"orders.order_date": {"min": "2025-03-01", "max": "2025-09-01"}}],
        [{"stores.store_name": "Harbor Store"}],
    ]
    # The tables the SQL reads and the ones the Python names; a table with no text or date column costs no probe.
    assert not any("products" in sql for sql in probes)
    assert probes[0].startswith('SELECT COUNT(DISTINCT "region") AS d0, COUNT(DISTINCT "status") AS d1, COUNT(DISTINCT "note") AS d2')
    assert 'quantile_disc("order_date", 0.25) AS lo' in probes[0]


# I2: floats are equal only when the client draws them the same


@pytest.mark.parametrize("chartType,saved,new,same", [
    ("bar", 1407.75, 1407.7500000000002, True),
    # Close, but 1.0005 is shown as 1.001 from its shortest form and as 1.000 from its exact double value: refused.
    ("bar", 1.0005, 1.0005000000000002, False),
    ("bar", 0.0004999999999, 0.0005, False),
    ("bar", 2.0004999, 2.0004999000000001, True),
    ("bar", -0.0001, 0.0001, False),
    ("radar", 1407.75, 1407.7500000000002, False),
    ("radar", 1407.75, 1407.75, True),
    ("pie", 33.3333333333, 33.3333333333 * (1 + 1e-15), True),
])
def test_floatsMustLookTheSameInTheClient(chartType, saved, new, same):
    a = _bar(["x"], [saved], chartType=chartType)
    b = _bar(["x"], [new], chartType=chartType)
    assert (conv.visibleDifference(a, b) is None) is same


@pytest.mark.parametrize("chartType", ["scatter", "bubble"])
def test_pointFloatsAreExact(chartType):
    point = {"x": 1.25, "y": 2.5} if chartType == "scatter" else {"x": 1.25, "y": 2.5, "r": 3.0}
    saved = {"chartType": chartType, "title": "t", "xLabels": "X", "yLabels": "Y", "data": {"datasets": [{"label": "S", "data": [point]}]}}
    close = copy.deepcopy(saved)
    close["data"]["datasets"][0]["data"][0]["x"] = 1.2500000000000002
    assert conv.visibleDifference(saved, copy.deepcopy(saved)) is None
    assert conv.visibleDifference(saved, close) == "data.datasets[0].data[0].x"


# I3: schema reads are memoised for the job


def test_eachTableSchemaIsReadOncePerJob(tmp_path, monkeypatch):
    import utils.tableSchemas as tableSchemasModule

    reads = []

    def storedTableSchema(projectId, tableName, leaveHeadroom=False):
        reads.append(tableName)
        return pa.schema([("region", pa.string())])

    monkeypatch.setattr(tableSchemasModule, "storedTableSchema", storedTableSchema)
    fakes = _Fakes(monkeypatch, plans={"q": _request("one")}, charts={_sql("one"): SAVED})
    grounded = []
    original = conv._Engine

    def engine(replay):
        built = original(replay)
        invoke = built.invoke

        def grounding(metadata, prompt, project):
            # What the workflow's ground node does for every candidate.
            grounded.append(tableSchemasModule.tableSchemas(project, ["orders"], skipUnreadable=True))
            return invoke(metadata, prompt, project)

        built.invoke = grounding
        return built

    monkeypatch.setattr(conv, "_Engine", engine)
    store = _store(_config(_widget("w1", SAVED, query="q"), _widget("w2", SAVED, query="q")))
    assert _run(tmp_path, store) == 0
    assert len(grounded) == 2 and all(set(schemas) == {"orders"} for schemas in grounded)
    assert reads == ["orders"]
    # Restored after the job: a later read goes to storage again.
    tableSchemasModule.storedTableSchema(PROJECT, "orders")
    assert reads == ["orders", "orders"]
    assert fakes.prompts == ["q", "q"]


# M4: the storage client


class _FakeBucket:
    def __init__(self):
        self.uploads = []

    def upload(self, path, file, file_options=None):
        self.uploads.append((path, bytes(file), dict(file_options or {})))


class _FakeQuery:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def select(self, columns):
        self.log.append(("select", columns))
        return self

    def eq(self, column, value):
        self.log.append(("eq", column, value))
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)

    def update(self, *_args, **_kwargs):
        raise AssertionError("the job never writes the project row")


def test_theStorageStoreUsesTheServicesCallsAndReadsTheProjectRowOnly(monkeypatch):
    import utils.projectStorage

    bucket = _FakeBucket()
    queries = []
    client = SimpleNamespace(
        storage=SimpleNamespace(from_=lambda name: bucket if name == "AnalyticsHub" else None),
        table=lambda name: _FakeQuery(queries, [{"modifiedAt": "2026-11-10T08:00:00+00:00"}]) if name == "Projects" else None,
    )
    reads = []
    monkeypatch.setattr(utils.projectStorage, "readBytes", lambda project, name: reads.append((project, name)) or b"{}")
    store = conv.StorageStore(PROJECT, client=client)

    store.write("dashboardConfig.backup-x.json", b"old", overwrite=False)
    store.write(CONFIG, b"new", overwrite=True)
    assert bucket.uploads == [
        (f"{PROJECT}/dashboardConfig.backup-x.json", b"old", {"upsert": "false"}),
        (f"{PROJECT}/{CONFIG}", b"new", {"upsert": "true"}),
    ]
    assert store.read(CONFIG) == b"{}" and reads == [(PROJECT, CONFIG)]
    assert store.projectModifiedAt() == "2026-11-10T08:00:00+00:00"
    assert queries == [("select", "modifiedAt"), ("eq", "projectId", PROJECT)]


def test_theStorageStoreSendsXUpsertFalseForTheBackupAndTrueForTheConfig():
    """Through the real storage3 client: the never-overwrite guarantee is the header storage reads."""
    from storage3 import SyncStorageClient

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path, request.headers.get("x-upsert")))
        return httpx.Response(200, json={"Key": "AnalyticsHub/x", "Id": "1"})

    storage = SyncStorageClient("http://storage.test/storage/v1", {"Authorization": "Bearer test-only"},
                                http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    store = conv.StorageStore(PROJECT, client=SimpleNamespace(storage=storage, table=None))
    store.write("dashboardConfig.backup-x.json", b"old", overwrite=False)
    store.write(CONFIG, b"new", overwrite=True)
    assert sent == [
        ("POST", f"/storage/v1/object/AnalyticsHub/{PROJECT}/dashboardConfig.backup-x.json", "false"),
        ("POST", f"/storage/v1/object/AnalyticsHub/{PROJECT}/{CONFIG}", "true"),
    ]


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
    request = _request("s", sql=f"SELECT region, revenue FROM orders WHERE region = '{secret}' ORDER BY region")
    _Fakes(monkeypatch, plans={f"q {secret}": request}, charts={request.sql: _bar([secret], [1])},
           filterSets=[[{"orders.region": secret}]])
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
# Saved Python that really runs in the sandbox, as a model wrote it.
BAR_PYTHON = (
    "import json\n"
    f"df = fetch_data_pl('{PROJECT}', 'orders')\n"
    "g = df.group_by('region').agg(pl.col('revenue').sum().alias('total')).sort('region')\n"
    "print(json.dumps({'chartType': 'bar', 'title': 'Revenue by region (saved)', 'xLabels': 'Region', 'yLabels': 'Total Revenue',\n"
    "    'data': {'labels': g['region'].to_list(), 'datasets': [{'label': 'Total Revenue', 'data': g['total'].to_list()}]}}))\n"
)
# The same chart unfiltered, but every region is always listed: under a region filter it shows zeros the SQL leaves out.
HARDCODED_PYTHON = (
    "import json\n"
    f"df = fetch_data_pl('{PROJECT}', 'orders')\n"
    "sums = dict(zip(*df.group_by('region').agg(pl.col('revenue').sum()).to_dict(as_series=False).values()))\n"
    "labels = ['East', 'North', 'South', 'West']\n"
    "print(json.dumps({'chartType': 'bar', 'title': 'Revenue by region (saved)', 'xLabels': 'Region', 'yLabels': 'Total Revenue',\n"
    "    'data': {'labels': labels, 'datasets': [{'label': 'Total Revenue', 'data': [sums.get(label, 0) for label in labels]}]}}))\n"
)
CARD_PYTHON = (
    "import json\n"
    f"print(json.dumps({{'chartType': 'card', 'title': 'Orders', 'label': 'Orders placed', 'data': fetch_data_pl('{PROJECT}', 'orders').height}}))\n"
)


def _syntheticStore(*names):
    files = {"metadata.json": (DATA / PROJECT / "metadata.json").read_bytes()}
    wrong = copy.deepcopy(BAR_SAVED)
    wrong["data"]["datasets"][0]["data"][3] = 1636.0
    widgets = {
        "w-bar": _widget("w-bar", BAR_SAVED, code=BAR_PYTHON, query="Bar chart of total revenue by region"),
        "w-card": _widget("w-card", CARD_SAVED, code=CARD_PYTHON, query="How many orders were placed?"),
        "w-wrong": _widget("w-wrong", wrong, code=BAR_PYTHON, query="Bar chart of total revenue by region"),
        "w-hardcoded": _widget("w-hardcoded", BAR_SAVED, code=HARDCODED_PYTHON, query="Bar chart of total revenue by region"),
        "w-panel": _widget("w-panel", BAR_SAVED, code=PANEL),
        "w-failed": _widget("w-failed", CARD_SAVED, code="# Failed to generate code for: q\n# Error: boom"),
    }
    config = _config(*[widgets[name] for name in names])
    files[CONFIG] = _bytes(config)
    return FakeStore(files), config


@pytest.fixture
def synthetic(monkeypatch):
    monkeypatch.setenv("CHART_EVAL_STORAGE_KEY", FIXTURE_KEY)
    monkeypatch.setattr(conv, "_utcNow", lambda: NOW)
    with chartEval._environment(True, str(DATA)):
        yield


def test_endToEndDryRunOnTheSyntheticTables(tmp_path, synthetic, capsys):
    store, _ = _syntheticStore("w-bar", "w-wrong", "w-hardcoded", "w-panel", "w-failed")
    before = dict(store.files)
    stub = _stubFor(tmp_path, {"w-bar": "bar-region", "w-wrong": "bar-region", "w-hardcoded": "bar-region"})
    assert _run(tmp_path, store, "--model-stub", str(stub)) == 0
    assert store.writes() == [] and store.files == before
    report = _report(tmp_path)
    assert _verdicts(report) == {"w-bar": "match", "w-wrong": "differ", "w-hardcoded": "differ", "w-panel": "skipped", "w-failed": "skipped"}
    entries = {entry["widgetId"]: entry for entry in report["widgets"]}
    assert entries["w-wrong"]["field"] == "data.datasets[0].data[3]"
    assert entries["w-bar"]["chartRequest"]["title"] == "Revenue by region (saved)"
    # The saved Python and the request agreed under a region filter and a date-range filter, both made from the data.
    assert entries["w-bar"]["filtersChecked"] == 2
    assert [list(filters[0]) for filters in entries["w-bar"]["filters"]] == [["orders.region"], ["orders.order_date"]]
    # The hard-coded label list agrees unfiltered and differs once a filter removes regions.
    assert (entries["w-hardcoded"]["field"], entries["w-hardcoded"]["filterIndex"]) == ("filter", 0)
    assert _stdout(capsys) == ["widgets 5", "candidates 3", "match 1", "differ 2", "failed 0", "skipped 2",
                               "filters 3", "written 0", "backup none"]


def test_endToEndWriteOnTheSyntheticTables(tmp_path, synthetic):
    store, config = _syntheticStore("w-bar", "w-card", "w-panel")
    stub = _stubFor(tmp_path, {"w-bar": "bar-region", "w-card": "card-order-count"})
    assert _run(tmp_path, store, "--write", "--model-stub", str(stub)) == 0
    assert store.files[BACKUP] == _bytes(config)
    after = json.loads(store.files[CONFIG])
    assert _changedGeneratedCode(config, after) == {"w-bar", "w-card"}
    converted = {widget["id"]: decodeGeneratedCode(widget["generatedCode"]) for widget in after["page-1"]["widgets"] if widget["id"] in ("w-bar", "w-card")}
    assert converted["w-bar"].title == "Revenue by region (saved)" and converted["w-card"].chartType == "card"
    assert _report(tmp_path)["outcome"] == "written"
