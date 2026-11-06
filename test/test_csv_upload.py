import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd
import pytest

from api.services.dataLoadService import DataLoadService
from utils import storedTables
from utils.exceptionHandler import CustomException
from utils.sandboxSlots import SandboxBusyError
from utils.storedTables import MARKER_KEY, markerIsValid, readFooter


class CsvUploadTests(unittest.TestCase):
    def test_csv_reader_detects_semicolon_delimited_uploads(self):
        df = DataLoadService._readCsvUpload(
            b'"age";"job";"balance"\n30;"unemployed";1787\n'
        )

        self.assertEqual(["age", "job", "balance"], list(df.columns))
        self.assertEqual("unemployed", df.loc[0, "job"])


PROJECT = "project_1"
CLAIMS = [{"transformation_id": "t1", "latest_approved_artifact": {"table_name": "sales"}}]


class _Bucket:
    """Keeps the bytes of every upload, because the temporary file is gone once the upload returns."""

    def __init__(self):
        self.uploads = {}

    def upload(self, file, path, file_options=None):
        with open(file, "rb") as stored:
            self.uploads[path] = stored.read()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "unit-test-key")
    monkeypatch.delenv("STORED_TABLE_NORMALISE", raising=False)
    monkeypatch.delenv("STORED_TABLE_MAX_DECODED_BYTES", raising=False)
    storedTables._missingKeyWarned = False


@pytest.fixture
def loader():
    """A real DataLoadService over a fake bucket, with name claims and everything after the upload stubbed."""
    from api.services.managementService import managementService

    bucket = _Bucket()
    service = object.__new__(DataLoadService)
    service.client = SimpleNamespace(storage=SimpleNamespace(from_=lambda _name: bucket))
    service.bucket = bucket
    service._acceptTableName = mock.Mock(return_value=CLAIMS)
    service._releaseTableName = mock.Mock(return_value=[])
    service._restoreClaims = mock.Mock()
    service.metadata = mock.Mock(return_value={})
    with (
        mock.patch.object(managementService, "generateMetadataForTables", service.metadata),
        mock.patch("api.services.dataLoadService.forget_table"),
        mock.patch("api.services.dataLoadService.updateProjectModifiedAt"),
    ):
        yield service


def _upload(csv: bytes, name: str = "sales.csv"):
    upload = mock.Mock(filename=name)
    upload.read = mock.AsyncMock(return_value=csv)
    return upload


def _assertMarked(loader, tableName: str):
    stored = loader.bucket.uploads[f"{PROJECT}/{tableName}.parquet"]
    metadata = readFooter(stored)
    assert metadata is not None
    assert markerIsValid(metadata, PROJECT, tableName)
    assert not markerIsValid(metadata, PROJECT, "another")
    return stored


def _connection(table: str):
    return mock.Mock(host="db.test", port=3306, user="u", password="p", db="shop", table=table, projectId=PROJECT)


def test_csvUploadStoresAMarkedTable(loader):
    asyncio.run(loader.loadCsvData(PROJECT, [_upload(b"amount,label\n1,a\n2,b\n")]))

    stored = _assertMarked(loader, "sales")
    assert pd.read_parquet(io.BytesIO(stored))["amount"].tolist() == [1, 2]


def test_excelUploadStoresAMarkedTable(loader):
    sheets = {"Q1": pd.DataFrame({"amount": [1, 2]})}
    with mock.patch("api.services.dataLoadService.pd.read_excel", return_value=sheets):
        asyncio.run(loader.loadExcelData(PROJECT, [_upload(b"x", "book.xlsx")]))

    _assertMarked(loader, "book_Q1")


def test_sqlLoadsStoreAMarkedTable(loader):
    with (
        mock.patch("api.services.dataLoadService.URL"),
        mock.patch("api.services.dataLoadService.create_engine"),
        mock.patch("api.services.dataLoadService.pd.read_sql", return_value=pd.DataFrame({"amount": [1]})),
    ):
        loader.loadMySql(_connection("orders"))
        loader.loadPostgreSQL(_connection("invoices"))

    _assertMarked(loader, "orders")
    _assertMarked(loader, "invoices")


def test_mongoLoadStoresAMarkedTable(loader):
    with mock.patch("api.services.dataLoadService.MongoClient") as mongo:
        mongo.return_value.__getitem__.return_value.__getitem__.return_value.find.return_value = [{"_id": 1, "amount": 3}]
        loader.loadMongoDB(mock.Mock(connectionString="mongodb://db.test", db="shop", collection="events", projectId=PROJECT))

    stored = _assertMarked(loader, "events")
    assert pd.read_parquet(io.BytesIO(stored)).columns.tolist() == ["amount"]


