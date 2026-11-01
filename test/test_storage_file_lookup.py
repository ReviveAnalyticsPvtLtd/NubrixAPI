"""Tests that named project files are found in folders holding more than 100 names."""

import ast
import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from api.models import CreateDataBlend, CreatePage, GenerateChartsInParallel, GetFieldsFromSources, PanelChartDetails, SaveQuery
from api.services.blendService import BlendService
from api.services.dashboardService import DashboardService
from api.services.managementService import ManagementService
from api.services.reportingService import ReportingService
from api.services.transformationService import TransformationService
from api.services.utilityService import UtilityService
from nubrix.components.insightContextBuilder import InsightContextBuilder
from test.storageListingFixtures import FILE_URL, PROJECT_ID, fakeClient, largeFolder
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
    with mock.patch(f"{moduleName}.urlopen", side_effect=bucket.readUrl), mock.patch(f"{moduleName}.updateProjectModifiedAt"):
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
    service._pullData = mock.Mock(return_value=SimpleNamespace(to_parquet=lambda *_args, **_kwargs: None, index=[1], columns=[1]))
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
    service = object.__new__(TransformationService)
    service.supabase = fakeClient(bucket)
    service.supabase.table.return_value.select.return_value.eq.return_value.neq.return_value.execute.return_value.data = []
    service.supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []

    with mock.patch("urllib.request.urlopen", side_effect=bucket.readUrl), pytest.raises(CustomException) as caught:
        service._validate_transformation_table_name(PROJECT_ID, "orders", currentTransformationId="transformation_1")

    assert caught.value.statusCode == 409


def test_dashboard_insights_load_from_past_the_first_names():
    bucket = largeFolder({"dashboardInsights.json": [{"pageId": "page_1"}]})

    with mock.patch("api.services.utilityService.urlopen", side_effect=bucket.readUrl):
        records = _service(UtilityService, bucket)._loadDashboardInsightsFile(PROJECT_ID)

    assert records == [{"pageId": "page_1"}]


def test_insight_context_reads_a_file_stored_past_the_first_names():
    bucket = largeFolder({"dashboardConfig.json": {"page_1": {}}})
    builder = InsightContextBuilder()
    builder.client = fakeClient(bucket)

    with mock.patch("nubrix.components.insightContextBuilder.urlopen", side_effect=bucket.readUrl):
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
