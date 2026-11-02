"""Tests for the base-table guard on transformation output names."""

import asyncio
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


def _service(fileNames=("metadata.json", "sales.parquet"), listError=None):
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
        _validate(_service(fileNames=[*earlierFiles, "metadata.json", "sales.parquet"]))

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


class _FakeRows:
    """The chained query builder over one project's `transformations` rows."""

    def __init__(self, project):
        self.project = project
        self.filters = []
        self.change = None

    def select(self, *_args):
        return self

    def eq(self, column, value):
        self.filters.append(lambda row: row.get(column) == value)
        return self

    def neq(self, column, value):
        self.filters.append(lambda row: row.get(column) != value)
        return self

    def limit(self, _count):
        return self

    def insert(self, row):
        self.change = ("insert", row)
        return self

    def update(self, values):
        self.change = ("update", values)
        return self

    def delete(self):
        self.change = ("delete", None)
        return self

    def _matches(self):
        return [row for row in self.project.rows if all(check(row) for check in self.filters)]

    def execute(self):
        action, values = self.change or ("select", None)
        if action == "insert":
            self.project.rows.append({"transformation_id": f"t{len(self.project.rows) + 100}", **values})
            return mock.Mock(data=[self.project.rows[-1]])
        if action == "delete":
            self.project.fail("rowDelete")
            self.project.rows = [row for row in self.project.rows if row not in self._matches()]
            return mock.Mock(data=[])
        return mock.Mock(data=self._matches())


class _FakeProject:
    """One project's storage and rows, so the real service methods can run in sequence."""

    PAGE_LIMIT = 100

    def __init__(self, metadata, tables=(), rows=()):
        self.metadata = dict(metadata)
        self.fileNames = {"metadata.json", *(f"{table}.parquet" for table in tables)}
        self.rows = [dict(row) for row in rows]
        self.failures = {}
        self.metadataWrites = 0
        self.storage = mock.Mock()
        self.storage.from_.return_value = self

    def fail(self, operation):
        if operation in self.failures:
            raise self.failures[operation]

    def table(self, _name):
        return _FakeRows(self)

    def list(self, path=None, options=None):
        search = (options or {}).get("search", "")
        self.fail(f"list:{search}")
        matches = [name for name in sorted(self.fileNames) if name.startswith(search)]
        return [{"name": name} for name in matches[:self.PAGE_LIMIT]]

    def download(self, path):
        self.fail("metadataRead")
        return json.dumps(self.metadata).encode("utf-8")

    def upload(self, path, file, file_options=None):
        self.fail("metadataWrite")
        self.metadata = json.loads(file)
        self.metadataWrites += 1

    def remove(self, paths):
        self.fail("tableRemove")
        for path in paths:
            self.fileNames.discard(path.rpartition("/")[2])


def _projectService(project):
    service = object.__new__(TransformationService)
    service.supabase = project
    return service


def _create(project, name):
    return asyncio.run(_projectService(project).createTransformation("project_1", name, None))


def _delete(project, transformationId="t1"):
    return asyncio.run(_projectService(project).deleteTransformation("project_1", transformationId))


def _appliedRow(name="summary", tableName=None, transformationId="t1"):
    return {
        "transformation_id": transformationId,
        "project_id": "project_1",
        "transformation_name": name,
        "latest_approved_artifact": {"table_name": tableName or name},
    }


@pytest.fixture
def projectStorage():
    """Serves the project's current metadata.json the way the public file route does."""
    holder = {}

    def currentMetadata(_url):
        return io.BytesIO(json.dumps(holder["project"].metadata).encode("utf-8"))

    with (
        mock.patch("urllib.request.urlopen", side_effect=currentMetadata),
        mock.patch("api.services.transformationService.updateProjectModifiedAt"),
    ):
        yield lambda project: holder.update(project=project) or project


def _appliedProject(projectStorage, **overrides):
    return projectStorage(_FakeProject(
        metadata={"sales": {"isActive": True}, "summary": {"isActive": True}},
        tables=("sales", "summary"),
        rows=[_appliedRow()],
        **overrides,
    ))


