"""Tests for the project folder reads that need every file, not one named file."""

import os
from unittest import mock

import pytest

from api.services.dashboardService import DashboardService
from api.services.managementService import ManagementService
from test.storageListingFixtures import FILE_URL, MAX_REMOVE_PATHS, NEWER, PROJECT_ID, FakeBucket, earlierTables, fakeClient, largeFolder
from utils.storageListing import listProjectFiles


@pytest.fixture(autouse=True)
def _fileUrl():
    with mock.patch.dict(os.environ, {"FILE_URL": FILE_URL}):
        yield


def _management(bucket):
    service = object.__new__(ManagementService)
    service.client = fakeClient(bucket)
    return service


def test_listing_returns_every_name_across_pages():
    names = earlierTables(2500)

    files = listProjectFiles(fakeClient(FakeBucket(fileNames=names)), PROJECT_ID)

    assert [file["name"] for file in files] == names


def test_listing_stops_on_a_final_full_page():
    bucket = FakeBucket(fileNames=earlierTables(1000))
    bucket.list = mock.Mock(side_effect=bucket.list)

    files = listProjectFiles(fakeClient(bucket), PROJECT_ID)

    assert len(files) == 1000
    assert bucket.list.call_count == 2


def test_listing_passes_a_failed_read_to_the_caller():
    with pytest.raises(ConnectionError):
        listProjectFiles(fakeClient(FakeBucket(listError=ConnectionError("storage down"))), PROJECT_ID)


def test_all_columns_cover_every_table_in_a_large_folder():
    bucket = largeFolder()
    service = object.__new__(DashboardService)
    service._getDataTypes = lambda projectId, tableName: []

    with mock.patch("api.services.dashboardService.client", fakeClient(bucket)):
        columns = service.getAllColumns(PROJECT_ID)

    assert len(columns) == 150


def test_full_metadata_scan_covers_every_table_in_a_large_folder():
    bucket = largeFolder()
    service = _management(bucket)
    service._attributeInfoFunc = mock.Mock(return_value=("info", None))
    service.metadataGenerator = mock.Mock()
    service._parseModelJsonOutput = mock.Mock(return_value={})

    service._generateMetadata(PROJECT_ID)

    assert service._attributeInfoFunc.call_count == 150


def test_metadata_refresh_sees_a_changed_table_past_the_first_names():
    bucket = largeFolder({"metadata.json": {"orders": {"isActive": True}}}, modified={"a_table_0120.parquet": NEWER})
    service = _management(bucket)
    service._generateMetadata = mock.Mock(return_value={})

    with mock.patch("api.services.managementService.urlopen", side_effect=bucket.readUrl), mock.patch("api.services.managementService.updateProjectModifiedAt"):
        service.generateMetadata(PROJECT_ID)

    service._generateMetadata.assert_called_once_with(projectId=PROJECT_ID, userId=None, tableNames=["a_table_0120"])


def test_project_delete_removes_every_file_in_a_large_folder():
    names = earlierTables(2500)
    bucket = FakeBucket(fileNames=names)

    _management(bucket).deleteProject(PROJECT_ID)

    removed = [path for call in bucket.removeCalls for path in call]
    assert sorted(removed) == sorted(os.path.join(PROJECT_ID, name) for name in names)
    assert max(len(call) for call in bucket.removeCalls) <= MAX_REMOVE_PATHS


def test_project_delete_leaves_storage_alone_for_an_empty_folder():
    bucket = FakeBucket()

    _management(bucket).deleteProject(PROJECT_ID)

    assert bucket.removeCalls == []
