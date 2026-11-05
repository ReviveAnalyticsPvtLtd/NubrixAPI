"""Tests that named project files are found in folders holding more than 100 names."""

import ast
import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from api.models import CreateDataBlend, CreatePage, GenerateChartsInParallel, GetFieldsFromSources, PanelChartDetails, SaveQuery
from api.services.blendService import BlendService
from api.services.dashboardService import DashboardService
from api.services.managementService import ManagementService
from api.services.reportingService import ReportingService
from api.services.transformationService import TransformationService
from api.services.utilityService import UtilityService
from nubrix.components.insightContextBuilder import InsightContextBuilder
from test.storageListingFixtures import FILE_URL, PROJECT_ID, FakeBucket, fakeClient, largeFolder
from utils.exceptionHandler import CustomException

REPO_ROOT = Path(__file__).resolve().parent.parent
TWO_TABLE_METADATA = {
    "orders": {"columns": [{"name": "id", "type": "int64"}]},
    "customers": {"columns": [{"name": "id", "type": "int64"}]},
}
TWO_TABLE_BLEND = {"blended": {"tables": ["orders", "customers"], "blendOn": ["id"], "joinTypes": ["inner"]}}


@pytest.fixture(autouse=True)
def _fileUrl():
    with mock.patch.dict(os.environ, {"FILE_URL": FILE_URL}):
        yield


@contextmanager
def _stored(moduleName, bucket):
    with mock.patch(f"{moduleName}.readJson", side_effect=bucket.readJson), mock.patch(f"{moduleName}.updateProjectModifiedAt"):
        yield


def _service(serviceClass, bucket):
    service = object.__new__(serviceClass)
    service.client = fakeClient(bucket)
    return service


def _reportingService(bucket):
    service = _service(ReportingService, bucket)
    service.codeTemplates = {"panelChartWithoutBlend": "code", "panelChartWithBlend": "code"}
    service._generatePanelChart = mock.Mock(return_value={})

    async def fetchJson(url):
        return json.loads(bucket.readUrl(url).read())

    service._fetchJson = fetchJson
    return service


@contextmanager
def _storedDashboard(bucket):
    with _stored("api.services.dashboardService", bucket), mock.patch("api.services.dashboardService.client", fakeClient(bucket)):
        yield


def test_create_data_blend_keeps_blends_stored_past_the_first_names():
    bucket = largeFolder({"blendConfig.json": TWO_TABLE_BLEND})
    details = CreateDataBlend(projectId=PROJECT_ID, blendOn=["id"], blendName="newer", tables=["orders", "customers"], joinTypes=["inner"])

    with _stored("api.services.blendService", bucket):
        _service(BlendService, bucket).createDataBlend(details)

    assert set(bucket.uploadedJson("blendConfig.json")) == {"blended", "newer"}


def test_data_sources_include_blends_stored_past_the_first_names():
    bucket = largeFolder({"blendConfig.json": TWO_TABLE_BLEND, "metadata.json": TWO_TABLE_METADATA})

    with _stored("api.services.blendService", bucket):
        sources = _service(BlendService, bucket).getDataSources(PROJECT_ID)

    assert [blend["blendName"] for blend in sources["blends"]] == ["blended"]


def test_fields_of_a_blend_stored_past_the_first_names_are_found():
    bucket = largeFolder({"blendConfig.json": TWO_TABLE_BLEND, "metadata.json": TWO_TABLE_METADATA})
    details = GetFieldsFromSources(projectId=PROJECT_ID, tableName="blended")

    with _stored("api.services.blendService", bucket):
        fields = _service(BlendService, bucket).getFieldsFromSources(details)

    assert "id" in fields["numericalColumns"]


def test_new_page_keeps_pages_stored_past_the_first_names():
    bucket = largeFolder({"dashboardConfig.json": {"page_1": {"name": "Sales", "widgets": []}}})

    with _storedDashboard(bucket):
        _service(DashboardService, bucket).createPage(CreatePage(projectId=PROJECT_ID, pageName="Costs"))

    assert "page_1" in bucket.uploadedJson("dashboardConfig.json")


def test_all_pages_include_pages_stored_past_the_first_names():
    bucket = largeFolder({"dashboardConfig.json": {"page_1": {"name": "Sales", "widgets": []}}})

    with _storedDashboard(bucket):
        pages = _service(DashboardService, bucket).getAllPages(PROJECT_ID)

    assert pages == [{"pageName": "Sales", "pageId": "page_1"}]


