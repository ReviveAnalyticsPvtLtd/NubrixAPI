"""Authenticated reads of the private AnalyticsHub bucket."""

import asyncio
import io
import json
import os
import threading

import httpx
import pandas as pd
import polars as pl
import pytest

from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import projectStorage

PROJECT = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    yield fake
    fake.close()


def _parquet(rows: int = 1000) -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame({"a": list(range(rows))}).write_parquet(buffer, row_group_size=100)
    return buffer.getvalue()


def _wideParquet() -> bytes:
    buffer = io.BytesIO()
    pl.DataFrame({f"c{i}": [os.urandom(16).hex() for _ in range(5000)] for i in range(4)}).write_parquet(buffer, row_group_size=500)
    return buffer.getvalue()


def test_readBytesSendsTheSameHeadersAsTheStorageClient(storage):
    storage.put(PROJECT, "metadata.json", b"{}")

    assert projectStorage.readBytes(PROJECT, "metadata.json") == b"{}"

    request = storage.requests[0]
    assert request["path"] == f"/storage/v1/object/AnalyticsHub/{PROJECT}/metadata.json"
    assert request["authorization"] == f"Bearer {SERVICE_KEY}"
    assert request["apikey"] == SERVICE_KEY


def test_readJsonParsesTheObject(storage):
    storage.putJson(PROJECT, "dashboardConfig.json", {"pages": []})

    assert projectStorage.readJson(PROJECT, "dashboardConfig.json") == {"pages": []}


def test_readTableBytesAddsTheParquetSuffix(storage):
    storage.put(PROJECT, "sales.parquet", b"PAR1")

    assert projectStorage.readTableBytes(PROJECT, "sales") == b"PAR1"


