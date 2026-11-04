"""Every service read of AnalyticsHub must work once the bucket is private.

FILE_URL points at the public route, so a read that still uses it gets the
private-bucket refusal. Several callers swallow read errors, so each test
asserts on what the read produced, not only that nothing raised.
"""

import asyncio
import io
import json
import re
import types
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import polars as pl
import pytest
from fastapi import HTTPException

from api.routers import manager
from api.services import blendService as blendModule
from api.services import dashboardService as dashboardModule
from api.services import dataLoadService as dataLoadModule
from api.services import managementService as managementModule
from api.services import reportingService as reportingModule
from api.services import transformationService as transformationModule
from api.services import utilityService as utilityModule
from nubrix.components.insightContextBuilder import InsightContextBuilder
from nubrix.components import reportGenerator as reportGeneratorModule
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import initMethods
from utils.exceptionHandler import CustomException

PROJECT = "11111111-2222-4333-8444-555555555555"
REPOSITORY = Path(__file__).resolve().parent.parent


class _EmptyQuery:
    """A Supabase table query that always returns the configured rows."""

    def __init__(self, rows=()):
        self.rows = list(rows)

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _FakeClient:
    """Stands in for the Supabase client: lists from the fake bucket and records uploads."""

    def __init__(self, fake: FakePrivateStorage, rows=()):
        self.uploads: dict[str, dict | list] = {}
        self._fake = fake
        self._rows = rows
        self.storage = types.SimpleNamespace(from_=self._bucket)

    def table(self, _name):
        return _EmptyQuery(self._rows)

    def _bucket(self, bucketName):
        return types.SimpleNamespace(list=self._list if bucketName == "AnalyticsHub" else lambda *a, **k: [], upload=self._upload, remove=lambda *a, **k: None)

    def _list(self, path=None):
        prefix = f"AnalyticsHub/{path}/"
        return [
            {"name": key[len(prefix):], "metadata": {"lastModified": "2026-10-31T00:00:00.000Z"}}
            for key in self._fake.objects if key.startswith(prefix)
        ]

    def _upload(self, path, file, file_options=None):
        self.uploads[path.split("/")[-1]] = json.loads(file)


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("FILE_URL", fake.url + "/storage/v1/object/public/AnalyticsHub/{projectId}/{fileName}.parquet")
    yield fake
    fake.close()


@pytest.fixture
def client(storage, monkeypatch):
    fake = _FakeClient(storage)
    for service in (blendModule.blendService, dashboardModule.dashboardService, dataLoadModule.dataLoadService,
                    managementModule.managementService, utilityModule.utilityService):
        monkeypatch.setattr(service, "client", fake)
    monkeypatch.setattr(dashboardModule, "client", fake)
    for module in (blendModule, dashboardModule, dataLoadModule, managementModule):
        monkeypatch.setattr(module, "updateProjectModifiedAt", lambda *_args: None)
    return fake


def _parquet(frame: dict) -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame(frame).write_parquet(buffer)
    return buffer.getvalue()


def _assertAuthenticated(storage, *fileNames):
    """Every read went to the authenticated route with the service key, and none to the public one."""
    reads = [r for r in storage.requests if r["method"] == "GET"]
    assert sorted({r["path"].rsplit("/", 1)[-1] for r in reads}) == sorted(fileNames)
    for read in reads:
        assert read["path"].startswith("/storage/v1/object/AnalyticsHub/")
        assert read["authorization"] == f"Bearer {SERVICE_KEY}"


# --- blendService ---------------------------------------------------------


def test_createDataBlendKeepsExistingBlends(storage, client):
    storage.putJson(PROJECT, "blendConfig.json", {"old": {"tables": ["a"]}})
    details = types.SimpleNamespace(projectId=PROJECT, tables=["a", "b"], blendOn=["id"], joinTypes=["inner"], blendName="new")

    blendModule.blendService.createDataBlend(details)

    assert set(client.uploads["blendConfig.json"]) == {"old", "new"}
    _assertAuthenticated(storage, "blendConfig.json")


def test_getDataSourcesReadsBlendsAndMetadata(storage, client):
    storage.putJson(PROJECT, "blendConfig.json", {"b1": {"tables": ["a"], "joinTypes": [], "blendOn": []}})
    storage.putJson(PROJECT, "metadata.json", {"a": {"isActive": True}})

    sources = blendModule.blendService.getDataSources(PROJECT)

    assert sources["rawTables"] == ["a"] and sources["blendedTables"] == ["b1"]
    _assertAuthenticated(storage, "blendConfig.json", "metadata.json")