def test_oversizedUploadIsRefusedWith413(loader, monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", str(1 << 20))
    rows = "\n".join(str(number) for number in range(300_000))

    with pytest.raises(CustomException) as caught:
        asyncio.run(loader.loadCsvData(PROJECT, [_upload(f"amount\n{rows}\n".encode())]))

    assert caught.value.statusCode == 413
    assert caught.value.errorCode == "TABLE_TOO_LARGE"
    assert caught.value.uiMessage == "This table is too large to load (over 1 MiB)."
    assert loader.bucket.uploads == {}
    loader._restoreClaims.assert_called_once_with(PROJECT, "sales", CLAIMS)
    loader._releaseTableName.assert_not_called()


def test_excelWithAnOversizedSecondSheetGives413AndKeepsTheFirstTable(loader, monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", str(1024 * 1024))
    sheets = {"Q1": pd.DataFrame({"amount": [1, 2]}), "Q2": pd.DataFrame({"amount": np.arange(400_000, dtype="int64")})}

    with (
        mock.patch("api.services.dataLoadService.pd.read_excel", return_value=sheets),
        pytest.raises(CustomException) as caught,
    ):
        asyncio.run(loader.loadExcelData(PROJECT, [_upload(b"x", "book.xlsx")]))

    assert (caught.value.statusCode, caught.value.errorCode) == (413, "TABLE_TOO_LARGE")
    assert caught.value.uiMessage == "This table is too large to load (over 1 MiB)."
    assert list(loader.bucket.uploads) == [f"{PROJECT}/book_Q1.parquet"]
    loader._restoreClaims.assert_called_once_with(PROJECT, "book_Q2", CLAIMS)
    loader.metadata.assert_not_called()


def test_oversizedImportIsRefusedWith413(loader, monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", str(1024 * 1024))
    big = pd.DataFrame({"a": np.arange(400_000, dtype="int64")})
    with (
        mock.patch("api.services.dataLoadService.URL"),
        mock.patch("api.services.dataLoadService.create_engine"),
        mock.patch("api.services.dataLoadService.pd.read_sql", return_value=big),
        pytest.raises(CustomException) as caught,
    ):
        loader.loadMySql(_connection("orders"))

    assert (caught.value.statusCode, caught.value.errorCode) == (413, "TABLE_TOO_LARGE")
    assert caught.value.uiMessage == "This table is too large to load (over 1 MiB)."
    assert "bytes decoded" not in caught.value.uiMessage
    assert loader.bucket.uploads == {}


def test_callbackRefusalRestoresClaimsAndReachesTheCallerAs413(loader):
    refusal = storedTables.TableTooLargeError("Table is 9 bytes decoded, over the limit of 1.")

    def refuse(path):
        raise refusal

    with pytest.raises(CustomException) as caught:
        loader._writeTable(PROJECT, "sales", CLAIMS, refuse)

    assert (caught.value.statusCode, caught.value.errorCode) == (413, "TABLE_TOO_LARGE")
    assert "bytes decoded" not in caught.value.uiMessage
    loader._restoreClaims.assert_called_once_with(PROJECT, "sales", CLAIMS)
    assert loader.bucket.uploads == {}


def test_missingKeyStoresAnUnmarkedTableAndNeverRaises(loader, monkeypatch):
    monkeypatch.delenv("SUPABASE_KEY")

    asyncio.run(loader.loadCsvData(PROJECT, [_upload(b"amount\n1\n2\n")]))

    stored = loader.bucket.uploads[f"{PROJECT}/sales.parquet"]
    metadata = readFooter(stored)
    assert metadata is not None
    assert MARKER_KEY not in (metadata.metadata or {})
    assert markerIsValid(metadata, PROJECT, "sales") is False
    assert pd.read_parquet(io.BytesIO(stored))["amount"].tolist() == [1, 2]


def test_busyMetadataStepIsLoggedAsAWarningWithTheTableName(loader):
    loader.metadata.side_effect = SandboxBusyError("no slot")
    with mock.patch("api.services.dataLoadService.logger") as log:
        newMetadata = asyncio.run(loader.loadCsvData(PROJECT, [_upload(b"amount\n1\n")]))

    assert newMetadata == {}
    assert f"{PROJECT}/sales.parquet" in loader.bucket.uploads
    warnings = [call.args[0] for call in log.warning.call_args_list]
    assert any("sales" in message and "CSV" in message for message in warnings)
    log.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
