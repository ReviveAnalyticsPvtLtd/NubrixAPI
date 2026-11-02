"""Tests for the base-table guard on transformation output names."""

import asyncio
import io
import json
import os
from unittest import mock
from urllib.error import URLError

import pytest

from api.services.dataLoadService import DataLoadService
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
        column, _, jsonField = column.partition("->>")
        if jsonField:
            self.filters.append(lambda row: (row.get(column) or {}).get(jsonField) == value)
        else:
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
        if action == "update":
            self.project.fail("rowUpdate")
            for row in self._matches():
                row.update(values)
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
        self.forgotten = []
        self.removed = []
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
        # As on real storage: a case-insensitive prefix, so a hit is not yet the exact file.
        matches = [name for name in sorted(self.fileNames) if name.lower().startswith(search.lower())]
        return [{"name": name} for name in matches[:self.PAGE_LIMIT]]

    def download(self, path):
        self.fail("metadataRead")
        return json.dumps(self.metadata).encode("utf-8")

    def upload(self, path, file, file_options=None):
        self.fail("metadataWrite")
        if (file_options or {}).get("upsert") != "true" and path.rpartition("/")[2] in self.fileNames:
            raise ValueError("The resource already exists")
        self.metadata = json.loads(file)
        self.metadataWrites += 1

    def remove(self, paths):
        self.fail("tableRemove")
        # Real storage turns a lone string into a one-item list.
        for path in [paths] if isinstance(paths, str) else paths:
            self.removed.append(path)
            self.fileNames.discard(path.rpartition("/")[2])

    def forget(self, projectId, tableName):
        self.fail("cacheClear")
        self.forgotten.append(tableName)


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
        mock.patch("api.services.dataLoadService.updateProjectModifiedAt"),
        mock.patch("api.services.dataLoadService.urlopen", side_effect=currentMetadata),
        mock.patch("api.services.transformationService.forget_table", side_effect=lambda *args: holder["project"].forget(*args)),
        mock.patch("api.services.dataLoadService.forget_table", side_effect=lambda *args: holder["project"].forget(*args)),
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


@pytest.mark.parametrize("operation", ["metadataRead", "metadataWrite", "tableRemove", "list:metadata.json", "cacheClear"])
def test_failedStepKeepsTheTransformationAndCanBeRetried(projectStorage, operation):
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


def _draftRow(name="sales", transformationId="t1"):
    return {"transformation_id": transformationId, "project_id": "project_1", "transformation_name": name}


def _deleteTable(project, tableName):
    service = object.__new__(DataLoadService)
    service.client = project
    service.deleteTable(mock.Mock(projectId="project_1", tableName=tableName))


def _upload(project, tableName):
    project.fileNames.add(f"{tableName}.parquet")
    project.metadata[tableName] = {"isActive": True}


class _FakeExecutor:
    """Stands in for the sandbox: running the code is not what these tests are about."""

    def __init__(self, project):
        self.project = project

    def executeAndPreview(self, projectId, pythonCode, tableName):
        return [{"row": 1}], b"parquet"

    def apply(self, projectId, parquetBytes, tableName):
        self.project.fileNames.add(f"{tableName}.parquet")


def _workspaceService(project):
    service = _projectService(project)
    service._executor = _FakeExecutor(project)
    service._agent = mock.Mock()
    service._redis_client = lambda: mock.Mock(get=mock.Mock(return_value=None))
    return service


def _withMessages(row):
    message = {"message_id": "m1", "role": "assistant", "python_code": "code", "artifact": {"code": "graph"}}
    return {**row, "messages": [{"message_id": "m0", "role": "user", "content": "go"}, message]}


def _approveAndApply(project, transformationId="t1"):
    service = _workspaceService(project)

    def registerTables(projectId, userId=None):
        project.metadata.update({name.removesuffix(".parquet"): {} for name in project.fileNames if name.endswith(".parquet")})

    async def run():
        await service.approveMessage("project_1", transformationId, "m1")
        await service.applyTransformation("project_1", transformationId, "m1")

    with mock.patch("api.services.managementService.managementService.generateMetadata", side_effect=registerTables):
        asyncio.run(run())