def test_getFieldsFromSourcesReadsABlend(storage, client):
    column = {"name": "id", "type": "int64"}
    storage.putJson(PROJECT, "metadata.json", {"a": {"columns": [column]}, "b": {"columns": [column]}})
    storage.putJson(PROJECT, "blendConfig.json", {"b1": {"tables": ["a", "b"], "blendOn": ["id"]}})

    fields = blendModule.blendService.getFieldsFromSources(types.SimpleNamespace(projectId=PROJECT, tableName="b1"))

    assert "id" in fields["numericalColumns"]
    _assertAuthenticated(storage, "blendConfig.json", "metadata.json")


# --- dashboardService -----------------------------------------------------


def _dashboard():
    return {"page1": {"name": "Main", "widgets": [{"id": "w1", "layout": {"x": 0}, "query": "q1"}]}}


def test_getAllColumnsReadsEachTable(storage, client):
    storage.put(PROJECT, "sales.parquet", _parquet({"amount": [1.5, 2.5]}))

    columns = dashboardModule.dashboardService.getAllColumns(PROJECT)

    assert columns["sales"][0]["columnName"] == "amount"
    assert columns["sales"][0]["max"] == 2.5
    _assertAuthenticated(storage, "sales.parquet")


def test_createPageKeepsExistingPages(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())

    pageId = dashboardModule.dashboardService.createPage(types.SimpleNamespace(projectId=PROJECT, pageName="New"))

    assert set(client.uploads["dashboardConfig.json"]) == {"page1", pageId}
    _assertAuthenticated(storage, "dashboardConfig.json")


def test_getAllPages(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())

    assert dashboardModule.dashboardService.getAllPages(PROJECT) == [{"pageName": "Main", "pageId": "page1"}]
    _assertAuthenticated(storage, "dashboardConfig.json")


def test_exportToDashboardAppendsToTheStoredPage(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())
    details = types.SimpleNamespace(
        projectId=PROJECT, page="page1", chartType="bar", title="t", label="l", xLabels="x",
        yLabels="y", data={}, map=None, layout={}, generatedCode="code",
    )

    dashboardModule.dashboardService.exportToDashboard(details)

    assert len(client.uploads["dashboardConfig.json"]["page1"]["widgets"]) == 2
    _assertAuthenticated(storage, "dashboardConfig.json")


def test_getDataReturnsTheStoredPage(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())
    details = types.SimpleNamespace(projectId=PROJECT, page="page1", filters=None, refresh=False)

    assert dashboardModule.dashboardService.getData(details)["name"] == "Main"
    _assertAuthenticated(storage, "dashboardConfig.json")


def test_editWidgetPositionChangesTheStoredPage(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())
    details = types.SimpleNamespace(projectId=PROJECT, pageId="page1", pageName="Renamed", widgets=[{"id": "w1", "layout": {"x": 9}}])

    page = dashboardModule.dashboardService.editWidgetPosition(details)

    assert page["name"] == "Renamed" and page["widgets"][0]["layout"] == {"x": 9}
    _assertAuthenticated(storage, "dashboardConfig.json")


def test_deleteDashboardElementReconcilesInsights(storage, client):
    storage.putJson(PROJECT, "dashboardConfig.json", _dashboard())
    storage.putJson(PROJECT, "insights.json", {"insights": [{"query": "q1", "isCharted": True}]})
    details = types.SimpleNamespace(projectId=PROJECT, deletionObject="widget", id="w1")

    dashboardModule.dashboardService.deleteDashboardElement(details)

    assert client.uploads["dashboardConfig.json"]["page1"]["widgets"] == []
    assert client.uploads["insights.json"]["insights"][0]["isCharted"] is False
    _assertAuthenticated(storage, "dashboardConfig.json", "insights.json")


def test_updateMetadataShapesRewritesTheStoredShape(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"shape": [1, 1]}})

    dashboardModule.dashboardService._updateMetadataShapes(PROJECT, {"sales": [9, 2]})

    assert client.uploads["metadata.json"]["sales"]["shape"] == [9, 2]
    _assertAuthenticated(storage, "metadata.json")


def test_pullDataInParallelReadsStoredConnections(storage, client, monkeypatch):
    storage.putJson(PROJECT, "connections.json", {"c1": {"table": "orders"}})
    pulled = []

    def pull(connection):
        pulled.append(connection["table"])
        raise RuntimeError("stop after the read")

    monkeypatch.setattr(dashboardModule.dashboardService, "_pullData", pull)

    with pytest.raises(CustomException):
        dashboardModule.dashboardService.pullDataInParallel(PROJECT)

    assert pulled == ["orders"]
    _assertAuthenticated(storage, "connections.json")