def test_insight_flags_reconcile_when_insights_sit_past_the_first_names():
    bucket = largeFolder({"insights.json": {"insights": [{"query": "gone", "isCharted": True}]}})

    with _storedDashboard(bucket):
        _service(DashboardService, bucket)._reconcileInsightChartedStatus(PROJECT_ID, {})

    assert bucket.uploadedJson("insights.json") == {"insights": [{"query": "gone", "isCharted": False}]}


def test_shapes_update_metadata_stored_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"orders": {"shape": [1, 1]}}})

    with _storedDashboard(bucket):
        _service(DashboardService, bucket)._updateMetadataShapes(PROJECT_ID, {"orders": [2, 3]})

    assert bucket.uploadedJson("metadata.json") == {"orders": {"shape": [2, 3]}}


def test_refresh_pulls_connections_stored_past_the_first_names():
    bucket = largeFolder({"connections.json": {"first": {"table": "orders"}}})
    service = _service(DashboardService, bucket)
    service._pullData = mock.Mock(return_value=pd.DataFrame({"id": [1]}))
    service._updateMetadataShapes = mock.Mock()

    with _storedDashboard(bucket):
        service.pullDataInParallel(PROJECT_ID)

    service._pullData.assert_called_once_with({"table": "orders"})


def test_incremental_metadata_keeps_tables_stored_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"orders": {"isActive": False}}})
    service = _service(ManagementService, bucket)
    service._generateMetadata = mock.Mock(return_value={"customers": {}})

    with _stored("api.services.managementService", bucket):
        service.generateMetadataForTables(PROJECT_ID, ["customers"])

    assert bucket.uploadedJson("metadata.json") == {"orders": {"isActive": False}, "customers": {"isActive": True}}


def test_get_metadata_reads_a_file_stored_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"orders": {"description": "Orders"}}})

    with _stored("api.services.managementService", bucket):
        metadata = _service(ManagementService, bucket).getMetadata(PROJECT_ID)

    assert [table["tableName"] for table in metadata["tables"]] == ["orders"]


def test_parallel_charts_keep_the_page_stored_past_the_first_names():
    bucket = largeFolder({
        "dashboardConfig.json": {"page_1": {"name": "Auto", "isAutomatic": True, "widgets": []}},
        "insights.json": {"insights": []},
    })
    service = _reportingService(bucket)
    service._getProjectMetadata = mock.AsyncMock(return_value={})

    with mock.patch("api.services.reportingService.updateProjectModifiedAt"), mock.patch("api.services.reportingService.buildLlmChain"):
        page = asyncio.run(service.generateChartsInParallel(GenerateChartsInParallel(projectId=PROJECT_ID, inputQueries=[])))

    assert page["name"] == "Auto"


def test_panel_chart_finds_a_table_stored_past_the_first_names():
    bucket = largeFolder({"z_table.parquet": {}})
    service = _reportingService(bucket)
    details = PanelChartDetails(projectId=PROJECT_ID, chartType="bar", dataSource="z_table")

    with mock.patch("api.services.reportingService.updateProjectModifiedAt"):
        response = asyncio.run(service.generatePanelChart(details))

    assert response == {"generatedCode": "code"}
    assert service._generatePanelChart.call_args.kwargs["tablesUsed"] == "z_table"


def test_panel_chart_finds_a_blend_stored_past_the_first_names():
    bucket = largeFolder({"blendConfig.json": TWO_TABLE_BLEND})
    service = _reportingService(bucket)
    details = PanelChartDetails(projectId=PROJECT_ID, chartType="bar", dataSource="blended")

    with mock.patch("api.services.reportingService.updateProjectModifiedAt"):
        asyncio.run(service.generatePanelChart(details))

    assert service._generatePanelChart.call_args.kwargs["tablesUsed"] == ["orders", "customers"]


def test_panel_chart_still_refuses_an_unknown_source():
    bucket = largeFolder()
    details = PanelChartDetails(projectId=PROJECT_ID, chartType="bar", dataSource="unknown")

    with pytest.raises(CustomException) as caught:
        asyncio.run(_reportingService(bucket).generatePanelChart(details))

    assert caught.value.statusCode == 400


def test_saved_query_keeps_queries_stored_past_the_first_names():
    bucket = largeFolder({"queryConfig.json": {"query_1": "total sales"}})

    with mock.patch("api.services.reportingService.updateProjectModifiedAt"):
        asyncio.run(_reportingService(bucket).saveQuery(SaveQuery(projectId=PROJECT_ID, query="total costs")))

    assert "query_1" in bucket.uploadedJson("queryConfig.json")


