import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT = "orders-project"
TABLE = "api__00112233445566778899aabbccddeeff"
V1 = "11111111-1111-1111-1111-111111111111"
V2 = "22222222-2222-2222-2222-222222222222"


def versionRow(version, count):
    return {
        "table_name": TABLE,
        "display_name": "Orders",
        "description": None,
        "columns": [{"name": "order_id", "path": "id", "type": "string", "nullable": False}],
        "active": True,
        "version_id": version,
        "object_name": f"{TABLE}--{version.replace('-', '')}.snapshot",
        "row_count": count,
    }


def test_managed_catalog_exists_without_legacy_metadata(monkeypatch):
    from api.services.ingestion import catalog
    from utils import projectStorage

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    monkeypatch.setattr(catalog, "_readManagedRows", lambda projectId, tableName=None: [versionRow(V1, 2)])

    def missing(*args):
        error = projectStorage.StorageReadError("Missing metadata")
        error.statusCode = 404
        raise error

    monkeypatch.setattr(projectStorage, "readJson", missing)
    entry = catalog.readProjectCatalog(PROJECT)[TABLE]
    assert entry["displayName"] == "Orders"
    assert entry["shape"] == [2, 1]
    assert entry["versionId"] == V1
    assert entry["columns"] == [{"name": "order_id", "type": "string", "description": ""}]


def test_snapshot_reads_use_pinned_object_and_preserve_legacy_reads(monkeypatch):
    from api.services.ingestion import catalog
    from utils import projectStorage

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    current = [versionRow(V1, 2)]
    monkeypatch.setattr(catalog, "_readManagedRows", lambda projectId, tableName=None: current)
    monkeypatch.setattr(projectStorage, "readBytes", lambda projectId, objectName: objectName.encode())
    assert projectStorage.readTableBytes(PROJECT, "legacy") == b"legacy.parquet"

    with catalog.pinTableVersions(PROJECT, catalog.currentTableVersions(PROJECT)) as used:
        current[:] = [versionRow(V2, 3)]
        assert projectStorage.readTableBytes(PROJECT, TABLE) == f"{TABLE}--{'1' * 32}.snapshot".encode()
        assert used == {TABLE: V1}
    assert projectStorage.readTableBytes(PROJECT, TABLE) == f"{TABLE}--{'2' * 32}.snapshot".encode()


def test_table_grid_pages_the_managed_snapshot_under_its_logical_name(monkeypatch):
    import httpx
    import pyarrow as pa

    from api.services.ingestion import catalog
    from utils import projectStorage
    from utils.storedTables import writeStoredTable

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_URL", "https://storage.example")
    monkeypatch.setenv("SUPABASE_KEY", "local-test")
    monkeypatch.setattr(catalog, "_readManagedRows", lambda *args: [versionRow(V1, 2)])
    parquet = writeStoredTable(pa.table({"order_id": ["0007", "0008"]}), PROJECT, TABLE)

    def serve(request):
        assert request.url.path == f"/storage/v1/object/AnalyticsHub/{PROJECT}/{TABLE}--{'1' * 32}.snapshot"
        assert request.headers["authorization"] == "Bearer local-test"
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(parquet))})
        start, end = request.headers["range"].removeprefix("bytes=").split("-")
        return httpx.Response(206, content=parquet[int(start):int(end) + 1])

    originalClient = httpx.Client
    monkeypatch.setattr(projectStorage.httpx, "Client", lambda **kwargs: originalClient(
        transport=httpx.MockTransport(serve), **kwargs
    ))
    page, count = projectStorage.readTablePage(PROJECT, TABLE, 1, 1)
    assert count == 2
    assert page.to_dicts() == [{"order_id": "0008"}]


