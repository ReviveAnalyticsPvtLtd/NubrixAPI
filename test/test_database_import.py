import dataclasses
import json
import types
from unittest.mock import Mock, patch

import pytest
import sqlalchemy
from celery.exceptions import Retry

from api import commons
from api.services import databaseImportService as service
from api.services import databaseSources as ds
from api.services import dataLoadService as dataLoadModule
from api.services import managementService as managementModule
from api.services.databaseSources import Source, listTables, storedName
from api.services.importJobs import ImportJobs
from api.services.subscriptions.entitlementService import EntitlementUnavailableError
from nubrix.triggers.tasks import databaseImportTask as tasks
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from test.test_import_jobs import FakeRedis
from utils.logger import logger

PROJECT = "11111111-2222-4333-8444-555555555555"
USER = "user-1"
MYSQL = Source(kind="mysql", host="db.test", address="192.0.2.1", port=3306, user="u", password="p", db="d")
DESCRIPTION_MISSING = "Description could not be generated"


class _Query:
    """A Supabase table query: no transformation claims any name."""

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: self

    def execute(self):
        return types.SimpleNamespace(data=[])


class _Client:
    """Lists and uploads against the fake bucket, so stored files can be read back."""

    def __init__(self, storage: FakePrivateStorage):
        self._storage = storage
        self.storage = types.SimpleNamespace(from_=lambda _bucket: types.SimpleNamespace(list=self._list, upload=self._upload))

    def table(self, _name):
        return _Query()

    def _list(self, path=None, options=None):
        prefix = f"AnalyticsHub/{path}/"
        return [{"name": key[len(prefix):], "metadata": {}} for key in self._storage.objects if key.startswith(prefix)]

    def _upload(self, path, file, file_options=None):
        if isinstance(file, str):
            with open(file, "rb") as stored:
                file = stored.read()
        self._storage.put(*path.split("/", 1), file)


@pytest.fixture
def job(monkeypatch, tmp_path):
    storage = FakePrivateStorage()
    monkeypatch.setenv("SUPABASE_URL", storage.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.delenv("STORED_TABLE_MAX_DECODED_BYTES", raising=False)
    for name in ("IMPORT_BATCH_TABLES", "IMPORT_BATCH_COLUMNS", "IMPORT_CHUNK_ROWS"):
        monkeypatch.delenv(name, raising=False)
    client = _Client(storage)
    monkeypatch.setattr(dataLoadModule.dataLoadService, "client", client)
    monkeypatch.setattr(managementModule.managementService, "client", client)
    monkeypatch.setattr(dataLoadModule, "forget_table", lambda *_args: None)
    monkeypatch.setattr(service, "updateProjectModifiedAt", lambda *_args: None)
    redis = FakeRedis()
    jobs = ImportJobs(redis)
    monkeypatch.setattr(service, "importJobs", jobs)
    metadata = Mock(side_effect=lambda projectId, tableNames, userId=None: {name: {} for name in tableNames})
    monkeypatch.setattr(managementModule.managementService, "generateMetadataForTables", metadata)
    insights = Mock(return_value={})
    monkeypatch.setattr(managementModule.managementService, "generateInsightsForProject", insights)
    credits = Mock(return_value=True)
    monkeypatch.setattr(service, "creditsAllow", credits)
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    with patch.object(ds, "_engine", return_value=engine):
        yield types.SimpleNamespace(storage=storage, jobs=jobs, redis=redis, metadata=metadata, insights=insights, credits=credits, engine=engine)
    storage.close()


def makeTables(engine, spec: dict[str, tuple[int, int]]) -> None:
    """spec: table name -> (columns, rows)."""
    with engine.begin() as conn:
        for name, (columns, rows) in spec.items():
            names = [f"c{i}" for i in range(columns)]
            conn.execute(sqlalchemy.text(f'CREATE TABLE "{name}" ({", ".join(f"{c} INTEGER" for c in names)})'))
            for row in range(rows):
                conn.execute(sqlalchemy.text(f'INSERT INTO "{name}" VALUES ({", ".join(str(row) for _ in names)})'))


def startJob(job):
    tables = listTables(MYSQL)
    jobId = job.jobs.start(PROJECT, [(t[0], storedName(t)) for t in tables])
    return tables, jobId


def statuses(job) -> dict:
    return {t["storedAs"]: t for t in job.jobs.get(PROJECT)["tables"]}


def describedNames(job) -> list[list[str]]:
    return [call.kwargs["tableNames"] for call in job.metadata.call_args_list]


def test_batchClosesAtEightTables(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(10)})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 8

    assert describedNames(job) == [[f"t{i:02}" for i in range(8)]]
    found = statuses(job)
    assert [found[f"t{i:02}"]["status"] for i in range(10)] == ["loaded"] * 8 + ["pending"] * 2
    assert found["t00"]["rows"] == 1 and found["t00"]["reason"] is None
    assert f"AnalyticsHub/{PROJECT}/t07.parquet" in job.storage.objects
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 8) is None