# --- dataLoadService ------------------------------------------------------


def test_deleteTableRemovesItFromStoredMetadata(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"a": {}, "b": {}})

    dataLoadModule.dataLoadService.deleteTable(types.SimpleNamespace(projectId=PROJECT, tableName="a"))

    assert client.uploads["metadata.json"] == {"b": {}}
    _assertAuthenticated(storage, "metadata.json")


# --- managementService ----------------------------------------------------


def test_attributeInfoReadsTheParquetSchema(storage, client):
    storage.put(PROJECT, "sales.parquet", _parquet({"amount": [1, 2, 3]}))

    info, shape = managementModule.managementService._attributeInfoFunc(PROJECT, "sales")

    assert shape == [3, 1] and "amount" in info
    _assertAuthenticated(storage, "sales.parquet")


def test_validateTableNameAvailableStillRejectsATakenName(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {}})

    with pytest.raises(CustomException) as caught:
        managementModule.managementService.validateTableNameAvailable(PROJECT, "sales")

    assert caught.value.statusCode == 409
    _assertAuthenticated(storage, "metadata.json")


def test_generateMetadataForTablesPreservesTheStoredActiveFlag(storage, client, monkeypatch):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": False}})
    monkeypatch.setattr(managementModule.managementService, "_generateMetadata", lambda **_kw: {"sales": {"isActive": True}})

    managementModule.managementService.generateMetadataForTables(PROJECT, ["sales"])

    assert client.uploads["metadata.json"]["sales"]["isActive"] is False
    _assertAuthenticated(storage, "metadata.json")


def test_generateRawInsightsReadsStoredMetadata(storage, client, monkeypatch):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": True}})
    monkeypatch.setattr(client, "table", lambda _name: _EmptyQuery([{"domainExpert": "Retail"}]))
    seen = {}

    class Chain:
        def invoke(self, payload, config=None):
            seen.update(payload)
            return '{"a": "How are sales?"}'

    monkeypatch.setattr(managementModule, "buildLlmChain", lambda *_args: Chain())

    raw = managementModule.managementService._generateRawInsights(PROJECT)

    assert raw == [{"query": "How are sales?"}]
    assert seen["metadata"] == {"sales": {"isActive": True}}
    _assertAuthenticated(storage, "metadata.json")


def test_getInsights(storage, client):
    storage.putJson(PROJECT, "insights.json", {"insights": [{"id": 1}]})

    assert managementModule.managementService.getInsights(PROJECT) == {"insights": [{"id": 1}]}
    _assertAuthenticated(storage, "insights.json")


def test_generateMetadataKeepsStoredMetadataWhenNothingChanged(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": True}})

    assert managementModule.managementService.generateMetadata(PROJECT) == {"sales": {"isActive": True}}
    _assertAuthenticated(storage, "metadata.json")


def test_getMetadata(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"description": "d", "shape": [1, 1], "columns": []}})

    tables = managementModule.managementService.getMetadata(PROJECT)["tables"]

    assert tables[0]["tableName"] == "sales" and tables[0]["tableDesc"] == "d"
    _assertAuthenticated(storage, "metadata.json")


def test_editMetadataChangesTheStoredDescription(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"description": "old"}})
    details = types.SimpleNamespace(projectId=PROJECT, tableName="sales", tableDescription="new", columnName=None, columnDescription=None)

    assert managementModule.managementService.editMetadata(details)["sales"]["description"] == "new"
    _assertAuthenticated(storage, "metadata.json")


def test_toggleTableActiveFlipsTheStoredFlag(storage, client):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": True}})

    assert managementModule.managementService.toggleTableActive(PROJECT, "sales")["isActive"] is False
    _assertAuthenticated(storage, "metadata.json")


def test_getReport(storage, client):
    storage.putJson(PROJECT, "generatedReport.json", {"sales": "<html></html>"})

    assert managementModule.managementService.getReport(PROJECT, "sales") == "<html></html>"
    _assertAuthenticated(storage, "generatedReport.json")


# --- utilityService -------------------------------------------------------


def test_loadDashboardInsightsFile(storage, client):
    storage.putJson(PROJECT, "dashboardInsights.json", [{"id": 1}])

    assert utilityModule.utilityService._loadDashboardInsightsFile(PROJECT) == [{"id": 1}]
    _assertAuthenticated(storage, "dashboardInsights.json")


# --- transformationService ------------------------------------------------