def test_panel_chart_dispatches_to_a_published_api_snapshot(monkeypatch):
    import pyarrow as pa

    from api.models import PanelChartDetails
    from api.services import reportingService
    from api.services.ingestion import catalog
    from utils import projectStorage
    from utils.storedTables import writeStoredTable

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_KEY", "local-test")
    row = versionRow(V1, 1)
    monkeypatch.setattr(catalog, "_readManagedRows", lambda *args: [row])
    monkeypatch.setattr(reportingService, "updateProjectModifiedAt", lambda *args: None)
    parquet = writeStoredTable(pa.table({"order_id": ["0007"]}), PROJECT, TABLE)

    def read(projectId, objectName):
        assert projectId == PROJECT
        assert objectName == row["object_name"]
        return parquet

    monkeypatch.setattr(projectStorage, "readBytes", read)
    service = object.__new__(reportingService.ReportingService)
    service.client = SimpleNamespace(storage=SimpleNamespace(from_=lambda bucket: SimpleNamespace(
        list=lambda **kwargs: [{"name": row["object_name"]}]
    )))
    service.codeTemplates = {"panelChartWithoutBlend": "read $tablesUsed"}
    chart = asyncio.run(service.generatePanelChart(PanelChartDetails(projectId=PROJECT, chartType="table", dataSource=TABLE)))
    assert chart["data"] == [{"order_id": "0007"}]
    assert chart["generatedCode"] == f"read {TABLE}"


def test_managed_spooling_and_arrow_reads_bypass_legacy_cache(tmp_path, monkeypatch):
    from api.services.ingestion import catalog
    from utils import initMethods, projectStorage

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_URL", "https://storage.example")
    current = [versionRow(V1, 2)]
    monkeypatch.setattr(catalog, "_readManagedRows", lambda projectId, tableName=None: current)
    monkeypatch.setattr(initMethods, "_cache_get", lambda key: pytest.fail("Read a stale Arrow cache key"))
    monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda key: pytest.fail("Read a stale Redis cache key"))
    monkeypatch.setattr(projectStorage, "readBytes", lambda projectId, objectName: objectName.encode())
    monkeypatch.setattr(initMethods, "decodeStoredTable", lambda projectId, tableName, data: data)

    def download(projectId, tableName, path, cancelled, *, reference):
        Path(path).write_text(reference.objectName)

    monkeypatch.setattr(initMethods, "downloadTable", download)
    with catalog.pinTableVersions(PROJECT, catalog.currentTableVersions(PROJECT)):
        current[:] = [versionRow(V2, 3)]
        spooled = initMethods.spool_table(PROJECT, TABLE, str(tmp_path / "spooled"))
        assert Path(spooled).read_text() == f"{TABLE}--{'1' * 32}.snapshot"
        assert initMethods._load_table(PROJECT, TABLE) == f"{TABLE}--{'1' * 32}.snapshot".encode()
    assert initMethods._load_table(PROJECT, TABLE) == f"{TABLE}--{'2' * 32}.snapshot".encode()


def test_dashboard_rerun_reports_version_used_and_preserves_saved_widget_on_failure(monkeypatch):
    from api.services.ingestion import catalog
    from api.services.dashboardService import DashboardService
    from utils import projectStorage

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    current = [versionRow(V1, 2)]
    monkeypatch.setattr(catalog, "_readManagedRows", lambda projectId, tableName=None: current)
    monkeypatch.setattr(projectStorage, "readBytes", lambda projectId, objectName: objectName.encode())
    widget = {"id": "orders", "chartType": "card", "data": 1, "generatedCode": f"print({TABLE!r})"}

    def compute(code, projectId):
        obj = projectStorage.readTableBytes(projectId, TABLE).decode()
        return json.dumps({"data": 2 if f"{'1' * 32}.snapshot" in obj else 3})

    executor = SimpleNamespace(run=compute)
    reference = catalog.currentTableVersions(PROJECT)
    current[:] = [versionRow(V2, 3)]
    first = DashboardService._applyFilterToAWidget(widget, [], executor, PROJECT, reference)
    assert first["data"] == 2
    assert first["dataVersions"] == {TABLE: V1}
    second = DashboardService._applyFilterToAWidget(first, [], executor, PROJECT, catalog.currentTableVersions(PROJECT))
    assert second["data"] == 3
    assert second["dataVersions"] == {TABLE: V2}

    with pytest.raises(ValueError, match="could not be refreshed"):
        DashboardService._applyFilterToAWidget(second, [], SimpleNamespace(run=lambda *a, **kw: "failed"), PROJECT,
                                               catalog.currentTableVersions(PROJECT))
    assert second["data"] == 3