def _rename(project, newName, transformationId="t1"):
    asyncio.run(_projectService(project).renameTransformation("project_1", transformationId, newName))


def test_deletingADraftLeavesAnUploadOfTheSameName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_draftRow()]))
    _upload(project, "sales")

    assert _delete(project)["status"] == "200"

    assert "sales.parquet" in project.fileNames
    assert "sales" in project.metadata
    assert project.forgotten == []
    assert project.rows == []


def test_deletingARolledBackDraftClearsTheKeyItLeftBehind(projectStorage):
    project = projectStorage(_FakeProject(metadata={"sales": {}, "summary": {}}, tables=("sales",), rows=[_draftRow("summary")]))

    assert _delete(project)["status"] == "200"

    assert list(project.metadata) == ["sales"]
    assert project.forgotten == ["summary"]


def test_aSimilarFileDoesNotCountAsTheDraftsTable(projectStorage):
    project = projectStorage(_FakeProject(metadata={"sales": {}}, rows=[_draftRow()]))
    project.fileNames |= {"sales.parquet.bak", "Sales.parquet"}

    assert _delete(project)["status"] == "200"

    assert project.metadata == {}
    assert {"sales.parquet.bak", "Sales.parquet"} <= project.fileNames


def test_aSimilarFileDoesNotMakeANameABaseTable(projectStorage):
    project = projectStorage(_FakeProject(metadata={"sales": {}}))
    project.fileNames |= {"sales.parquet.bak", "Sales.parquet"}

    assert _create(project, "sales")["transformation_name"] == "sales"


def test_failedFileCheckKeepsADraftAndItsKey(projectStorage):
    project = projectStorage(_FakeProject(metadata={"sales": {}}, rows=[_draftRow()]))
    project.failures["list:sales.parquet"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _delete(project)

    assert caught.value.statusCode == 503
    assert project.metadata == {"sales": {}}
    assert len(project.rows) == 1


def test_deleteRemovesTheTableTheArtifactNames(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={"before": {}, "after": {}},
        tables=("before", "after"),
        rows=[_appliedRow(name="after", tableName="before")],
    ))

    assert _delete(project)["status"] == "200"

    assert list(project.metadata) == ["after"]
    assert "before.parquet" not in project.fileNames
    assert "after.parquet" in project.fileNames
    assert project.forgotten == ["before"]


def test_failedTableRemoveChangesNothing(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["tableRemove"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException):
        _delete(project)

    assert project.metadataWrites == 0
    assert "summary" in project.metadata
    assert "summary.parquet" in project.fileNames


def test_cachesAreClearedOnceTheTableIsGone(projectStorage):
    project = _appliedProject(projectStorage)
    seenByCacheClear = {}
    realForget = project.forget

    def forget(projectId, tableName):
        seenByCacheClear.update(file="summary.parquet" in project.fileNames, key="summary" in project.metadata)
        realForget(projectId, tableName)

    project.forget = forget

    _delete(project)

    assert project.forgotten == ["summary"]
    assert seenByCacheClear == {"file": False, "key": False}


def test_deletingATableReleasesItFromItsTransformation(projectStorage):
    project = _appliedProject(projectStorage)

    _deleteTable(project, "summary")

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.forgotten == ["summary"]


def test_deletingATableLeavesOtherTransformationsAlone(projectStorage):
    project = _appliedProject(projectStorage)

    _deleteTable(project, "sales")

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "summary"}