def test_trailingSlashOnTheBaseUrlIsIgnored(storage, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", storage.url + "/")

    assert projectStorage.objectUrl(PROJECT, "a.json") == f"{storage.url}/storage/v1/object/AnalyticsHub/{PROJECT}/a.json"


def test_missingObjectRaisesInsteadOfReturningTheErrorBody(storage):
    with pytest.raises(httpx.HTTPStatusError):
        projectStorage.readBytes(PROJECT, "missing.json")


def test_wrongKeyRaises(storage, monkeypatch):
    storage.put(PROJECT, "metadata.json", b"{}")
    monkeypatch.setenv("SUPABASE_KEY", "anon-key")

    with pytest.raises(httpx.HTTPStatusError):
        projectStorage.readBytes(PROJECT, "metadata.json")


def test_redirectIsRefusedSoTheKeyNeverReachesAnotherHost(storage):
    other = FakePrivateStorage()
    storage.redirectTo = f"{other.url}/stolen"
    try:
        with pytest.raises(httpx.HTTPStatusError):
            projectStorage.readBytes(PROJECT, "metadata.json")
        assert other.requests == []
    finally:
        other.close()


@pytest.mark.parametrize("projectId, fileName", [
    (PROJECT, "../other/metadata.json"),
    (PROJECT, "../../OtherBucket/secret.json"),
    (PROJECT, ".."),
    (PROJECT, "."),
    (PROJECT, "..\\..\\secret.json"),
    (PROJECT, "..%2F..%2Fsecret.json"),
    (PROJECT, "a/b.json"),
    (PROJECT, "a\x00b.json"),
    (PROJECT, ""),
    ("..", "metadata.json"),
    (f"{PROJECT}/..", "metadata.json"),
    ("", "metadata.json"),
    (None, "metadata.json"),
])
def test_pathsThatLeaveTheProjectFolderAreRefusedBeforeAnyRequest(storage, projectId, fileName):
    with pytest.raises(ValueError):
        projectStorage.readBytes(projectId, fileName)

    assert storage.requests == []


def test_tableNamesWithDotsAndSpacesStayReadable(storage):
    storage.put(PROJECT, "q1 sales..v2.parquet", b"PAR1")

    assert projectStorage.readTableBytes(PROJECT, "q1 sales..v2") == b"PAR1"
    assert storage.requests[0]["path"].endswith("/q1%20sales..v2.parquet")


def test_asyncCallersGetAUrlAndHeadersThatReadThePrivateObject(storage):
    storage.putJson(PROJECT, "metadata.json", {"t": {}})

    async def read():
        async with httpx.AsyncClient() as client:
            return await client.get(projectStorage.objectUrl(PROJECT, "metadata.json"), headers=projectStorage.authHeaders())

    assert asyncio.run(read()).json() == {"t": {}}


def test_readTablePageReturnsTheRequestedRowsAndTheTotal(storage):
    storage.put(PROJECT, "sales.parquet", _parquet())

    frame, totalRows = projectStorage.readTablePage(PROJECT, "sales", offset=250, limit=5)

    assert frame["a"].to_list() == [250, 251, 252, 253, 254]
    assert totalRows == 1000


def test_readTablePageSpansARowGroupBoundary(storage):
    storage.put(PROJECT, "sales.parquet", _parquet())

    frame, _ = projectStorage.readTablePage(PROJECT, "sales", offset=98, limit=4)

    assert frame["a"].to_list() == [98, 99, 100, 101]


def test_readTablePageReadsOnlyTheFooterAndTheRowGroupsItNeeds(storage):
    body = _wideParquet()
    storage.put(PROJECT, "sales.parquet", body)

    projectStorage.readTablePage(PROJECT, "sales", offset=0, limit=10)

    ranges = [r["range"] for r in storage.requests if r["method"] == "GET"]
    assert ranges and all(ranges)
    bytesRead = sum(int(end) - int(start) + 1 for start, end in (r.split("=")[1].split("-") for r in ranges))
    assert bytesRead < len(body) / 3


def test_readTablePageSendsTheServiceKeyOnEveryRequestAndUsesNoSignedUrl(storage):
    storage.put(PROJECT, "sales.parquet", _parquet())

    projectStorage.readTablePage(PROJECT, "sales", offset=0, limit=1)

    assert {r["method"] for r in storage.requests} == {"HEAD", "GET"}
    assert all(r["authorization"] == f"Bearer {SERVICE_KEY}" for r in storage.requests)
    assert all(r["path"] == f"/storage/v1/object/AnalyticsHub/{PROJECT}/sales.parquet" for r in storage.requests)


def test_readTablePagePastTheEndIsEmptyWithTheRealTotal(storage):
    storage.put(PROJECT, "sales.parquet", _parquet())

    frame, totalRows = projectStorage.readTablePage(PROJECT, "sales", offset=5000, limit=10)

    assert frame.height == 0 and frame.columns == ["a"]
    assert totalRows == 1000


def test_readTablePageOfAnEmptyTable(storage):
    buffer = io.BytesIO()
    pl.DataFrame({"a": []}, schema={"a": pl.Int64}).write_parquet(buffer)
    storage.put(PROJECT, "empty.parquet", buffer.getvalue())

    frame, totalRows = projectStorage.readTablePage(PROJECT, "empty", offset=0, limit=10)

    assert frame.height == 0 and totalRows == 0


def test_readTablePageMatchesPolarsOnAPandasWrittenTable(storage):
    source = pd.DataFrame({
        "when": pd.date_range("2026-01-01", periods=5, freq="D"),
        "region": pd.Categorical(["a", "b", "a", "c", "b"]),
        "amount": [1.5, None, 3.5, 4.5, 5.5],
        "note": ["x", None, "z", "₹", "w"],
    })
    buffer = io.BytesIO()
    source.to_parquet(buffer, compression="snappy")
    storage.put(PROJECT, "loaded.parquet", buffer.getvalue())

    frame, _ = projectStorage.readTablePage(PROJECT, "loaded", offset=1, limit=3)

    expected = pl.read_parquet(io.BytesIO(buffer.getvalue())).slice(1, 3)
    assert frame.to_dicts() == expected.to_dicts()
    assert frame.columns == expected.columns


def test_readTablePageWorksForNamesThatSigningCannotHandle(storage):
    storage.put(PROJECT, "a+b&c.parquet", _parquet(10))

    frame, totalRows = projectStorage.readTablePage(PROJECT, "a+b&c", offset=0, limit=2)

    assert frame.height == 2 and totalRows == 10


def test_readTablePageForAMissingTableRaises(storage):
    with pytest.raises(httpx.HTTPStatusError):
        projectStorage.readTablePage(PROJECT, "missing", offset=0, limit=1)


def test_readTablePagePathIsValidated(storage):
    with pytest.raises(ValueError):
        projectStorage.readTablePage(PROJECT, "../other", offset=0, limit=1)

    assert storage.requests == []


def test_readTablePageRefusesARedirect(storage):
    storage.put(PROJECT, "sales.parquet", _parquet(10))
    other = FakePrivateStorage()
    storage.redirectTo = f"{other.url}/stolen"
    try:
        with pytest.raises(httpx.HTTPStatusError):
            projectStorage.readTablePage(PROJECT, "sales", offset=0, limit=1)
        assert other.requests == []
    finally:
        other.close()


def test_aServerThatIgnoresRangeIsAnErrorNotWrongRows(storage):
    storage.put(PROJECT, "sales.parquet", _parquet(10))
    storage.ignoreRange = True

    with pytest.raises(httpx.HTTPError):
        projectStorage.readTablePage(PROJECT, "sales", offset=0, limit=1)


def test_aSeekOnOneThreadDoesNotMoveAnotherThreadsRead(storage):
    body = bytes(range(256)) * 400
    storage.put(PROJECT, "sales.parquet", body)
    bothSought = threading.Barrier(2)
    results = {}

    with httpx.Client(headers=projectStorage.authHeaders()) as client:
        tableFile = projectStorage._TableFile(client, projectStorage.objectUrl(PROJECT, "sales.parquet"))

        def worker(position):
            tableFile.seek(position)
            bothSought.wait(timeout=5)
            results[position] = tableFile.read(64)

        threads = [threading.Thread(target=worker, args=(position,)) for position in (100, 5000)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert results == {100: body[100:164], 5000: body[5000:5064]}


def test_sslContextIsBuiltOnce():
    assert projectStorage._sslContext() is projectStorage._sslContext()


def test_jsonReaderHandlesUtf8(storage):
    storage.put(PROJECT, "insights.json", json.dumps({"k": "₹"}, ensure_ascii=False).encode("utf-8"))

    assert projectStorage.readJson(PROJECT, "insights.json") == {"k": "₹"}