def test_batchClosesAt400Columns(job):
    makeTables(job.engine, {name: (150, 1) for name in "abcd"})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 3
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 3) is None

    assert describedNames(job) == [["a", "b", "c"], ["d"]]


def test_wideTableIsItsOwnBatch(job):
    makeTables(job.engine, {"a": (500, 1), "b": (1, 1)})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 1
    assert describedNames(job) == [["a"]]


def test_missingDescriptionsAreRetriedOneByOne(job):
    makeTables(job.engine, {name: (1, 1) for name in "abc"})
    tables, jobId = startJob(job)
    job.metadata.side_effect = lambda projectId, tableNames, userId=None: {n: {} for n in tableNames if n != "c"}

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    assert describedNames(job) == [["a", "b", "c"], ["c"]]
    found = statuses(job)
    assert found["c"]["status"] == "loaded" and found["c"]["reason"] == DESCRIPTION_MISSING
    assert found["a"]["reason"] is None


def test_failedDescribeCallRetriesEveryTable(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    job.metadata.side_effect = lambda projectId, tableNames, userId=None: (
        (_ for _ in ()).throw(RuntimeError("model answer cut off")) if len(tableNames) > 1 else {n: {} for n in tableNames}
    )

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    assert describedNames(job) == [["a", "b"], ["a"], ["b"]]
    assert all(t["reason"] is None and t["status"] == "loaded" for t in statuses(job).values())


def test_outOfCreditsSkipsTheRest(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(10)})
    tables, jobId = startJob(job)
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 8
    job.credits.return_value = False

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 8) is None

    job.credits.assert_called_with(USER, "metadata_generation")
    found = statuses(job)
    assert [(found[n]["status"], found[n]["reason"]) for n in ("t08", "t09")] == [("skipped", "Out of credits")] * 2
    assert found["t07"]["status"] == "loaded"
    assert len(job.metadata.call_args_list) == 1


def test_takenNameIsSkipped(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    job.storage.putJson(PROJECT, "metadata.json", {"a": {"isActive": True}})
    tables, jobId = startJob(job)

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert (found["a"]["status"], found["a"]["reason"]) == ("skipped", "A table with this name already exists")
    assert found["b"]["status"] == "loaded"
    assert describedNames(job) == [["b"]]


def test_tooLargeTableFailsAndOthersLoad(job, monkeypatch):
    makeTables(job.engine, {"big": (5, 2000), "small": (1, 1), "tiny": (1, 1)})
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "20000")
    tables, jobId = startJob(job)

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert (found["big"]["status"], found["big"]["reason"]) == ("failed", "Too large to import")
    assert found["small"]["status"] == found["tiny"]["status"] == "loaded"
    assert f"AnalyticsHub/{PROJECT}/big.parquet" not in job.storage.objects
    assert describedNames(job) == [["small", "tiny"]]


def test_driverErrorReasonIsGeneric(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    realRead = service.readChunks

    def readChunks(source, table, chunkRows):
        if table[0] == "a":
            raise Exception("user=alice host=db.example")
        return realRead(source, table, chunkRows)

    monkeypatch.setattr(service, "readChunks", readChunks)
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)
    finally:
        logger.remove(sink)

    found = statuses(job)
    assert (found["a"]["status"], found["a"]["reason"]) == ("failed", "Could not read this table")
    assert found["b"]["status"] == "loaded"
    assert "alice" not in json.dumps(job.jobs.get(PROJECT))
    assert not any("alice" in str(m) or "db.example" in str(m) for m in messages)


