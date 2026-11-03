"""Tests for the base-table guard on transformation output names."""

import asyncio
import copy
import io
import json
import os
from unittest import mock
from urllib.error import URLError

import pandas as pd
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

    def is_(self, column, value):
        assert value == "null"
        column, _, jsonField = column.partition("->>")
        if jsonField:
            self.filters.append(lambda row: (row.get(column) or {}).get(jsonField) is None)
        else:
            self.filters.append(lambda row: row.get(column) is None)
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
            updated = self._matches()
            for row in updated:
                row.update(values)
            if self.project.afterRowUpdate:
                self.project.afterRowUpdate.pop(0)()
            return mock.Mock(data=updated)
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
        self.applied = []
        # Every table write the transformation started, including ones storage refused.
        self.writeAttempts = []
        # Who wrote each table file last, to tell an upload from a transformation's output.
        self.writers = {}
        # Bumped on every table write, as storage moves updated_at on every write.
        self.versions = {}
        self.afterRowUpdate = []
        self.beforeTableWrite = None
        self.duringSandbox = None
        self.duringTableWrite = None
        self.storage = mock.Mock()
        self.storage.from_.return_value = self

    def fail(self, operation):
        failure = self.failures.get(operation)
        if isinstance(failure, list):
            # One failure, or None for a success, per call in turn.
            failure = failure.pop(0) if failure else None
        if failure:
            raise failure

    def table(self, _name):
        return _FakeRows(self)

    def list(self, path=None, options=None):
        search = (options or {}).get("search", "")
        self.fail(f"list:{search}")
        # As on real storage: a case-insensitive prefix, so a hit is not yet the exact file.
        matches = [name for name in sorted(self.fileNames) if name.lower().startswith(search.lower())]
        return [_storageEntry(name, self.versions.get(name, 0)) for name in matches[:self.PAGE_LIMIT]]

    def download(self, path):
        self.fail("metadataRead")
        return json.dumps(self.metadata).encode("utf-8")

    def upload(self, path, file, file_options=None):
        fileName = path.rpartition("/")[2]
        if fileName.endswith(".parquet"):
            self.fail("tableWrite")
            hook, self.beforeTableWrite = self.beforeTableWrite, None
            if hook:
                hook()
            self.fileNames.add(fileName)
            self.writers[fileName] = "upload"
            self.versions[fileName] = self.versions.get(fileName, 0) + 1
            return
        self.fail("metadataWrite")
        if (file_options or {}).get("upsert") != "true" and fileName in self.fileNames:
            raise ValueError("The resource already exists")
        self.metadata = json.loads(file)
        self.metadataWrites += 1

    def remove(self, paths):
        self.fail("tableRemove")
        # Real storage turns a lone string into a one-item list.
        for path in [paths] if isinstance(paths, str) else paths:
            self.removed.append(path)
            self.fileNames.discard(path.rpartition("/")[2])
            self.writers.pop(path.rpartition("/")[2], None)

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


def _storageEntry(fileName, version=0):
    # As on real storage, every write moves updated_at and the eTag.
    return {"name": fileName, "id": f"id-{fileName}", "updated_at": str(version), "metadata": {"eTag": f"v{version}"}}


def _recordedFile(tableName, version=0):
    entry = _storageEntry(f"{tableName}.parquet", version)
    return {"id": entry["id"], "updated_at": entry["updated_at"], "eTag": entry["metadata"]["eTag"]}


