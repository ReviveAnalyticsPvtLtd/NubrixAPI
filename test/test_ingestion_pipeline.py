from contextlib import closing
from pathlib import Path
from uuid import uuid4

import os
import psycopg2
from psycopg2.extensions import parse_dsn
import pytest


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("INGESTION_TEST_DSN")
    if not dsn:
        pytest.skip("Set INGESTION_TEST_DSN to the disposable ingestion database")
    target = parse_dsn(dsn)
    assert target.get("host") == "127.0.0.1"
    assert target.get("dbname") == "nubrix_ingestion_test"
    monkeypatch.setenv("DATABASE_URL", dsn)
    migration = next(Path("supabase/migrations").glob("*_api_ingestion_registry.sql"))
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS api_ingestion CASCADE')
        cursor.execute('CREATE TABLE IF NOT EXISTS public."Projects" ("projectId" text primary key)')
        cursor.execute('INSERT INTO public."Projects" VALUES (\'test-project\') ON CONFLICT DO NOTHING')
        cursor.execute(migration.read_text(encoding="utf-8"))
    yield dsn


def test_credentials_are_bound_to_their_source(monkeypatch):
    from cryptography.fernet import Fernet
    from api.services.ingestion.credentials import protect, reveal
    monkeypatch.setenv("API_INGESTION_SECRET_KEY", Fernet.generate_key().decode())
    ciphertext = protect("source-1", "test-only-key")
    assert "test-only-key" not in ciphertext
    assert reveal("source-1", ciphertext) == "test-only-key"
    with pytest.raises(ValueError):
        reveal("source-2", ciphertext)


def test_failed_run_preserves_current_version_and_retry_is_fenced(database):
    from api.services.ingestion import repository as repo
    source = repo.createSource("test-project", {
        "displayName": "Orders", "columns": [{"name": "id", "path": "id", "type": "string", "nullable": False}],
        "endpoint": "https://customer.example/orders", "cadenceMinutes": 0,
    }, "opaque-ciphertext")
    first = repo.requestRun(source["id"])
    assert repo.requestRun(source["id"])["id"] == first["id"]
    assert repo.dueRunIds() == [first["id"]]
    assert repo.dueRunIds() == []
    lease = repo.claimRun(first["id"])
    assert repo.claimRun(first["id"]) is None
    firstVersion = str(uuid4())
    assert repo.publishSnapshot(lease, firstVersion, 2, "digest")
    assert repo.publishSnapshot(lease, firstVersion, 2, "digest")
    version = repo.getSource(source["id"])["current_version_id"]
    second = repo.requestRun(source["id"])
    failedLease = repo.claimRun(second["id"])
    repo.finishAttempt(failedLease, "INVALID_RECORD")
    assert repo.getRun(second["id"])["status"] == "failed"
    assert repo.getSource(source["id"])["current_version_id"] == version
    assert not repo.publishSnapshot(failedLease, str(uuid4()), 1, "digest")
    assert repo.getSource(source["id"])["current_version_id"] == version


def test_abandoned_snapshot_is_collectable_after_project_deletion(database, monkeypatch):
    from api.services.ingestion import repository as repo

    source = repo.createSource("test-project", {
        "displayName": "Orders", "columns": [{"name": "id", "path": "id", "type": "string", "nullable": False}],
        "endpoint": "https://customer.example/orders", "cadenceMinutes": 0,
    }, "opaque-ciphertext")
    lease = repo.claimRun(repo.requestRun(source["id"])["id"])
    versionId = str(uuid4())
    assert repo.recordStaging(lease, versionId)
    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    with repo.projectStorageLock("test-project"):
        repo.fenceProjectSources("test-project")
    assert repo.getRun(lease["id"])["status"] == "cancelled"
    assert not repo.recordStaging(lease, str(uuid4()))
    with repo.transaction() as cursor:
        cursor.execute('DELETE FROM public."Projects" WHERE "projectId"=%s', ("test-project",))
        cursor.execute("UPDATE api_ingestion.staged_objects SET created_at=now()-interval '2 days'")
    assert not repo.publishSnapshot(lease, versionId, 1, "digest")
    assert [row["version_id"] for row in repo.cleanupCandidates()] == [versionId]