def test_emptyTablesLoadWithNoRows(job, monkeypatch):
    makeTables(job.engine, {"sql": (2, 0), "mongo": (1, 1)})
    tables, jobId = startJob(job)
    realRead = service.readChunks
    monkeypatch.setattr(service, "readChunks", lambda source, table, chunkRows: iter(()) if table[0] == "mongo" else realRead(source, table, chunkRows))

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert [(found[n]["status"], found[n]["rows"]) for n in ("mongo", "sql")] == [("loaded", 0)] * 2


def test_redeliveredBatchResumes(job):
    makeTables(job.engine, {name: (1, 1) for name in "abcde"})
    tables, jobId = startJob(job)
    for name in "abc":
        job.jobs.setTable(PROJECT, jobId, name, status="loaded", rows=99)
    job.jobs.setTable(PROJECT, jobId, "d", status="loading")
    job.storage.put(PROJECT, "d.parquet", b"written before the crash")

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    found = statuses(job)
    assert all((found[n]["status"], found[n]["rows"], found[n]["reason"]) == ("loaded", 99, None) for n in "abc")
    assert (found["d"]["status"], found["d"]["reason"]) == ("loaded", DESCRIPTION_MISSING)
    assert found["e"]["status"] == "loaded"
    assert describedNames(job) == [["e"]]


def test_secondDeliveryOfARunningBatchExitsQuietly(job):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    job.redis.set(f"import:{PROJECT}:batch", f"{jobId}:0", nx=True, ex=900)

    with pytest.raises(service.JobStopped):
        service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    assert statuses(job)["a"]["status"] == "pending"
    assert job.redis.get(f"import:{PROJECT}:batch") == f"{jobId}:0".encode()


def test_batchMarkerIsReleasedAndRefreshedWhileWriting(job, monkeypatch):
    makeTables(job.engine, {"a": (1, 5)})
    monkeypatch.setenv("IMPORT_CHUNK_ROWS", "2")
    tables, jobId = startJob(job)
    expired = []
    expire = job.redis.expire
    monkeypatch.setattr(job.redis, "expire", lambda key, seconds: expired.append((key, seconds)) or expire(key, seconds))

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    assert expired.count((f"import:{PROJECT}:batch", 900)) == 3
    assert expired.count((f"import:{PROJECT}:running", 21600)) >= 3
    assert job.redis.get(f"import:{PROJECT}:batch") is None


def test_supersededJobStops(job):
    makeTables(job.engine, {"a": (1, 1)})
    tables, oldJob = startJob(job)
    job.jobs.finish(PROJECT, oldJob, "failed")
    startJob(job)

    with pytest.raises(service.JobStopped):
        service.runBatch(PROJECT, USER, oldJob, MYSQL, tables, 0)
    assert statuses(job)["a"]["status"] == "pending"


@pytest.mark.parametrize("insights, kpis", [("present", "skipped"), ("missing", "done"), ("raises", "failed")])
def test_kpisOnlyWhenProjectHasNone(job, insights, kpis):
    _, jobId = startJob(job)
    if insights == "present":
        job.storage.putJson(PROJECT, "insights.json", {"insights": []})
    if insights == "raises":
        job.insights.side_effect = RuntimeError("prompt too large")

    service.runKpis(PROJECT, USER, jobId)

    record = job.jobs.get(PROJECT)
    assert (record["kpis"], record["state"]) == (kpis, "done")
    assert job.insights.called == (insights != "present")
    if insights != "present":
        job.credits.assert_called_with(USER, "insight_generation")


def test_kpisSkippedWithoutCredits(job):
    _, jobId = startJob(job)
    job.credits.return_value = False

    service.runKpis(PROJECT, USER, jobId)

    assert not job.insights.called
    assert (job.jobs.get(PROJECT)["kpis"], job.jobs.get(PROJECT)["state"]) == ("skipped", "done")


