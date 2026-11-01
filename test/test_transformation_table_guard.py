"""Tests for the base-table guard on transformation output names."""

import io
import json
import os
from unittest import mock
from urllib.error import URLError

import pytest

from api.services.transformationService import TransformationService
from utils.exceptionHandler import CustomException


class _FakeQuery:
    def select(self, *_args):
        return self

    def eq(self, *_args):
        return self

    def neq(self, *_args):
        return self

    def execute(self):
        return mock.Mock(data=[])


class _FakeBucket:
    PAGE_LIMIT = 100

    def __init__(self, fileNames, listError=None):
        self.fileNames = sorted(fileNames)
        self.listError = listError

    def list(self, path=None, options=None):
        if self.listError:
            raise self.listError
        search = (options or {}).get("search", "")
        matches = [name for name in self.fileNames if name.startswith(search)]
        return [{"name": name} for name in matches[:self.PAGE_LIMIT]]


class _FakeSupabase:
    def __init__(self, fileNames, listError=None):
        self.storage = mock.Mock()
        self.storage.from_.return_value = _FakeBucket(fileNames, listError)

    def table(self, _name):
        return _FakeQuery()


def _service(fileNames=("metadata.json",), listError=None):
    service = object.__new__(TransformationService)
    service.supabase = _FakeSupabase(fileNames, listError)
    return service


def _validate(service, name="sales"):
    service._validate_transformation_table_name("project_1", name, currentTransformationId=None)


@pytest.fixture(autouse=True)
def _fileUrl():
    with mock.patch.dict(os.environ, {"FILE_URL": "https://files.example.test/{projectId}/{fileName}.parquet"}):
        yield


@pytest.fixture
def storedMetadata():
    response = lambda _url: io.BytesIO(json.dumps({"sales": {"isActive": True}}).encode("utf-8"))
    with mock.patch("urllib.request.urlopen", side_effect=response) as urlopen:
        yield urlopen


def test_baseTableNameIsRefused(storedMetadata):
    with pytest.raises(CustomException) as caught:
        _validate(_service())

    assert caught.value.statusCode == 409


def test_baseTableNameIsRefusedForAnExistingTransformation(storedMetadata):
    with pytest.raises(CustomException) as caught:
        _service()._validate_transformation_table_name("project_1", "sales", currentTransformationId="t1")

    assert caught.value.statusCode == 409


def test_freeNameIsAllowed(storedMetadata):
    _validate(_service(), name="sales_summary")


def test_projectWithoutMetadataHasNoBaseTables(storedMetadata):
    _validate(_service(fileNames=()))

    storedMetadata.assert_not_called()


def test_metadataPastTheFirstPageOfAListingIsStillChecked(storedMetadata):
    earlierFiles = [f"a_table_{index:03}.parquet" for index in range(150)]

    with pytest.raises(CustomException) as caught:
        _validate(_service(fileNames=[*earlierFiles, "metadata.json"]))

    assert caught.value.statusCode == 409


def test_nameIsRefusedWhenTheMetadataDownloadFails(storedMetadata):
    storedMetadata.side_effect = URLError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _validate(_service(), name="sales_summary")

    assert caught.value.statusCode == 503


def test_nameIsRefusedWhenTheListingFails(storedMetadata):
    with pytest.raises(CustomException) as caught:
        _validate(_service(listError=ConnectionError("storage unavailable")), name="sales_summary")

    assert caught.value.statusCode == 503


def test_nameIsRefusedWhenTheMetadataIsUnreadable(storedMetadata):
    storedMetadata.side_effect = lambda _url: io.BytesIO(b"{not json")

    with pytest.raises(CustomException) as caught:
        _validate(_service(), name="sales_summary")

    assert caught.value.statusCode == 503