def test_source_views_exclude_credentials_and_require_ownership(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.commons import UserContext, requireProductAccess
    from api.routers import ingestion

    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    sourceId = "7632fb57-462f-4c70-9b41-33c0c556b699"
    source = {
        "id": sourceId, "project_id": "owned", "dataset_id": "dataset", "display_name": "Orders",
        "table_name": "api__orders", "current_version_id": None, "revision": 1, "enabled": True,
        "cadence_minutes": 0, "needs_attention": False, "has_active_run": False,
        "last_success_at": None, "next_due_at": None, "credential": "secret-ciphertext",
        "config": {"endpoint": "https://example.test", "recordsPath": "data", "authHeader": "Authorization"},
    }
    app = FastAPI()
    app.include_router(ingestion.router)
    app.dependency_overrides[requireProductAccess] = lambda: UserContext("owner", "owner@example.test", "session")
    monkeypatch.setattr(ingestion.repository, "listSources", lambda projectId: [source])

    def requireOwner(projectId, _user):
        from fastapi import HTTPException
        if projectId != "owned":
            raise HTTPException(404)

    monkeypatch.setattr(ingestion, "requireProjectOwner", requireOwner)
    with TestClient(app) as client:
        response = client.get("/projects/owned/sources")
        assert response.status_code == 200
        assert response.json()["sources"][0]["id"] == sourceId
        assert "secret-ciphertext" not in response.text
        assert client.get("/projects/other/sources").status_code == 404


def test_worker_publishes_typed_parquet_and_keeps_it_when_next_page_fails(database, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    import pyarrow.parquet as pq
    from api.services.ingestion import repository as repo, restPull
    from api.services.ingestion.credentials import protect
    from nubrix.triggers.tasks import apiIngestionTask as worker

    monkeypatch.setenv("API_INGESTION_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("API_INGESTION_ALLOWED_HOSTS", "customer.example")
    monkeypatch.setenv("API_INGESTION_ENABLED", "1")
    sourceId = "b115fa58-87b5-43ef-9e99-b117295ad32f"
    source = repo.createSource("test-project", {
        "displayName": "Orders", "columns": [{"name": "id", "path": "id", "type": "string", "nullable": False}],
        "endpoint": "https://customer.example/orders", "cadenceMinutes": 0,
        "recordsPath": "records", "nextCursorPath": "cursor", "cursorQueryParam": "page",
    }, protect(sourceId, "test-secret"), sourceId=sourceId)
    snapshots = {}

    def stage(projectId, logicalName, objectName, table):
        target = tmp_path / objectName
        pq.write_table(table, target)
        snapshots[objectName] = pq.read_table(target).to_pylist()
        return "fixture-digest"

    monkeypatch.setattr(worker, "_stage", stage)
    monkeypatch.setattr(worker, "_projectCanSync", lambda _projectId: True)

    def readPage(host, target, headers, deadline, limit):
        if "page=next" in target:
            return b'{"records":[{"id":"0008"}],"cursor":null}'
        return b'{"records":[{"id":"0007"}],"cursor":"next"}'

    monkeypatch.setattr(restPull, "_readPage", readPage)
    first = repo.requestRun(source["id"])
    worker.executeRun(first["id"])
    completed = repo.getRun(first["id"])
    assert completed["status"] == "succeeded"
    assert list(snapshots.values()) == [[{"id": "0007"}, {"id": "0008"}]]
    from api.services.ingestion.catalog import resolveTableVersion
    reference = resolveTableVersion("test-project", source["table_name"])
    assert reference.objectName in snapshots
    assert reference.versionId == completed["version_id"]

    def failSecondPage(host, target, headers, deadline, limit):
        if "page=next" in target:
            raise restPull.IngestionError("UPSTREAM_UNAVAILABLE", retryable=True)
        return readPage(host, target, headers, deadline, limit)

    monkeypatch.setattr(restPull, "_readPage", failSecondPage)
    second = repo.requestRun(source["id"])
    worker.executeRun(second["id"])
    assert repo.getRun(second["id"])["status"] == "retrying"
    assert repo.getSource(source["id"])["current_version_id"] == completed["version_id"]
    assert len(snapshots) == 1
