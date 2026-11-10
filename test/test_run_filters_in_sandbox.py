"""Dashboard filters reach saved Python widgets through the run input, not through code rewriting.

Each test runs real sandbox children: the filters travel in the run's stdin, `sandbox_launcher` hands them to
`use_run_filters`, and the data functions narrow every table they load.
"""

import io
import json
import os
from unittest.mock import MagicMock

import polars as pl
import pytest
import yaml

from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from test.test_sandbox_storage_reads import PROJECT
from utils import codeExecutor

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
    templates = yaml.safe_load(open(os.path.join(os.getcwd(), "codeTemplates.yaml"), encoding="utf-8"))
    import string

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
    import string

    templates = yaml.safe_load(open(os.path.join(os.getcwd(), "codeTemplates.yaml"), encoding="utf-8"))
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