def _transformationService(storage):
    service = transformationModule.TransformationService()
    service.supabase = _FakeClient(storage)
    service._redis_client = lambda: MagicMock(get=lambda _key: None)
    return service


def test_transformationMetadataIsReadWithTheServiceKey(storage):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": True}, "off": {"isActive": False}})

    metadata = asyncio.run(_transformationService(storage)._get_metadata(PROJECT))

    assert list(metadata) == ["sales"]
    _assertAuthenticated(storage, "metadata.json")


def test_transformationCannotOverwriteABaseTable(storage):
    storage.putJson(PROJECT, "metadata.json", {"sales": {}})

    with pytest.raises(CustomException) as caught:
        _transformationService(storage)._validate_transformation_table_name(PROJECT, "sales", currentTransformationId="t1")

    assert caught.value.statusCode == 409
    _assertAuthenticated(storage, "metadata.json")


# --- reportingService -----------------------------------------------------


def _reportingService(storage, monkeypatch):
    service = reportingModule.ReportingService.__new__(reportingModule.ReportingService)
    service.client = _FakeClient(storage)
    service._httpClient = httpx.AsyncClient(timeout=20)
    service._metadataCache = {}
    service._metadataCacheTtl = 30.0
    service._redis = MagicMock(get=lambda _key: None)
    monkeypatch.setattr(reportingModule, "updateProjectModifiedAt", lambda *_args: None)
    monkeypatch.setattr(initMethods, "classify_table_size", lambda *_args: {"size_class": "small", "hint": ""})
    return service


def test_reportingProjectMetadata(storage, monkeypatch):
    storage.putJson(PROJECT, "metadata.json", {"sales": {"isActive": True}})

    metadata = asyncio.run(_reportingService(storage, monkeypatch)._getProjectMetadata(PROJECT))

    assert list(metadata) == ["sales"]
    _assertAuthenticated(storage, "metadata.json")


@pytest.mark.parametrize("fileName", ["insights.json", "dashboardConfig.json", "blendConfig.json"])
def test_reportingFetchesEachConfigFile(storage, monkeypatch, fileName):
    storage.putJson(PROJECT, fileName, {"stored": True})
    service = _reportingService(storage, monkeypatch)

    assert asyncio.run(service._fetchJson(service._projectMetadataUrl(PROJECT, fileName))) == {"stored": True}
    _assertAuthenticated(storage, fileName)


def test_getQueries(storage, monkeypatch):
    storage.putJson(PROJECT, "queryConfig.json", {"q1": "text"})

    assert asyncio.run(_reportingService(storage, monkeypatch).getQueries(PROJECT)) == {"q1": "text"}
    _assertAuthenticated(storage, "queryConfig.json")


def test_saveQueryKeepsStoredQueries(storage, monkeypatch):
    storage.putJson(PROJECT, "queryConfig.json", {"q1": "text"})
    service = _reportingService(storage, monkeypatch)

    queryId = asyncio.run(service.saveQuery(types.SimpleNamespace(projectId=PROJECT, query="new")))

    assert service.client.uploads["queryConfig.json"] == {"q1": "text", queryId: "new"}
    _assertAuthenticated(storage, "queryConfig.json")


def test_deleteQueryRemovesFromStoredQueries(storage, monkeypatch):
    storage.putJson(PROJECT, "queryConfig.json", {"q1": "text", "q2": "keep"})
    service = _reportingService(storage, monkeypatch)

    asyncio.run(service.deleteQuery(types.SimpleNamespace(projectId=PROJECT, queryId="q1")))

    assert service.client.uploads["queryConfig.json"] == {"q2": "keep"}
    _assertAuthenticated(storage, "queryConfig.json")


# --- nubrix components ----------------------------------------------------


def test_insightContextBuilderLoadsAProjectFile(storage):
    storage.putJson(PROJECT, "metadata.json", {"sales": {}})
    builder = InsightContextBuilder.__new__(InsightContextBuilder)
    builder.config = types.SimpleNamespace(storageBucket="AnalyticsHub")
    builder.client = _FakeClient(storage)

    assert builder._loadJsonFromStorage(PROJECT, "metadata.json") == {"sales": {}}
    _assertAuthenticated(storage, "metadata.json")


def test_reportGeneratorListsActiveTables(storage):
    storage.putJson(PROJECT, "metadata.json", {"on": {}, "off": {"isActive": False}})
    generator = reportGeneratorModule.ReportGenerator.__new__(reportGeneratorModule.ReportGenerator)

    assert generator.getAllTables(PROJECT) == ["on"]
    _assertAuthenticated(storage, "metadata.json")