def test_get_queries_reads_a_file_stored_past_the_first_names():
    bucket = largeFolder({"queryConfig.json": {"query_1": "total sales"}})

    assert asyncio.run(_reportingService(bucket).getQueries(PROJECT_ID)) == {"query_1": "total sales"}


def test_base_table_guard_reads_metadata_stored_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"orders": {}}})
    bucket.fileNames = sorted([*bucket.fileNames, "orders.parquet"])
    service = object.__new__(TransformationService)
    service.supabase = fakeClient(bucket)
    service.supabase.table.return_value.select.return_value.eq.return_value.neq.return_value.execute.return_value.data = []
    service.supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

    with mock.patch("api.services.transformationService.readJson", side_effect=bucket.readJson), pytest.raises(CustomException) as caught:
        service._validate_transformation_table_name(PROJECT_ID, "orders", currentTransformationId="transformation_1")

    assert caught.value.statusCode == 409


def test_dashboard_insights_load_from_past_the_first_names():
    bucket = largeFolder({"dashboardInsights.json": [{"pageId": "page_1"}]})

    with mock.patch("api.services.utilityService.readJson", side_effect=bucket.readJson):
        records = _service(UtilityService, bucket)._loadDashboardInsightsFile(PROJECT_ID)

    assert records == [{"pageId": "page_1"}]


def test_insight_context_reads_a_file_stored_past_the_first_names():
    bucket = largeFolder({"dashboardConfig.json": {"page_1": {}}})
    builder = InsightContextBuilder()
    builder.client = fakeClient(bucket)

    with mock.patch("nubrix.components.insightContextBuilder.readJson", side_effect=bucket.readJson):
        assert builder._loadJsonFromStorage(PROJECT_ID, "dashboardConfig.json") == {"page_1": {}}


def _bucketName(call):
    """Return the bucket name of a `.from_(name).list(...)` call, or None for any other call."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "list"):
        return None
    owner = call.func.value
    if not (isinstance(owner, ast.Call) and isinstance(owner.func, ast.Attribute) and owner.func.attr == "from_"):
        return None
    source = owner.args[0]
    return source.value if isinstance(source, ast.Constant) else ast.unparse(source)


def _optionKeys(call):
    options = next((keyword.value for keyword in call.keywords if keyword.arg == "options"), None)
    if options is None and len(call.args) > 1:
        options = call.args[1]
    return {key.value for key in options.keys} if isinstance(options, ast.Dict) else set()


def test_no_project_folder_listing_stops_at_the_default_page():
    uncapped = []
    for folder in ("api", "nubrix", "utils"):
        for path in (REPO_ROOT / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and _bucketName(node) in ("AnalyticsHub", "self.config.storageBucket"):
                    if not _optionKeys(node) & {"search", "limit"}:
                        uncapped.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert uncapped == []


class _KeepingBucket(FakeBucket):
    """Keeps the bytes of every table upload, because the temporary file is gone once the upload returns."""

    def upload(self, path, file, file_options=None):
        if path.endswith(".parquet"):
            with open(file, "rb") as stored:
                file = stored.read()
        self.uploads[path.rsplit("/", 1)[1]] = file


def _refresh(frame):
    bucket = _KeepingBucket({"connections.json": {"first": {"table": "orders"}}})
    service = _service(DashboardService, bucket)
    service._pullData = mock.Mock(return_value=frame)
    service._updateMetadataShapes = mock.Mock()
    with _storedDashboard(bucket):
        service.pullDataInParallel(PROJECT_ID)
    return bucket


def test_refresh_stores_a_marked_table():
    from utils.storedTables import markerIsValid, readFooter

    with mock.patch.dict(os.environ, {"SUPABASE_KEY": "unit-test-key"}):
        bucket = _refresh(pd.DataFrame({"id": [1, 2]}))
        metadata = readFooter(bucket.uploads["orders.parquet"])
        assert metadata is not None
        assert markerIsValid(metadata, PROJECT_ID, "orders")


def test_refresh_refuses_a_table_over_the_size_limit_with_413():
    big = pd.DataFrame({"id": range(400_000)})

    with mock.patch.dict(os.environ, {"STORED_TABLE_MAX_DECODED_BYTES": str(1024 * 1024)}), pytest.raises(CustomException) as caught:
        _refresh(big)

    assert (caught.value.statusCode, caught.value.errorCode) == (413, "TABLE_TOO_LARGE")
    assert caught.value.uiMessage == "This table is too large to load (over 1 MiB)."
