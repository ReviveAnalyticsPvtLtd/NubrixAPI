"""Dashboard filters reach saved Python widgets through the run input, not through code rewriting.

Each test runs real sandbox children: the filters travel in the run's stdin, `sandbox_launcher` hands them to
`use_run_filters`, and the data functions narrow every table they load.
"""

import copy
import datetime
import io
import json
import string
import sys
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
import yaml

from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from test.test_sandbox_storage_reads import PROJECT
from api.models import GetData
from utils import codeExecutor
from utils.runFilters import applyFilters, filterDataset

TEMPLATES_FILE = Path(__file__).resolve().parent.parent / "codeTemplates.yaml"
SALES = {"state": ["SP", "RJ", "SP", "MG"], "amount": [1, 2, 3, 4]}
SP_ROWS = 2


@pytest.fixture
def salesStorage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame(SALES).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    regions = io.BytesIO()
    pl.DataFrame({"state": ["SP", "RJ", "MG"], "zone": ["south", "east", "west"]}).write_parquet(regions)
    fake.put(PROJECT, "regions.parquet", regions.getvalue())
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    yield fake
    fake.close()


def loadTemplates():
    with open(TEMPLATES_FILE, encoding="utf-8") as templatesFile:
        return yaml.safe_load(templatesFile)


def runCode(code, filters=None):
    return codeExecutor.REPLManager(timeoutSeconds=120).run(code, PROJECT, filters=filters).strip()


PANDAS = f'import json\nprint(json.dumps(len(fetch_data("{PROJECT}", "sales"))))'
POLARS_EAGER = f'import json\nprint(json.dumps(fetch_data_pl("{PROJECT}", "sales").height))'
POLARS_LAZY = f'import json\nprint(json.dumps(scan_data("{PROJECT}", "sales").collect().height))'


def test_pandasWidgetIsFiltered(salesStorage):
    assert runCode(PANDAS, [{"state": "SP"}]) == str(SP_ROWS)


def test_polarsEagerWidgetIsFiltered(salesStorage):
    assert runCode(POLARS_EAGER, [{"state": "SP"}]) == str(SP_ROWS)


def test_polarsLazyWidgetIsFiltered(salesStorage):
    assert runCode(POLARS_LAZY, [{"state": "SP"}]) == str(SP_ROWS)


@pytest.mark.parametrize("code", [PANDAS, POLARS_EAGER, POLARS_LAZY])
def test_noFiltersMeansAllRows(salesStorage, code):
    assert runCode(code) == "4"
    assert runCode(code, []) == "4"


def test_explicitBaseFiltersStillApply(salesStorage):
    code = f'import json\nprint(json.dumps(len(fetch_data("{PROJECT}", "sales", [{{"amount": {{"min": 2}}}}]))))'
    assert runCode(code) == "3"


def test_baseFiltersAndRunFiltersCombine(salesStorage):
    code = f'import json\nprint(json.dumps(fetch_data("{PROJECT}", "sales", [{{"amount": {{"min": 2}}}}])["amount"].tolist()))'
    assert json.loads(runCode(code, [{"state": "SP"}])) == [3]


def test_listArgsAreFilteredToo(salesStorage):
    code = f'import json\nprint(json.dumps(len(fetch_data("{PROJECT}", "sales", None, [{{"amount": {{"max": 2}}}}]))))'
    assert runCode(code, [{"state": "SP"}]) == "1"


def test_aTableIsReadFreshAfterAFilteredLoad(salesStorage):
    code = (
        f'import json\na = len(fetch_data("{PROJECT}", "sales"))\n'
        f'b = len(fetch_data("{PROJECT}", "sales"))\nc = scan_data("{PROJECT}", "sales").collect().height\n'
        "print(json.dumps([a, b, c]))"
    )
    assert json.loads(runCode(code, [{"state": "SP"}])) == [SP_ROWS] * 3


def test_aFilterOnAnotherTableLeavesThisOneAlone(salesStorage):
    assert runCode(PANDAS, [{"orders.state": "SP"}]) == "4"


def test_panelTemplateWidgetIsFilteredThroughRunInput(salesStorage):
    """The saved panel widget loads its table with fetch_data, so the run's filters narrow it."""
    templates = loadTemplates()

    code = string.Template(templates["panelChartWithoutBlend"]).substitute(
        projectId=PROJECT, chartType="bar", xAxis="state", yAxis="amount", aggregationMetric="sum",
        dataSourceName="sales", tablesUsed="sales", index=None, columns=None, values=None,
        selectedColumns=None, mapType="", isFilterApplied=False, filters=None, geoCodeColumn="None",
    )

    everything = json.loads(runCode(code))
    assert sorted(everything["data"]["labels"]) == ["MG", "RJ", "SP"]

    filtered = json.loads(runCode(code, [{"state": "SP"}]))
    assert filtered["data"]["labels"] == ["SP"]
    assert filtered["data"]["datasets"][0]["data"] == [4]


def test_panelBlendTemplateWidgetIsFilteredThroughRunInput(salesStorage):
    """The blend template loads every table with fetch_data, so the run's filters narrow each one before the merge."""
    templates = loadTemplates()
    code = string.Template(templates["panelChartWithBlend"]).substitute(
        projectId=PROJECT, chartType="bar", xAxis="zone", yAxis="amount", aggregationMetric="sum",
        dataSourceName="blend", tablesUsed=["sales", "regions"], joinTypes=["inner"], blendOn=["state"],
        index=None, columns=None, values=None, selectedColumns=None, mapType="", isFilterApplied=False,
        filters=None, geoCodeColumn="None",
    )

    everything = json.loads(runCode(code))
    assert sorted(everything["data"]["labels"]) == ["east", "south", "west"]

    filtered = json.loads(runCode(code, [{"state": "SP"}]))
    assert filtered["data"]["labels"] == ["south"]
    assert filtered["data"]["datasets"][0]["data"] == [4]