def test_deletingATableStillWorksIfTheCacheClearFails(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["cacheClear"] = ConnectionError("redis unavailable")

    _deleteTable(project, "summary")

    assert "summary" not in project.metadata


def test_failedReleaseLeavesTheTableInPlace(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["rowUpdate"] = ConnectionError("database unavailable")

    with pytest.raises(CustomException):
        _deleteTable(project, "summary")

    assert "summary.parquet" in project.fileNames
    assert "summary" in project.metadata


def test_uploadUnderADeletedTablesNameSurvivesDeletingItsOldTransformation(projectStorage):
    project = _appliedProject(projectStorage)
    _deleteTable(project, "summary")
    _upload(project, "summary")

    assert _delete(project)["status"] == "200"

    assert "summary.parquet" in project.fileNames
    assert "summary" in project.metadata
    assert project.rows == []


def test_appliedTransformationCanBeDeletedAndItsNameAppliedAgain(projectStorage):
    project = projectStorage(_FakeProject(metadata={"sales": {}}, tables=("sales",), rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)
    assert project.rows[0]["latest_approved_artifact"]["table_name"] == "summary"

    _delete(project)
    assert list(project.metadata) == ["sales"]

    recreated = _create(project, "summary")
    project.rows[0].update(_withMessages({}))
    _approveAndApply(project, recreated["transformation_id"])

    assert "summary.parquet" in project.fileNames
    assert "summary" in project.metadata


def test_renamedTransformationKeepsItsOldNameUntilItsOldTableIsDeleted(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("before"))]))
    _approveAndApply(project)
    _rename(project, "after")
    _approveAndApply(project)
    assert project.rows[0]["latest_approved_artifact"]["table_name"] == "after"

    with pytest.raises(CustomException) as caught:
        _create(project, "before")
    assert caught.value.statusCode == 409

    _deleteTable(project, "before")

    assert _create(project, "before")["transformation_name"] == "before"


def test_rolledBackTransformationCanBeDeletedAndItsNameReused(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)

    service = _workspaceService(project)
    service.getMessages = mock.AsyncMock(return_value=[])
    with mock.patch("api.services.managementService.managementService.generateMetadata"):
        asyncio.run(service.rollbackTransformation("project_1", "t1", "m0"))
    assert project.rows[0]["latest_approved_artifact"] is None
    assert "summary.parquet" not in project.fileNames

    _delete(project)

    assert project.metadata == {}
    assert _create(project, "summary")["transformation_name"] == "summary"


def _rollback(project, messageId="m0", transformationId="t1"):
    service = _workspaceService(project)
    service.getMessages = mock.AsyncMock(return_value=[])
    with mock.patch("api.services.managementService.managementService.generateMetadata"):
        return asyncio.run(service.rollbackTransformation("project_1", transformationId, messageId))


def test_rollingBackADraftLeavesAnUploadOfTheSameName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _upload(project, "sales")

    _rollback(project)

    assert "sales.parquet" in project.fileNames
    assert "sales" in project.metadata
    assert project.removed == []
    assert project.forgotten == []


def test_rollingBackANeverAppliedDraftTouchesNothing(projectStorage):
    # A key with no file behind it is left for delete to clear.
    project = projectStorage(_FakeProject(metadata={"sales": {}}, rows=[_withMessages(_draftRow())]))

    _rollback(project)

    assert project.removed == []
    assert project.metadataWrites == 0
    assert project.forgotten == []
    assert project.rows[0]["messages"] == []


def test_rollingBackRemovesTheTableTheArtifactNamesWithItsKeyAndCaches(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={"before": {}, "after": {}},
        tables=("before", "after"),
        rows=[_withMessages(_appliedRow(name="after", tableName="before"))],
    ))

    _rollback(project)

    assert "before.parquet" not in project.fileNames
    assert list(project.metadata) == ["after"]
    assert project.forgotten == ["before"]
    assert "after.parquet" in project.fileNames
    assert project.rows[0]["latest_approved_artifact"] is None


@pytest.mark.parametrize("operation", ["tableRemove", "list:metadata.json", "metadataRead", "metadataWrite", "cacheClear"])
def test_failedRollbackCleanupChangesNoRowAndCanBeRetried(projectStorage, operation):
    project = _appliedProject(projectStorage)
    project.rows[0].update(_withMessages({}))
    project.failures[operation] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _rollback(project)

    assert caught.value.statusCode == 503
    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "summary"}
    assert len(project.rows[0]["messages"]) == 2

    project.failures.clear()
    _rollback(project)
    assert "summary.parquet" not in project.fileNames
    assert list(project.metadata) == ["sales"]
    assert project.rows[0]["latest_approved_artifact"] is None