def _appliedRow(name="summary", tableName=None, transformationId="t1", recorded=False):
    artifact = {"table_name": tableName or name}
    if recorded:
        artifact["table_file"] = _recordedFile(tableName or name)
    return {
        "transformation_id": transformationId,
        "project_id": "project_1",
        "transformation_name": name,
        "latest_approved_artifact": artifact,
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
        mock.patch("api.services.managementService.urlopen", side_effect=currentMetadata),
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

    with pytest.raises(CustomException) as caught:
        _delete(project)

    assert caught.value.statusCode == 503
    assert "database unavailable" not in caught.value.uiMessage
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
        hook, self.project.duringSandbox = self.project.duringSandbox, None
        if hook:
            hook()
        return [{"row": 1}], b"parquet"

    def apply(self, projectId, parquetBytes, tableName, overwrite=True):
        self.project.writeAttempts.append(tableName)
        hook, self.project.duringTableWrite = self.project.duringTableWrite, None
        if hook:
            hook()
        if not overwrite and f"{tableName}.parquet" in self.project.fileNames:
            # As the real executor does when storage refuses an upload over an existing file.
            raise CustomException(ValueError("The resource already exists"), statusCode=409, uiMessage="exists")
        self.project.applied.append(tableName)
        self.project.fileNames.add(f"{tableName}.parquet")
        self.project.writers[f"{tableName}.parquet"] = "transformation"
        self.project.versions[f"{tableName}.parquet"] = self.project.versions.get(f"{tableName}.parquet", 0) + 1


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


def test_failedTableRemoveKeepsTheTableWithItsTransformation(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["tableRemove"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException):
        _deleteTable(project, "summary")

    assert "summary" in project.metadata
    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "summary"}


def test_failedReleaseStillDeletesTheTableAndLeavesItsNameSafeToLoad(projectStorage):
    project = _appliedProject(projectStorage)
    project.failures["rowUpdate"] = ConnectionError("database unavailable")

    _deleteTable(project, "summary")

    assert "summary.parquet" not in project.fileNames
    assert "summary" not in project.metadata
    project.failures.clear()
    _load(project, "summary")
    _delete(project)
    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata


def test_deletingATableLeavesTheSameNameInOtherProjectsAlone(projectStorage):
    project = _appliedProject(projectStorage)
    project.rows.append({**_appliedRow(transformationId="t2"), "project_id": "project_2"})

    _deleteTable(project, "summary")

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.rows[1]["latest_approved_artifact"] == {"table_name": "summary"}


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


def _rollbackToAfterTheApply(project):
    project.rows[0]["messages"].append({"message_id": "m2", "role": "user", "content": "more"})
    return _rollback(project, "m2")


def _approve(project, transformationId="t1"):
    asyncio.run(_workspaceService(project).approveMessage("project_1", transformationId, "m1"))


def _apply(project, transformationId="t1"):
    with mock.patch("api.services.managementService.managementService.generateMetadata"):
        asyncio.run(_workspaceService(project).applyTransformation("project_1", transformationId, "m1"))


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


def test_draftNamedAfterAnUploadIsRefusedAtApprove(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _upload(project, "sales")

    with pytest.raises(CustomException) as caught:
        _approve(project)

    assert caught.value.statusCode == 409


def test_applyIsRefusedOnceAnUploadTakesTheName(projectStorage):
    # Approve passed before the upload, so only apply's own check stands in the way.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    _upload(project, "sales")

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    assert project.applied == []
    assert project.rows[0].get("latest_approved_artifact") is None
    assert not project.rows[0]["messages"][1]["artifact"].get("is_approved")


def test_applyIsRefusedWhenTheNameCannotBeChecked(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    project.failures["list:metadata.json"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 503
    assert project.applied == []


def test_transformationCanReapplyItsOwnTable(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)

    _approveAndApply(project)

    assert project.applied == ["summary", "summary"]


def test_tableWithNoArtifactIsRefusedUntilItIsDeleted(projectStorage):
    # An upload of the draft's name looks like this, and so does a row from before artifacts proved ownership.
    project = projectStorage(_FakeProject(metadata={"summary": {}}, tables=("summary",), rows=[_withMessages(_draftRow("summary"))]))

    with pytest.raises(CustomException) as caught:
        _approve(project)
    assert caught.value.statusCode == 409

    _deleteTable(project, "summary")
    _approveAndApply(project)
    assert project.applied == ["summary"]


def test_fileWithNoKeyIsRefused(projectStorage):
    # An upload has no key until its metadata is generated, and keeps none if that fails.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_withMessages(_draftRow())]))

    with pytest.raises(CustomException) as caught:
        _approveAndApply(project)

    assert caught.value.statusCode == 409
    assert project.applied == []
    assert project.writeAttempts == []


def test_fileWithNoKeyIsRefusedAtApprove(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_withMessages(_draftRow())]))

    with pytest.raises(CustomException) as caught:
        _approve(project)

    assert caught.value.statusCode == 409


def test_rollbackToACheckpointRewritesItsOwnTable(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)

    _rollbackToAfterTheApply(project)

    assert project.applied == ["summary", "summary"]
    assert project.rows[0]["latest_approved_artifact"]["table_name"] == "summary"


def test_rollbackToACheckpointDoesNotOverwriteAnUploadOfItsName(projectStorage):
    # Deleting the table from the table list clears the artifact, but its messages stay approved.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)
    _deleteTable(project, "summary")
    _upload(project, "summary")

    with pytest.raises(CustomException) as caught:
        _rollbackToAfterTheApply(project)

    assert caught.value.statusCode == 409
    assert project.applied == ["summary"]
    assert len(project.rows[0]["messages"]) == 3
    assert project.rows[0]["latest_approved_artifact"] is None


def _register(project):
    def register(projectId, tableNames, userId=None):
        project.metadata.update({name: {"isActive": True} for name in tableNames})
        return {}
    return register


def _upload_file(name):
    upload = mock.Mock(filename=name)
    upload.read = mock.AsyncMock(return_value=b"amount\n1\n")
    return upload


def _connection(table):
    return mock.Mock(host="db.test", port=5432, user="reader", password="unused", db="shop", table=table, projectId="project_1")


def _loadVia(kind, service, tableName, pdfTables=1):
    """Run one load route so that it stores tableName."""
    amounts = pd.DataFrame({"amount": [1]})
    if kind == "csv":
        asyncio.run(service.loadCsvData("project_1", [_upload_file(f"{tableName}.csv")]))
    elif kind == "excel":
        base, _, sheet = tableName.rpartition("_")
        with mock.patch("api.services.dataLoadService.pd.read_excel", return_value={sheet: amounts}):
            asyncio.run(service.loadExcelData("project_1", [_upload_file(f"{base}.xlsx")]))
    elif kind == "pdf":
        extractor = mock.Mock(dpi=100)
        extractor.extractPagesParallel = mock.AsyncMock(return_value=[{"table": mock.Mock(rows=[[1]])}] * pdfTables)
        extractor.planMerge.return_value = mock.Mock(groups=[mock.Mock(canonicalHeaders=["amount"], tableIndices=[index]) for index in range(pdfTables)])
        document = mock.MagicMock()
        document.__enter__.return_value = []
        with (
            mock.patch("api.services.dataLoadService.fitz.open", return_value=document),
            mock.patch("api.services.dataLoadService.PdfTableExtractor", return_value=extractor),
        ):
            asyncio.run(service.loadPdfData("project_1", [_upload_file(f"{tableName.removesuffix('_table1')}.pdf")]))
    elif kind in ("mysql", "postgresql"):
        with (
            mock.patch("api.services.dataLoadService.URL"),
            mock.patch("api.services.dataLoadService.create_engine"),
            mock.patch("api.services.dataLoadService.pd.read_sql", return_value=amounts),
        ):
            (service.loadMySql if kind == "mysql" else service.loadPostgreSQL)(_connection(tableName))
    else:
        with mock.patch("api.services.dataLoadService.MongoClient") as mongo:
            mongo.return_value.__getitem__.return_value.__getitem__.return_value.find.return_value = [{"amount": 1}]
            service.loadMongoDB(mock.Mock(connectionString="mongodb://db.test", db="shop", collection=tableName, projectId="project_1"))


def _load(project, tableName, beforeWrite=None, kind="mongo", parquetError=None, pdfTables=1):
    """Load a table through the real loader. beforeWrite runs after the name is accepted, before the file is written.

    parquetError makes the load fail after it accepts the name, while it prepares its file.
    """
    from api.services.managementService import managementService

    service = object.__new__(DataLoadService)
    service.client = project
    project.beforeTableWrite = beforeWrite
    with (
        mock.patch.object(managementService, "client", project),
        mock.patch.object(managementService, "generateMetadataForTables", side_effect=_register(project)),
        mock.patch("pandas.DataFrame.to_parquet", side_effect=parquetError),
    ):
        _loadVia(kind, service, tableName, pdfTables)


def _loadPdf(project, baseName="report"):
    _load(project, f"{baseName}_table1", kind="pdf")


def _applyWithoutRegistering(project):
    with mock.patch("api.services.managementService.managementService.generateMetadata", side_effect=RuntimeError("model unavailable")):
        asyncio.run(_workspaceService(project).applyTransformation("project_1", "t1", "m1"))


@pytest.mark.parametrize("cleanUp", [_delete, _rollback], ids=["delete", "rollback"])
def test_uploadBetweenAFailedCleanupAndItsRetrySurvives(projectStorage, cleanUp):
    # The table and its key are gone, but the transformation still names the table.
    project = _appliedProject(projectStorage)
    project.rows[0].update(_withMessages({}))
    project.failures["cacheClear"] = ConnectionError("redis unavailable")
    with pytest.raises(CustomException):
        cleanUp(project)
    project.failures.clear()

    _load(project, "summary")
    cleanUp(project)

    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata


def test_uploadBetweenAFailedRollbackAndAnApplyIsNotOverwritten(projectStorage):
    project = _appliedProject(projectStorage)
    project.rows[0].update(_withMessages({}))
    project.failures["cacheClear"] = ConnectionError("redis unavailable")
    with pytest.raises(CustomException):
        _rollback(project)
    project.failures.clear()

    _load(project, "summary")
    with pytest.raises(CustomException) as caught:
        _approveAndApply(project)

    assert caught.value.statusCode == 409
    assert project.writers["summary.parquet"] == "upload"


def test_uploadAfterAnUnregisteredApplySurvivesDeletingTheTransformation(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approve(project)
    _applyWithoutRegistering(project)
    assert "summary" not in project.metadata

    _load(project, "summary")
    _delete(project)

    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata


def test_unregisteredApplyCanBeAppliedAgain(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approve(project)
    _applyWithoutRegistering(project)

    _approveAndApply(project)

    assert project.applied == ["summary", "summary"]
    assert "summary" in project.metadata


def test_applyIsRefusedWhenAnUploadTakesTheNameWhileItsCodeRuns(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    project.duringSandbox = lambda: _load(project, "sales")

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    assert project.applied == []
    assert project.writers["sales.parquet"] == "upload"


def test_rollbackIsRefusedWhenAnUploadTakesTheNameWhileItsCodeRuns(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approveAndApply(project)
    _deleteTable(project, "summary")
    project.duringSandbox = lambda: _load(project, "summary")

    with pytest.raises(CustomException) as caught:
        _rollbackToAfterTheApply(project)

    assert caught.value.statusCode == 409
    assert project.applied == ["summary"]
    assert project.writers["summary.parquet"] == "upload"


def test_applyIsRefusedWhenALoadAcceptsTheNameBeforeItWrites(projectStorage):
    # The load has accepted the name but is still preparing its file.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    loader = object.__new__(DataLoadService)
    loader.client = project
    project.afterRowUpdate.append(lambda: loader._releaseTableName("project_1", "sales"))

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    assert project.applied == []
    assert project.rows[0].get("latest_approved_artifact") is None


def test_loadWrittenAfterAnApplyTakesTheNameFromTheTransformation(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)

    _load(project, "sales", beforeWrite=lambda: _apply(project))

    assert project.applied == ["sales"]
    assert project.rows[0]["latest_approved_artifact"] is None
    _delete(project)
    assert project.writers["sales.parquet"] == "upload"
    assert "sales" in project.metadata


def test_loadIsRefusedWhenItCannotTakeTheName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_appliedRow()]))
    project.failures["rowUpdate"] = ConnectionError("database unavailable")

    with pytest.raises(CustomException) as caught:
        _load(project, "summary")

    assert caught.value.statusCode == 503
    assert "summary.parquet" not in project.fileNames


def test_pdfLoadIsRefusedForANameAlreadyInTheProject(projectStorage):
    project = projectStorage(_FakeProject(metadata={"report_table1": {}}, tables=("report_table1",)))

    with pytest.raises(CustomException) as caught:
        _loadPdf(project)

    assert caught.value.statusCode == 409
    assert project.writers == {}


def test_pdfLoadTakesItsNamesFromTransformations(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_appliedRow("report_table1")]))

    _loadPdf(project)

    assert project.writers == {"report_table1.parquet": "upload"}
    assert project.rows[0]["latest_approved_artifact"] is None


def test_applyIsRefusedWhenTheNameCannotBeClaimed(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    project.failures["rowUpdate"] = ConnectionError("database unavailable")

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 503
    assert project.applied == []


def test_applyIsRefusedWhenAnUploadIsWrittenAfterItsFirstCheck(projectStorage):
    # The load accepted the name before the claim, so the claim survives; the second check finds its file.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)

    def uploadArrives():
        project.fileNames.add("sales.parquet")
        project.writers["sales.parquet"] = "upload"

    project.afterRowUpdate.append(uploadArrives)

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    # Refused by the second check, before uploading the table.
    assert project.writeAttempts == []
    assert project.writers["sales.parquet"] == "upload"
    assert project.rows[0].get("latest_approved_artifact") is None


def test_loadAcceptedWhileTheTableIsWrittenKeepsTheName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    loader = object.__new__(DataLoadService)
    loader.client = project
    project.duringTableWrite = lambda: loader._releaseTableName("project_1", "sales")

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    assert project.rows[0].get("latest_approved_artifact") is None


def test_retriedApplyIsRefusedOnceALoadAcceptsItsName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approve(project)
    _applyWithoutRegistering(project)
    refused = []

    def retry():
        with pytest.raises(CustomException) as caught:
            _apply(project)
        refused.append(caught.value.statusCode)

    _load(project, "summary", beforeWrite=retry)

    assert refused == [409]
    assert project.writers["summary.parquet"] == "upload"


LOADS = {"csv": "sales", "excel": "sales_q1", "pdf": "sales_table1", "mysql": "sales", "postgresql": "sales", "mongo": "sales"}


@pytest.mark.parametrize("kind", LOADS)
def test_everyLoadRefusesANameAlreadyInTheProject(projectStorage, kind):
    name = LOADS[kind]
    project = projectStorage(_FakeProject(metadata={name: {}}, tables=(name,)))

    with pytest.raises(CustomException) as caught:
        _load(project, name, kind=kind)

    assert caught.value.statusCode == 409
    assert project.writers == {}


@pytest.mark.parametrize("kind", LOADS)
def test_everyLoadTakesItsNameBeforeAndAfterWriting(projectStorage, kind):
    name = LOADS[kind]
    project = projectStorage(_FakeProject(metadata={}, rows=[_appliedRow(name)]))
    seenAtWrite = []

    def claimAgain():
        # A transformation claims the name between the load's check and its write.
        seenAtWrite.append(project.rows[0]["latest_approved_artifact"])
        project.rows[0]["latest_approved_artifact"] = {"table_name": name}

    _load(project, name, beforeWrite=claimAgain, kind=kind)

    assert seenAtWrite == [None]
    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.writers == {f"{name}.parquet": "upload"}


def _writeFails():
    raise CustomException(ConnectionError("storage unavailable"), statusCode=500, uiMessage="Failed to apply transformation.")


def _claimWithNoFile(projectStorage, name="summary"):
    """An apply whose write failed: the row keeps its claim, with no file and no key."""
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow(name))]))
    _approve(project)
    project.duringTableWrite = _writeFails
    with pytest.raises(CustomException):
        _apply(project)
    assert project.rows[0]["latest_approved_artifact"] == {"table_name": name}
    assert f"{name}.parquet" not in project.fileNames
    # So a rollback to a checkpoint has one to go back to.
    project.rows[0]["messages"][1]["artifact"]["is_approved"] = True
    project.rows[0]["messages"].append({"message_id": "m2", "role": "user", "content": "more"})
    return project