def panelWithoutBlendCode():
    """The unmodified saved panel template, as the chart builder renders it for a bar chart over `sales`."""
    return string.Template(loadTemplates()["panelChartWithoutBlend"]).substitute(
        projectId=PROJECT, chartType="bar", xAxis="state", yAxis="amount", aggregationMetric="sum",
        dataSourceName="sales", tablesUsed="sales", index=None, columns=None, values=None,
        selectedColumns=None, mapType="", isFilterApplied=False, filters=None, geoCodeColumn="None",
    )


LAZY_BAR = (
    "import json\n"
    f'n = scan_data("{PROJECT}", "sales").collect().height\n'
    'print(json.dumps({"chartType": "bar", "title": "rows", "xLabels": "x", "yLabels": "y", '
    '"data": {"labels": ["rows"], "datasets": [{"label": "rows", "data": [n]}]}}))'
)
SAVED_BAR = {"labels": ["rows"], "datasets": [{"label": "rows", "data": [999]}]}


def savedWidgets():
    return [
        {"id": "lazy", "chartType": "bar", "title": "rows", "data": SAVED_BAR, "layout": {"x": 0}, "generatedCode": LAZY_BAR},
        {"id": "panel", "chartType": "bar", "title": "t", "data": SAVED_BAR, "layout": {"x": 1}, "generatedCode": panelWithoutBlendCode()},
        {"id": "broken", "chartType": "table", "title": "t", "data": [{"a": 1}], "layout": {"x": 2},
         "generatedCode": "raise RuntimeError('boom')"},
    ]


@pytest.fixture
def dashboardService(salesStorage, monkeypatch):
    """The real DashboardService and real sandbox children; only the project's config storage is stubbed."""
    from api.services import dashboardService as module

    store = {"pg": {"name": "n", "widgets": savedWidgets()}}
    monkeypatch.setattr(module, "readJson", lambda projectId, name: copy.deepcopy(store))
    service = module.DashboardService.__new__(module.DashboardService)
    service.client = MagicMock()
    return service


def test_getDataFilterRerunsSavedWidgetsThroughRealSandboxChildren(dashboardService):
    page = dashboardService.getData(GetData(projectId=PROJECT, page="pg", filters=[{"sales.state": "SP"}]))

    lazy, panel, broken = page["widgets"]
    assert lazy["data"]["datasets"][0]["data"] == [SP_ROWS]
    assert panel["data"]["labels"] == ["SP"]
    assert panel["data"]["datasets"][0]["data"] == [4]
    assert broken["chartType"] == "table" and broken["data"] == []
    assert [w["id"] for w in page["widgets"]] == ["lazy", "panel", "broken"]
    assert [w["generatedCode"] for w in page["widgets"]] == [w["generatedCode"] for w in savedWidgets()]
    dashboardService.client.storage.from_.assert_not_called()


def test_getDataRefreshSavesSucceededWidgetsAndKeepsTheFailedOne(dashboardService):
    page = dashboardService.getData(GetData(projectId=PROJECT, page="pg", refresh=True))

    upload = dashboardService.client.storage.from_.return_value.upload
    saved = json.loads(upload.call_args.kwargs["file"].decode("utf-8"))["pg"]["widgets"]
    assert [w["id"] for w in saved] == ["lazy", "panel", "broken"]
    assert [w["generatedCode"] for w in saved] == [w["generatedCode"] for w in savedWidgets()]
    assert saved[0]["data"]["datasets"][0]["data"] == [4]
    assert sorted(saved[1]["data"]["labels"]) == ["MG", "RJ", "SP"]
    assert saved[2]["data"] == [{"a": 1}]
    assert [w["data"] for w in page["widgets"]] == [w["data"] for w in saved]


@pytest.mark.skipif(sys.platform == "win32", reason="Arrow has no time zone database on Windows")
@pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata"])
def test_textFilterOnTimeZoneTimestampsMatchesByTheColumnsText(zone, tmp_path):
    """Runs in the production image: its Arrow must find a time zone database to turn tz-aware timestamps into text."""
    moments = [datetime.datetime(2024, 1, 15, 6), datetime.datetime(2024, 2, 15, 6), datetime.datetime(2023, 1, 15, 6)]
    table = pa.table({"id": pa.array([1, 2, 3], type=pa.int64()), "at": pa.array(moments, type=pa.timestamp("us", tz=zone))})
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    texts = table.column("at").cast(pa.string()).to_pylist()
    assert all(text.startswith(("2024-01-15", "2024-02-15", "2023-01-15")) for text in texts), texts

    for flt, expected in (
        ([{"at": {"startswith": "2024-01"}}], [1]),
        ([{"at": {"startswith": "2023"}}], [3]),
        ([{"at": {"contains": "2024-02-15"}}], [2]),
    ):
        assert applyFilters(table, "t", flt).column("id").to_pylist() == expected
        assert filterDataset(ds.dataset(path), "t", flt).to_table().column("id").to_pylist() == expected