def test_reportGeneratorProfilesAStoredTable(storage, monkeypatch):
    storage.put(PROJECT, "sales.parquet", _parquet({"amount": [1, 2]}))
    profiled = {}

    class Report:
        def __init__(self, frame, title):
            profiled["rows"] = len(frame.index)

        def to_html(self):
            return (
                '<button id="tab-overview-reproduction"></button><div id="tab-pane-overview-reproduction"></div>'
                '<p class="text-body-secondary text-end"></p><footer></footer><main>report</main>'
            )

    monkeypatch.setattr(reportGeneratorModule, "ProfileReport", Report)
    generator = reportGeneratorModule.ReportGenerator.__new__(reportGeneratorModule.ReportGenerator)

    assert "report" in generator.getProfilingReport(PROJECT, "sales")
    assert profiled["rows"] == 2
    _assertAuthenticated(storage, "sales.parquet")


# --- data layer -----------------------------------------------------------


@pytest.fixture
def coldDataCache(monkeypatch):
    initMethods.invalidate_data_cache()
    monkeypatch.setattr(initMethods, "_fetch_data_from_redis", lambda _key: None)
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda *_args: None)
    yield
    initMethods.invalidate_data_cache()


@pytest.mark.parametrize("read, rows", [
    (lambda: initMethods.fetch_data(PROJECT, "sales"), lambda frame: len(frame.index)),
    (lambda: initMethods.fetch_data_pl(PROJECT, "sales"), lambda frame: frame.height),
    (lambda: initMethods.scan_data(PROJECT, "sales"), lambda frame: frame.collect().height),
], ids=["fetch_data", "fetch_data_pl", "scan_data"])
def test_dataLayerColdPathReadsWithTheServiceKey(storage, coldDataCache, read, rows):
    storage.put(PROJECT, "sales.parquet", _parquet({"amount": [1, 2, 3]}))

    assert rows(read()) == 3
    _assertAuthenticated(storage, "sales.parquet")


# --- viewTable ------------------------------------------------------------


def _viewTable(tableName="sales", page=1, pageSize=2):
    return asyncio.run(manager.viewTable(PROJECT, tableName, page=page, pageSize=pageSize, token="session"))


def test_viewTablePagesAPrivateTable(storage):
    storage.put(PROJECT, "sales.parquet", _parquet({"amount": [1, 2, 3, 4, 5]}))

    body = json.loads(_viewTable(page=2).body)

    assert body["rows"] == [{"amount": 3}, {"amount": 4}]
    assert body["pagination"]["totalRows"] == 5
    assert body["pagination"]["totalPages"] == 3


def test_viewTableErrorCarriesNoCredential(storage, monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "wrong-key")

    with pytest.raises(HTTPException) as caught:
        _viewTable()

    detail = json.dumps(caught.value.detail)
    assert caught.value.status_code == 500
    assert "Failed to read table" in detail
    assert "wrong-key" not in detail


def test_viewTableReadsNoMoreThanItNeeds(storage):
    frame = {f"c{i}": [str(j) for j in range(2000)] for i in range(3)}
    buffer = io.BytesIO()
    pl.DataFrame(frame).write_parquet(buffer, row_group_size=200)
    storage.put(PROJECT, "sales.parquet", buffer.getvalue())

    _viewTable(page=1, pageSize=10)

    assert all(r["range"] for r in storage.requests if r["method"] == "GET")


def test_viewTableForAMissingTableIsAnError(storage):
    with pytest.raises(HTTPException) as caught:
        _viewTable(tableName="missing")

    assert caught.value.status_code == 500


def test_viewTableRefusesAPathOutOfTheProject(storage):
    with pytest.raises(HTTPException):
        _viewTable(tableName="../other")

    assert storage.requests == []


# --- guard ----------------------------------------------------------------


def _sourceFiles():
    for folder in ("api", "nubrix", "utils"):
        yield from (REPOSITORY / folder).rglob("*.py")


def test_noSourceFileStillReadsTheBucketWithoutCredentials():
    offenders = []
    for path in _sourceFiles():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"(?<!DOMAIN_)FILE_URL|object/public/AnalyticsHub", line):
                offenders.append(f"{path.name}:{number}")
            elif "urlopen(" in line and "domainFileUrl" not in line.replace("DomainFileUrl", "domainFileUrl"):
                offenders.append(f"{path.name}:{number}")

    assert offenders == []


def test_noSourceFileBuildsAPublicUrlForTheBucketThroughTheStorageClient():
    offenders = [path.name for path in _sourceFiles() if "get_public_url" in path.read_text(encoding="utf-8")]

    assert offenders == []