def _inAnotherRequest(call):
    """Run a whole second request to the end while this one waits, as another worker would."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(call).result()


def _afterFirstCheck(service, action):
    realValidate = service._validate_transformation_table_name
    checks = []

    def validate(*args, **kwargs):
        result = realValidate(*args, **kwargs)
        checks.append(kwargs)
        if len(checks) == 1:
            action()
        return result

    service._validate_transformation_table_name = validate


def _run(service, operation):
    with mock.patch("api.services.managementService.managementService.generateMetadata"):
        if operation == "apply":
            return asyncio.run(service.applyTransformation("project_1", "t1", "m1"))
        service.getMessages = mock.AsyncMock(return_value=[])
        return asyncio.run(service.rollbackTransformation("project_1", "t1", "m2"))


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_retryIsRefusedWhenALoadTakesTheNameBeforeTheClaimIsWritten(projectStorage, operation):
    # The load releases the row's claim and writes its whole file between the retry's first check and its claim.
    project = _claimWithNoFile(projectStorage)
    service = _workspaceService(project)
    _afterFirstCheck(service, lambda: _load(project, "summary"))

    with pytest.raises(CustomException) as caught:
        _run(service, operation)

    assert caught.value.statusCode == 409
    assert project.applied == []
    assert project.writers["summary.parquet"] == "upload"
    assert project.rows[0]["latest_approved_artifact"] is None
    assert "summary" in project.metadata


def test_applyRacingAnotherApplyOfTheSameTransformationLeavesTheOthersTable(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    service = _workspaceService(project)
    _afterFirstCheck(service, lambda: _inAnotherRequest(lambda: _apply(project)))

    with pytest.raises(CustomException) as caught:
        _run(service, "apply")

    assert caught.value.statusCode == 409
    assert project.applied == ["sales"]
    assert project.rows[0]["latest_approved_artifact"] == {
        "mermaid_code": "graph", "message_id": "m1", "table_name": "sales", "table_file": _recordedFile("sales", 1),
    }


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_uploadBetweenTheLastCheckAndAFirstWriteIsNotOverwritten(projectStorage, operation):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    if operation == "apply":
        _approve(project)
    else:
        _approveAndApply(project)
        _deleteTable(project, "sales")
        project.rows[0]["messages"].append({"message_id": "m2", "role": "user", "content": "more"})
    project.duringTableWrite = lambda: _load(project, "sales")

    with pytest.raises(CustomException) as caught:
        _run(_workspaceService(project), operation)

    assert caught.value.statusCode == 409
    assert project.writers["sales.parquet"] == "upload"
    assert project.rows[0].get("latest_approved_artifact") is None


def test_firstWriteRefusedByStorageGivesTheClaimBack(projectStorage):
    # A file appears without any release, as from a worker still running code that does not release names.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)

    def fileArrives():
        project.fileNames.add("sales.parquet")
        project.writers["sales.parquet"] = "upload"

    project.duringTableWrite = fileArrives

    with pytest.raises(CustomException) as caught:
        _apply(project)

    assert caught.value.statusCode == 409
    assert project.writers["sales.parquet"] == "upload"
    assert project.rows[0].get("latest_approved_artifact") is None


def _realExecutor(uploadError=None):
    from nubrix.components.transformationExecutor import TransformationExecutor

    executor = object.__new__(TransformationExecutor)
    bucket = mock.Mock()
    bucket.upload.side_effect = uploadError
    executor.client = mock.Mock()
    executor.client.storage.from_.return_value = bucket
    executor._redis_client = mock.Mock()
    return executor, bucket


def test_firstWriteIsRefusedWhenStorageAlreadyHasTheFile():
    from storage3.exceptions import StorageApiError

    executor, bucket = _realExecutor(StorageApiError("The resource already exists", "Duplicate", "409"))

    with pytest.raises(CustomException) as caught:
        executor.apply(projectId="project_1", parquetBytes=b"parquet", tableName="sales", overwrite=False)

    assert caught.value.statusCode == 409
    assert bucket.upload.call_args.kwargs["file_options"] == {"upsert": "false"}
    executor._redis_client.assert_not_called()


def test_rewriteOfItsOwnTableReplacesTheFile():
    executor, bucket = _realExecutor()

    executor.apply(projectId="project_1", parquetBytes=b"parquet", tableName="sales")

    assert bucket.upload.call_args.kwargs["file_options"] == {"upsert": "true"}


@pytest.mark.parametrize("overwrite", [True, False])
def test_otherStorageFailuresStayServerErrors(overwrite):
    from storage3.exceptions import StorageApiError

    executor, _ = _realExecutor(StorageApiError("Internal Server Error", "InternalError", "500"))

    with pytest.raises(CustomException) as caught:
        executor.apply(projectId="project_1", parquetBytes=b"parquet", tableName="sales", overwrite=overwrite)

    assert caught.value.statusCode == 500


def test_refusalDuringARewriteOfItsOwnTableStaysAServerError():
    # A rewrite asks storage to replace the file, so a refusal there is not an upload taking the name.
    from storage3.exceptions import StorageApiError

    executor, _ = _realExecutor(StorageApiError("The resource already exists", "Duplicate", "409"))

    with pytest.raises(CustomException) as caught:
        executor.apply(projectId="project_1", parquetBytes=b"parquet", tableName="sales", overwrite=True)

    assert caught.value.statusCode == 500


@pytest.mark.parametrize("kind", LOADS)
def test_loadThatFailsBeforeWritingGivesTheNameBack(projectStorage, kind):
    # The transformation's table has no key yet, so the load accepts its name and releases the claim.
    name = LOADS[kind]
    project = projectStorage(_FakeProject(metadata={}, tables=(name,), rows=[_appliedRow(name, recorded=True)]))
    project.writers[f"{name}.parquet"] = "transformation"

    with pytest.raises(CustomException):
        _load(project, name, kind=kind, parquetError=ValueError("cannot convert"))

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": name, "table_file": _recordedFile(name)}
    assert project.writers == {f"{name}.parquet": "transformation"}


def test_failedLoadLeavesANameTheTransformationClaimedSince(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))

    def claimElsewhereThenFail(*_args, **_kwargs):
        project.rows[0]["latest_approved_artifact"] = {"table_name": "renamed"}
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=claimElsewhereThenFail)

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "renamed"}


def test_csvThatCannotBeParsedGivesTheNameBack(projectStorage):
    from api.services.managementService import managementService

    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    loader = object.__new__(DataLoadService)
    loader.client = project
    emptyFile = mock.Mock(filename="sales.csv")
    emptyFile.read = mock.AsyncMock(return_value=b"")

    with mock.patch.object(managementService, "client", project), pytest.raises(CustomException):
        asyncio.run(loader.loadCsvData("project_1", [emptyFile]))

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "sales", "table_file": _recordedFile("sales")}


def test_loadWhoseUploadFailsLeavesTheNameReleased(projectStorage):
    # The upload may have landed, so its file is never handed back to the transformation.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    project.failures["tableWrite"] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException):
        _load(project, "sales")

    assert project.rows[0]["latest_approved_artifact"] is None


def test_pdfLoadRefusedForALaterNameGivesTheEarlierNamesBack(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={"report_table2": {}}, tables=("report_table1", "report_table2"), rows=[_appliedRow("report_table1", recorded=True)],
    ))

    with pytest.raises(CustomException) as caught:
        _load(project, "report_table1", kind="pdf", pdfTables=2)

    assert caught.value.statusCode == 409
    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "report_table1", "table_file": _recordedFile("report_table1")}
    assert project.writers == {}


def test_pdfLoadThatFailsPartWayGivesBackOnlyTheNamesItDidNotWrite(projectStorage):
    project = projectStorage(_FakeProject(
        metadata={}, tables=("report_table1", "report_table2"),
        rows=[_appliedRow("report_table1", recorded=True), _appliedRow("report_table2", transformationId="t2", recorded=True)],
    ))
    project.writers.update({"report_table1.parquet": "transformation", "report_table2.parquet": "transformation"})

    with pytest.raises(CustomException):
        _load(project, "report_table1", kind="pdf", pdfTables=2, parquetError=[None, ValueError("cannot convert")])

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.rows[1]["latest_approved_artifact"] == {"table_name": "report_table2", "table_file": _recordedFile("report_table2")}
    assert project.writers == {"report_table1.parquet": "upload", "report_table2.parquet": "transformation"}


@pytest.mark.parametrize("kind", LOADS)
def test_loadOverATransformationsTableClearsItsCaches(projectStorage, kind):
    name = LOADS[kind]
    project = projectStorage(_FakeProject(metadata={}, tables=(name,), rows=[_appliedRow(name)]))

    _load(project, name, kind=kind)

    assert project.forgotten == [name]


def test_loadOverATableNoTransformationClaimsClearsItsCaches(projectStorage):
    # Another load may have taken the claim first, so this one sees none but still replaces a cached table.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",)))

    _load(project, "sales")

    assert project.forgotten == ["sales"]


def test_loadIsKeptWhenClearingCachesFails(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales")]))
    project.failures["cacheClear"] = ConnectionError("redis unavailable")

    _load(project, "sales")

    assert project.writers["sales.parquet"] == "upload"
    assert "sales" in project.metadata


def _uploadLands(project, name):
    project.fileNames.add(f"{name}.parquet")
    project.writers[f"{name}.parquet"] = "upload"
    project.versions[f"{name}.parquet"] = project.versions.get(f"{name}.parquet", 0) + 1


def test_failedLoadDoesNotHandAnotherLoadsTableToTheTransformation(projectStorage):
    # The transformation's table has no key. Load A takes the claim; load B writes the name; then A fails.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    project.writers["sales.parquet"] = "transformation"

    def loadBThenFail(*_args, **_kwargs):
        _uploadLands(project, "sales")
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=loadBThenFail)

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.writers["sales.parquet"] == "upload"


def test_restoreIsUndoneWhenAnotherLoadWritesStraightAfterIt(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))

    def failAfterArmingTheWrite(*_args, **_kwargs):
        # The next row update is the restore; the other load's file lands right after it.
        project.afterRowUpdate.append(lambda: _uploadLands(project, "sales"))
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=failAfterArmingTheWrite)

    assert project.rows[0]["latest_approved_artifact"] is None


def test_restoreIsUndoneWhenTheFileCannotBeChecked(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))

    def failAfterArmingTheOutage(*_args, **_kwargs):
        project.afterRowUpdate.append(lambda: project.failures.update({"list:sales.parquet": ConnectionError("storage unavailable")}))
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=failAfterArmingTheOutage)

    assert project.rows[0]["latest_approved_artifact"] is None


def test_failedLoadGivesBackAClaimWhoseTableHadNoFile(projectStorage):
    # An apply whose write failed: a claim with no file. Nothing has written the name since.
    project = projectStorage(_FakeProject(metadata={}, rows=[_appliedRow("sales")]))

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=ValueError("cannot convert"))

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "sales"}


def test_failedLoadDoesNotGiveBackANameWhoseFileWasNotRecorded(projectStorage):
    # Applied before files were recorded, or the record failed: the file cannot be told apart from an upload.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales")]))

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=ValueError("cannot convert"))

    assert project.rows[0]["latest_approved_artifact"] is None


def test_failedLoadDoesNotHandOverAnUploadThatLandedBeforeItReleasedTheName(projectStorage):
    # Another load wrote the name but has not released it yet when this load takes the claim and fails.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    _uploadLands(project, "sales")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=ValueError("cannot convert"))

    assert project.rows[0]["latest_approved_artifact"] is None


def test_restoreIsUndoneWhenTheRestoreItselfFails(projectStorage):
    # The write may have been applied before the error came back.
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    restoreErrors = []

    def failTheRestore(*_args, **_kwargs):
        def restoreWithError():
            restoreErrors.append(True)
            raise ConnectionError("response lost")
        project.afterRowUpdate.append(restoreWithError)
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=failTheRestore)

    assert restoreErrors == [True]
    assert project.rows[0]["latest_approved_artifact"] is None


def test_applyRecordsTheFileItWrote(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)

    _apply(project)

    assert project.rows[0]["latest_approved_artifact"]["table_file"] == _recordedFile("sales", project.versions["sales.parquet"])


def test_applyStillSucceedsWhenItsFileCannotBeRecorded(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    project.duringTableWrite = lambda: project.failures.update({"list:sales.parquet": ConnectionError("storage unavailable")})

    _apply(project)

    assert project.rows[0]["latest_approved_artifact"]["table_name"] == "sales"
    assert "table_file" not in project.rows[0]["latest_approved_artifact"]


def test_claimOnANewNameDropsTheOldFileRecord(projectStorage):
    previous = {"table_name": "before", "table_file": _recordedFile("before")}
    project = projectStorage(_FakeProject(metadata={}, tables=("before",), rows=[_withMessages({**_draftRow(), "latest_approved_artifact": previous})]))
    _approve(project)
    snapshots = []
    project.afterRowUpdate.extend([lambda: snapshots.append(dict(project.rows[0]["latest_approved_artifact"] or {}))] * 5)

    _apply(project)

    claim = next(snapshot for snapshot in snapshots if snapshot.get("table_name") == "sales")
    assert "table_file" not in claim


def test_nameIsNotGivenBackEvenBrieflyOntoAChangedFile(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    _uploadLands(project, "sales")
    seen = []
    project.afterRowUpdate.extend([lambda: seen.append(project.rows[0]["latest_approved_artifact"])] * 5)

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=ValueError("cannot convert"))

    assert seen == [None]


def test_releasingAgainLeavesANameTheTransformationClaimedMeanwhile(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))

    def failAfterArmingAClaimAndAWrite(*_args, **_kwargs):
        def claimElsewhereAndWrite():
            project.rows[0]["latest_approved_artifact"] = {"table_name": "renamed"}
            _uploadLands(project, "sales")
        project.afterRowUpdate.append(claimElsewhereAndWrite)
        raise ValueError("cannot convert")

    with pytest.raises(CustomException):
        _load(project, "sales", parquetError=failAfterArmingAClaimAndAWrite)

    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "renamed"}


def _cleanUp(project, operation, afterRowRead=None):
    """Delete the transformation, or roll it back to the start. afterRowRead runs once its row has been read."""
    service = _workspaceService(project)
    if afterRowRead:
        realRead = service._ensure_transformation

        def readThenRun(*args, **kwargs):
            # A copy, as a real read returns: later changes to the row do not reach it.
            row = copy.deepcopy(realRead(*args, **kwargs))
            hook = afterRowRead
            service._ensure_transformation = realRead
            hook()
            return row

        service._ensure_transformation = readThenRun
    service.getMessages = mock.AsyncMock(return_value=[])
    with mock.patch("api.services.managementService.managementService.generateMetadata"):
        if operation == "delete":
            return asyncio.run(service.deleteTransformation("project_1", "t1"))
        return asyncio.run(service.rollbackTransformation("project_1", "t1", "m0"))


def _cleanedUpRow(project, operation):
    if operation == "delete":
        assert project.rows == []
    else:
        assert project.rows[0]["latest_approved_artifact"] is None
        assert project.rows[0]["messages"] == []


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "notRecorded"])
@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_uploadBetweenTheRowReadAndTheRemoveSurvives(projectStorage, operation, recorded):
    # The transformation's table has no key yet, so a load may take its name.
    project = projectStorage(_FakeProject(metadata={}, tables=("summary",), rows=[_withMessages(_appliedRow(recorded=recorded))]))

    _cleanUp(project, operation, afterRowRead=lambda: _load(project, "summary"))

    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata
    assert project.removed == []
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_uploadAfterTheClaimIsReleasedSurvivesWhenTheFileWasRecorded(projectStorage, operation):
    # The upload lands after the cleanup takes the name back, but before it looks at the file.
    project = projectStorage(_FakeProject(metadata={"summary": {}}, tables=("summary",), rows=[_withMessages(_appliedRow(recorded=True))]))
    project.afterRowUpdate.append(lambda: _uploadLands(project, "summary"))

    _cleanUp(project, operation)

    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata
    assert project.removed == []
    assert project.forgotten == ["summary"]
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_fileChangedSinceItWasRecordedIsKeptWithItsKey(projectStorage, operation):
    # Written without the name being released, as by a worker still running code that does not release names.
    project = projectStorage(_FakeProject(metadata={"summary": {}}, tables=("summary",), rows=[_withMessages(_appliedRow(recorded=True))]))
    _uploadLands(project, "summary")

    _cleanUp(project, operation)

    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata
    assert project.removed == []
    assert project.forgotten == ["summary"]
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_unchangedRecordedFileIsRemovedWithItsKeyAndCaches(projectStorage, operation):
    project = projectStorage(_FakeProject(metadata={"sales": {}, "summary": {}}, tables=("sales", "summary"), rows=[_withMessages(_appliedRow(recorded=True))]))

    _cleanUp(project, operation)

    assert "summary.parquet" not in project.fileNames
    assert list(project.metadata) == ["sales"]
    assert project.forgotten == ["summary"]
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_claimWhoseFileIsGoneStillClearsItsKeyAndCaches(projectStorage, operation):
    project = projectStorage(_FakeProject(metadata={"sales": {}, "summary": {}}, tables=("sales",), rows=[_withMessages(_appliedRow(recorded=True))]))

    _cleanUp(project, operation)

    assert project.removed == []
    assert list(project.metadata) == ["sales"]
    assert project.forgotten == ["summary"]
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
@pytest.mark.parametrize("failure", ["tableRemove", "metadataWrite", "cacheClear"])
def test_failedCleanupOfARecordedFileKeepsTheClaimAndCanBeRetried(projectStorage, operation, failure):
    project = projectStorage(_FakeProject(metadata={"sales": {}, "summary": {}}, tables=("sales", "summary"), rows=[_withMessages(_appliedRow(recorded=True))]))
    project.failures[failure] = ConnectionError("storage unavailable")

    with pytest.raises(CustomException) as caught:
        _cleanUp(project, operation)

    assert caught.value.statusCode == 503
    assert project.rows[0]["latest_approved_artifact"] == {"table_name": "summary", "table_file": _recordedFile("summary")}
    project.failures.clear()
    _cleanUp(project, operation)
    assert "summary.parquet" not in project.fileNames
    assert list(project.metadata) == ["sales"]
    _cleanedUpRow(project, operation)


@pytest.mark.parametrize("operation", ["delete", "rollback"])
def test_failedCleanupDoesNotGiveTheNameBackOverAnUploadThatLandedMeanwhile(projectStorage, operation):
    project = projectStorage(_FakeProject(metadata={"summary": {}}, tables=("summary",), rows=[_withMessages(_appliedRow(recorded=True))]))
    project.forgotten = []

    def uploadLandsThenRemoveFails(paths):
        _uploadLands(project, "summary")
        raise ConnectionError("storage unavailable")

    project.remove = uploadLandsThenRemoveFails

    with pytest.raises(CustomException):
        _cleanUp(project, operation)

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.writers["summary.parquet"] == "upload"
    assert "summary" in project.metadata


def test_reapplyKeepsItsFileRecordWhileItClaimsTheSameName(projectStorage):
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow())]))
    _approve(project)
    _apply(project)
    recorded = project.rows[0]["latest_approved_artifact"]["table_file"]
    snapshots = []
    project.afterRowUpdate.extend([lambda: snapshots.append(dict(project.rows[0]["latest_approved_artifact"] or {}))] * 5)

    _apply(project)

    assert snapshots[0]["table_file"] == recorded


def test_failedReapplyThenAFailedLoadGivesTheNameBack(projectStorage):
    # The re-apply's write fails without landing, so the file is still the one the first apply recorded.
    project = projectStorage(_FakeProject(metadata={}, rows=[_withMessages(_draftRow("summary"))]))
    _approve(project)
    _apply(project)
    applied = project.rows[0]["latest_approved_artifact"]
    project.duringTableWrite = _writeFails
    with pytest.raises(CustomException):
        _apply(project)

    with pytest.raises(CustomException):
        _load(project, "summary", parquetError=ValueError("cannot convert"))

    assert project.rows[0]["latest_approved_artifact"] == applied
    assert project.writers["summary.parquet"] == "transformation"


@pytest.fixture
def noRetryPause():
    with mock.patch("api.services.dataLoadService.RELEASE_RETRY_PAUSES", (0, 0)):
        yield


def test_loadRetriesTheReleaseAfterItsWrite(projectStorage, noRetryPause):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    # The release before the write works; the first one after it fails.
    project.failures["rowUpdate"] = [None, ConnectionError("database unavailable")]

    _load(project, "sales")

    assert project.rows[0]["latest_approved_artifact"] is None
    assert project.forgotten == ["sales"]
    assert project.writers["sales.parquet"] == "upload"


def test_loadWhoseReleaseAfterItsWriteKeepsFailingStillClearsCachesAndIsRefused(projectStorage, noRetryPause):
    project = projectStorage(_FakeProject(metadata={}, tables=("sales",), rows=[_appliedRow("sales", recorded=True)]))
    project.failures["rowUpdate"] = [None] + [ConnectionError("database unavailable")] * 10

    with mock.patch("api.services.dataLoadService.logger") as log, pytest.raises(CustomException) as caught:
        _load(project, "sales")

    assert caught.value.statusCode == 503
    assert project.forgotten == ["sales"]
    assert any("project_1" in str(call) and "sales" in str(call) for call in log.error.call_args_list)