def test_deletingATransformationRemovesItsTableAndKey(projectStorage):
    project = _appliedProject(projectStorage)

    assert _delete(project)["status"] == "200"

    assert list(project.metadata) == ["sales"]
    assert "summary.parquet" not in project.fileNames
    assert "sales.parquet" in project.fileNames
    assert project.rows == []


def test_deletedTransformationNameCanBeReused(projectStorage):
    project = _appliedProject(projectStorage)
    _delete(project)

    assert _create(project, "summary")["transformation_name"] == "summary"


def test_nameLeftBehindByAnEarlierDeleteCanBeReused(projectStorage):
    project = projectStorage(_FakeProject(metadata={"summary": {"isActive": True}}))

    assert _create(project, "summary")["transformation_name"] == "summary"


def test_realBaseTableStaysRefusedAfterADelete(projectStorage):
    project = _appliedProject(projectStorage)
    _delete(project)

    with pytest.raises(CustomException) as caught:
        _create(project, "sales")

    assert caught.value.statusCode == 409


def test_tableFilePastTheFirstPageOfAListingIsStillFound(projectStorage):
    earlierTables = [f"a_table_{index:03}" for index in range(150)]
    project = projectStorage(_FakeProject(metadata={"sales": {}}, tables=[*earlierTables, "sales"]))

    with pytest.raises(CustomException) as caught:
        _create(project, "sales")

    assert caught.value.statusCode == 409


def test_nameIsRefusedWhenTheTableFileCheckFails(projectStorage):
    project = projectStorage(_FakeProject(metadata={"summary": {}}))
    project.failures["list:summary.parquet"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _create(project, "summary")

    assert caught.value.statusCode == 503
    assert project.rows == []


def test_oldNameOfARenamedTransformationStaysRefusedWhileItsTableExists(projectStorage):
    # Applying under a new name leaves the old table in place, so it is still data to protect.
    project = projectStorage(_FakeProject(
        metadata={"before": {}, "after": {}},
        tables=("before", "after"),
        rows=[_appliedRow(name="after")],
    ))

    with pytest.raises(CustomException) as caught:
        _create(project, "before")

    assert caught.value.statusCode == 409


def test_oldNameOfARenamedTransformationIsFreeOnceItsTableIsGone(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={"before": {}, "after": {}},
        tables=("after",),
        rows=[_appliedRow(name="after")],
    ))

    assert _create(project, "before")["transformation_name"] == "before"


@pytest.mark.parametrize("operation", ["metadataRead", "metadataWrite", "tableRemove", "list:metadata.json"])
def test_failedStorageStepKeepsTheTransformationAndCanBeRetried(projectStorage, operation):
    project = _appliedProject(projectStorage)
    project.failures[operation] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _delete(project)

    assert caught.value.statusCode == 503
    assert len(project.rows) == 1

    project.failures.clear()
    assert _delete(project)["status"] == "200"
    assert list(project.metadata) == ["sales"]
    assert "summary.parquet" not in project.fileNames
    assert project.rows == []


def test_failedRowDeleteIsReportedAndCanBeRetried(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["rowDelete"] = ConnectionError("database unavailable")

    with pytest.raises(CustomException):
        _delete(project)

    assert len(project.rows) == 1

    project.failures.clear()
    assert _delete(project)["status"] == "200"
    assert project.rows == []


def test_deletingANeverAppliedTransformationLeavesMetadataAlone(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={"sales": {}},
        tables=("sales",),
        rows=[{"transformation_id": "t1", "project_id": "project_1", "transformation_name": "draft"}],
    ))

    assert _delete(project)["status"] == "200"

    assert project.metadataWrites == 0
    assert list(project.metadata) == ["sales"]


def test_deletingInAProjectWithNoMetadataFileStillWorks(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_appliedRow()]))
    project.fileNames.discard("metadata.json")

    assert _delete(project)["status"] == "200"

    assert project.metadataWrites == 0
    assert project.rows == []
