"""Tests for the table name check that guards every data upload."""

import io
import os
from unittest import mock
from urllib.error import URLError

import pytest

from api.services.managementService import ManagementService
from test.storageListingFixtures import FILE_URL, PROJECT_ID, FakeBucket, fakeClient, largeFolder
from utils.exceptionHandler import CustomException


@pytest.fixture(autouse=True)
def _fileUrl():
    with mock.patch.dict(os.environ, {"FILE_URL": FILE_URL}):
        yield


def _validate(bucket, tableName="sales"):
    service = object.__new__(ManagementService)
    service.client = fakeClient(bucket)
    service.validateTableNameAvailable(PROJECT_ID, tableName)


def _readingStoredFiles(bucket):
    return mock.patch("api.services.managementService.readJson", side_effect=bucket.readJson)


def test_taken_name_is_refused():
    bucket = FakeBucket({"metadata.json": {"sales": {}}})

    with _readingStoredFiles(bucket), pytest.raises(CustomException) as caught:
        _validate(bucket)

    assert caught.value.statusCode == 409


def test_taken_name_is_refused_when_metadata_sits_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"sales": {}}})

    with _readingStoredFiles(bucket), pytest.raises(CustomException) as caught:
        _validate(bucket)

    assert caught.value.statusCode == 409


def test_free_name_passes():
    bucket = largeFolder({"metadata.json": {"sales": {}}})

    with _readingStoredFiles(bucket):
        _validate(bucket, tableName="costs")


def test_missing_metadata_passes():
    with mock.patch("api.services.managementService.readJson") as urlopen:
        _validate(largeFolder())

    urlopen.assert_not_called()


def test_failed_listing_refuses():
    with pytest.raises(CustomException) as caught:
        _validate(FakeBucket(listError=ConnectionError("storage down")))

    assert caught.value.statusCode == 503
    assert caught.value.uiMessage == "We could not check this table name right now. Please try again."


def test_failed_metadata_read_refuses():
    bucket = FakeBucket({"metadata.json": {}})

    with mock.patch("api.services.managementService.readJson", side_effect=URLError("connection reset")), pytest.raises(CustomException) as caught:
        _validate(bucket)

    assert caught.value.statusCode == 503


def test_unparseable_metadata_refuses():
    bucket = FakeBucket({"metadata.json": {}})

    with mock.patch("api.services.managementService.readJson", side_effect=ValueError("not json")), pytest.raises(CustomException) as caught:
        _validate(bucket)

    assert caught.value.statusCode == 503