def test_tasksRouteToImportsQueue():
    from nubrix.triggers.celery import celeryApp

    for name in ("NubrixAI.importBatch", "NubrixAI.importKpis"):
        assert celeryApp.conf.task_routes[name] == {"queue": "imports"}
        task = celeryApp.tasks[name]
        assert (task.acks_late, task.reject_on_worker_lost, task.ignore_result) == (True, True, True)


class _FakeTask:
    def __init__(self, retries=0):
        self.request = types.SimpleNamespace(retries=retries)
        self.sent = []
        self.retried = None
        self.app = types.SimpleNamespace(send_task=lambda name, args: self.sent.append((name, args)))

    def retry(self, countdown, max_retries):
        self.retried = (countdown, max_retries)
        return Retry()


def test_batchTaskEnqueuesNextBatchThenKpis(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(9)})
    tables, jobId = startJob(job)
    fields = dataclasses.asdict(MYSQL)
    listed = [list(t) for t in tables]
    task = _FakeTask()

    tasks.importBatch(task, PROJECT, USER, jobId, fields, listed, 0)
    tasks.importBatch(task, PROJECT, USER, jobId, fields, listed, 8)
    tasks.importKpis(task, PROJECT, USER, jobId)

    assert task.sent == [
        ("NubrixAI.importBatch", [PROJECT, USER, jobId, fields, listed, 8]),
        ("NubrixAI.importKpis", [PROJECT, USER, jobId]),
    ]
    assert job.jobs.get(PROJECT)["state"] == "done"


def test_creditsUnavailableRetriesThenSkips(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    job.credits.side_effect = commons.CreditsUnavailable()
    listed = [list(t) for t in tables]

    task = _FakeTask(retries=2)
    with pytest.raises(Retry):
        tasks.importBatch(task, PROJECT, USER, jobId, dataclasses.asdict(MYSQL), listed, 0)
    assert task.retried == (60, 3) and task.sent == []

    task = _FakeTask(retries=3)
    tasks.importBatch(task, PROJECT, USER, jobId, dataclasses.asdict(MYSQL), listed, 0)
    assert all((t["status"], t["reason"]) == ("skipped", "Could not check credits") for t in statuses(job).values())
    assert task.sent == [("NubrixAI.importKpis", [PROJECT, USER, jobId])]

    tasks.importKpis(task, PROJECT, USER, jobId)
    assert (job.jobs.get(PROJECT)["kpis"], job.jobs.get(PROJECT)["state"]) == ("skipped", "done")


def test_unexpectedErrorFailsJobWithoutLeakingIt(job, monkeypatch):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    monkeypatch.setattr(tasks, "runBatch", Mock(side_effect=RuntimeError("password=hunter2")))
    messages = []
    sink = logger.add(messages.append, format="{message}")
    task = _FakeTask()
    try:
        tasks.importBatch(task, PROJECT, USER, jobId, dataclasses.asdict(MYSQL), [list(t) for t in tables], 0)
    finally:
        logger.remove(sink)

    record = job.jobs.get(PROJECT)
    assert (record["state"], record["kpis"]) == ("failed", "failed")
    assert task.sent == []
    assert not any("hunter2" in str(m) for m in messages)


def test_creditsAllowMatchesTheDependency(monkeypatch):
    from api.services.credits import creditService as creditModule

    plan = types.SimpleNamespace(trialOrAbove=True)
    monkeypatch.setattr(commons.subscriptionEntitlementService, "get", lambda userId: plan)
    balance = {"value": 5000}
    monkeypatch.setattr(creditModule.creditService, "getRemainingTokens", lambda userId: balance["value"])

    assert commons.creditsAllow(USER, "metadata_generation") is True
    balance["value"] = 2999
    assert commons.creditsAllow(USER, "metadata_generation") is False
    balance["value"] = -1
    with pytest.raises(commons.CreditsUnavailable):
        commons.creditsAllow(USER, "metadata_generation")
    plan.trialOrAbove = False
    assert commons.creditsAllow(USER, "metadata_generation") is False

    def unavailable(userId):
        raise EntitlementUnavailableError("down")

    monkeypatch.setattr(commons.subscriptionEntitlementService, "get", unavailable)
    with pytest.raises(commons.CreditsUnavailable):
        commons.creditsAllow(USER, "metadata_generation")
