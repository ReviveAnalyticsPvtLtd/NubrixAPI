import contextlib
import dataclasses
import json
import types
from unittest.mock import ANY, MagicMock, Mock, patch

import pytest
import sqlalchemy
from celery.exceptions import Retry

from api.services import databaseImportService as service
from api.services import databaseSources as ds
from api.services import dataLoadService as dataLoadModule
from api.services import managementService as managementModule
from api.services.databaseSources import Source, listTables, storedName
from api.services.importJobs import ImportJobs
from nubrix.triggers.tasks import databaseImportTask as tasks
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from test.test_import_jobs import FakeRedis
from loguru import logger

PROJECT = "11111111-2222-4333-8444-555555555555"
USER = "user-1"
MYSQL = Source(kind="mysql", host="db.test", address="192.0.2.1", port=3306, user="u", password="p", db="d")


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
        self.storage = types.SimpleNamespace(from_=lambda _bucket: types.SimpleNamespace(list=self._list, upload=self._upload, remove=self._remove))

    def table(self, _name):
        return _Query()

    def _list(self, path=None, options=None):
        prefix = f"AnalyticsHub/{path}/"
        return [{"name": key[len(prefix):], "metadata": {}} for key in self._storage.objects if key.startswith(prefix)]

    def _remove(self, paths):
        for path in paths:
            self._storage.objects.pop(f"AnalyticsHub/{path}", None)

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
    for name in ("IMPORT_BATCH_TABLES", "IMPORT_BATCH_COLUMNS", "IMPORT_CHUNK_ROWS", "IMPORT_CHUNK_CELLS", "IMPORT_MONGO_CHUNK_ROWS"):
        monkeypatch.delenv(name, raising=False)
    client = _Client(storage)
    monkeypatch.setattr(dataLoadModule.dataLoadService, "client", client)
    monkeypatch.setattr(managementModule.managementService, "client", client)
    monkeypatch.setattr(dataLoadModule, "forget_table", lambda *_args: None)
    monkeypatch.setattr(service, "updateProjectModifiedAt", lambda *_args: None)
    redis = FakeRedis()
    jobs = ImportJobs(redis)
    monkeypatch.setattr(service, "importJobs", jobs)
    metadata = Mock(return_value={})  # the job must never call the model: step 3 runs from the browser
    monkeypatch.setattr(managementModule.managementService, "generateMetadataForTables", metadata)
    insights = Mock(return_value={})
    monkeypatch.setattr(managementModule.managementService, "generateInsightsForProject", insights)
    project = types.SimpleNamespace(exists=True)  # whether the Projects row is there
    monkeypatch.setattr(service, "_projectExists", lambda projectId: project.exists)
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'source.db'}")
    with patch.object(ds, "_engine", return_value=engine):
        yield types.SimpleNamespace(storage=storage, jobs=jobs, redis=redis, metadata=metadata, insights=insights,
                                    engine=engine, project=project)
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


def test_tableCarriesItsOwnStoredName(job):
    makeTables(job.engine, {"a b": (1, 1), "a_b": (1, 2)})
    jobId = job.jobs.start(PROJECT, [("a b", "a_b"), ("a_b", "a_b_2")])

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, [("a b", None, "a_b"), ("a_b", None, "a_b_2")], 0) is None

    found = statuses(job)
    assert (found["a_b"]["status"], found["a_b"]["rows"]) == ("loaded", 1)
    assert (found["a_b_2"]["status"], found["a_b_2"]["rows"]) == ("loaded", 2)


def statuses(job) -> dict:
    return {t["storedAs"]: t for t in job.jobs.get(PROJECT)["tables"]}


def loadedNames(job) -> list[str]:
    return [name for name, table in statuses(job).items() if table["status"] == "loaded"]


def test_batchClosesAtEightTables(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(10)})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 8

    found = statuses(job)
    assert [found[f"t{i:02}"]["status"] for i in range(10)] == ["loaded"] * 8 + ["pending"] * 2
    assert found["t00"]["rows"] == 1 and found["t00"]["reason"] is None
    assert f"AnalyticsHub/{PROJECT}/t07.parquet" in job.storage.objects
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 8) is None


def test_batchClosesAt400Columns(job):
    makeTables(job.engine, {name: (150, 1) for name in "abcd"})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 3
    assert loadedNames(job) == ["a", "b", "c"]
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 3) is None
    assert loadedNames(job) == ["a", "b", "c", "d"]


def test_wideTableIsItsOwnBatch(job):
    makeTables(job.engine, {"a": (500, 1), "b": (1, 1)})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 1
    assert loadedNames(job) == ["a"]


def test_takenNameIsSkipped(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    job.storage.putJson(PROJECT, "metadata.json", {"a": {"isActive": True}})
    tables, jobId = startJob(job)

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert (found["a"]["status"], found["a"]["reason"]) == ("skipped", "A table with this name already exists")
    assert found["b"]["status"] == "loaded"


def test_tooLargeTableFailsAndOthersLoad(job, monkeypatch):
    makeTables(job.engine, {"big": (5, 2000), "small": (1, 1), "tiny": (1, 1)})
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "20000")
    tables, jobId = startJob(job)

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert (found["big"]["status"], found["big"]["reason"]) == ("failed", "Too large to import")
    assert found["small"]["status"] == found["tiny"]["status"] == "loaded"
    assert f"AnalyticsHub/{PROJECT}/big.parquet" not in job.storage.objects


@contextlib.contextmanager
def capturedLogs(monkeypatch, module):
    # Other test files swap utils.logger for a stub at collection, so point the module at loguru itself.
    monkeypatch.setattr(module, "logger", logger)
    messages = []
    sink = logger.add(messages.append, format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink)
    assert messages, "nothing was logged, so the leak check saw nothing"


def test_driverErrorReasonIsGeneric(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    realRead = service.readChunks

    def readChunks(source, table, *sizes):
        if table[0] == "a":
            raise Exception("user=alice host=db.example")
        return realRead(source, table, *sizes)

    monkeypatch.setattr(service, "readChunks", readChunks)
    with capturedLogs(monkeypatch, service) as messages:
        service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert (found["a"]["status"], found["a"]["reason"]) == ("failed", "Could not read this table")
    assert found["b"]["status"] == "loaded"
    assert "alice" not in json.dumps(job.jobs.get(PROJECT))
    assert not any("alice" in str(m) or "db.example" in str(m) for m in messages)


def test_emptyTablesLoadWithNoRows(job, monkeypatch):
    makeTables(job.engine, {"sql": (2, 0), "mongo": (1, 1)})
    tables, jobId = startJob(job)
    realRead = service.readChunks
    monkeypatch.setattr(service, "readChunks", lambda source, table, *sizes: iter(()) if table[0] == "mongo" else realRead(source, table, *sizes))

    service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0)

    found = statuses(job)
    assert [(found[n]["status"], found[n]["rows"]) for n in ("mongo", "sql")] == [("loaded", 0)] * 2


def crashMidBatch(job, jobId):
    """What a delivery that died mid-batch leaves: a, b, c stored and marked, d stored but not marked."""
    for name in "abc":
        job.jobs.setTable(PROJECT, jobId, name, status="loaded", rows=99)
    job.jobs.setTable(PROJECT, jobId, "d", status="loading")
    job.storage.put(PROJECT, "d.parquet", b"written before the crash")


def test_redeliveredBatchResumes(job):
    makeTables(job.engine, {name: (1, 1) for name in "abcde"})
    tables, jobId = startJob(job)
    crashMidBatch(job, jobId)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    found = statuses(job)
    assert all((found[n]["status"], found[n]["rows"], found[n]["reason"]) == ("loaded", 99, None) for n in "abc")
    assert (found["d"]["status"], found["d"]["reason"]) == ("loaded", None)
    assert found["e"]["status"] == "loaded"
    assert job.storage.objects[f"AnalyticsHub/{PROJECT}/d.parquet"] == b"written before the crash"


def test_resumedLastTableStillMarksTheProjectModified(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    # the dead delivery stored b but died before marking it, and before touching the project
    job.jobs.setTable(PROJECT, jobId, "a", status="loaded", rows=1)
    job.jobs.setTable(PROJECT, jobId, "b", status="loading")
    job.storage.put(PROJECT, "b.parquet", b"written before the crash")
    touched = []
    monkeypatch.setattr(service, "updateProjectModifiedAt", touched.append)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    assert touched == [PROJECT]


def test_redeliveryAfterEveryTableWasMarkedStillMarksTheProjectModified(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    # the dead delivery marked both tables, then died before touching the project
    for name in "ab":
        job.jobs.setTable(PROJECT, jobId, name, status="loaded", rows=1)
    touched = []
    monkeypatch.setattr(service, "updateProjectModifiedAt", touched.append)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    assert touched == [PROJECT]


def test_resumedTablesDoNotCountTowardTheBatch(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "abcdefg"})
    monkeypatch.setenv("IMPORT_BATCH_TABLES", "2")
    tables, jobId = startJob(job)
    crashMidBatch(job, jobId)

    # a to d were stored by the dead delivery and cost nothing now, so the batch closes after e and f
    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) == 6
    assert statuses(job)["g"]["status"] == "pending"


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


def test_tasksRouteToImportsQueue():
    from nubrix.triggers.celery import celeryApp

    assert celeryApp.conf.task_routes == {"NubrixAI.importBatch": {"queue": "imports"}}
    task = celeryApp.tasks["NubrixAI.importBatch"]
    assert (task.acks_late, task.reject_on_worker_lost, task.ignore_result) == (True, True, True)
    assert "NubrixAI.importKpis" not in celeryApp.tasks


def test_celeryAppLoadsNoImportService():
    """The default worker and beat import the app too; the service would load the AI stack into them."""
    import subprocess
    import sys
    from pathlib import Path

    probe = (
        "import json, sys; from nubrix.triggers.celery import celeryApp; "
        "print(json.dumps([[m for m in ('api.services.managementService', 'api.services.databaseImportService') if m in sys.modules], "
        "celeryApp.conf.task_routes]))"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=Path(__file__).parents[1], timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    loaded, routes = json.loads(done.stdout.strip().splitlines()[-1])
    assert loaded == []
    assert routes == {"NubrixAI.importBatch": {"queue": "imports"}}


REDACTED = {"argsrepr": "(redacted)", "kwargsrepr": "{}"}


class _FakeTask:
    def __init__(self, retries=0):
        self.request = types.SimpleNamespace(retries=retries)
        self.sent = []
        self.options = []
        self.retried = None

        def send_task(name, args, **options):
            self.sent.append((name, args))
            self.options.append(options)

        self.app = types.SimpleNamespace(send_task=send_task)

    def retry(self, countdown, max_retries, **options):
        self.retried = (countdown, max_retries, options)
        return Retry()


def sealed(jobId, source=MYSQL, projectId=PROJECT, tables=(), start=0, userId=USER):
    """The sealed argument as sendBatch queues it."""
    task = _FakeTask()
    tasks.sendBatch(task.app, projectId, userId, jobId, source, list(tables), start)
    return task.sent[0][1][3]


def batch(task, jobId, tables, start=0, source=MYSQL):
    """Deliver a batch message as sendBatch queues it."""
    tasks.importBatch(task, PROJECT, USER, jobId, sealed(jobId, source, tables=tables, start=start), tables, start)


def test_batchTaskEnqueuesNextBatchThenEndsTheJob(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(9)})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    task = _FakeTask()

    batch(task, jobId, listed, 0)
    assert job.redis.get(f"import:{PROJECT}:next") == f"{jobId}:8".encode()
    batch(task, jobId, listed, 8)
    assert job.redis.get(f"import:{PROJECT}:next") == f"{jobId}:done".encode()

    assert task.sent == [("NubrixAI.importBatch", [PROJECT, USER, jobId, ANY, listed, 8])]
    assert all(options == {"ignore_result": True, **REDACTED} for options in task.options)
    record = job.jobs.get(PROJECT)
    assert record["state"] == "done" and "kpis" not in record
    assert all(t["status"] == "loaded" for t in record["tables"])


def test_lateDuplicateOfTheLastBatchAfterDoneExitsQuietly(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    batch(_FakeTask(), jobId, listed, 0)
    before = job.jobs.get(PROJECT)
    assert before["state"] == "done"

    late = _FakeTask()
    batch(late, jobId, listed, 0)

    assert late.sent == [] and late.retried is None
    assert job.jobs.get(PROJECT) == before


def test_deliveryAfterADeathBeforeTheEndEndsTheJob(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    # the last batch loaded everything and moved the pointer on, then its worker died before ending the job
    for name in "ab":
        job.jobs.setTable(PROJECT, jobId, name, status="loaded", rows=1)
    job.redis.set(f"import:{PROJECT}:next", f"{jobId}:done")

    again = _FakeTask(retries=1)
    batch(again, jobId, listed, 0)

    assert job.jobs.get(PROJECT)["state"] == "done"
    assert again.sent == [] and again.retried is None


def test_lateDuplicateLeavesAnEndedJobAsItIs(job):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    job.redis.set(f"import:{PROJECT}:next", f"{jobId}:done")
    service.failJob(PROJECT, jobId)

    late = _FakeTask(retries=1)
    batch(late, jobId, [list(t) for t in tables], 0)

    assert job.jobs.get(PROJECT)["state"] == "failed"
    assert late.sent == [] and late.retried is None


def test_lastBatchEndsTheJobDone(job):
    makeTables(job.engine, {name: (1, 1) for name in "abc"})
    tables, jobId = startJob(job)

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    record = job.jobs.get(PROJECT)
    assert record["state"] == "done" and "kpis" not in record
    assert all(t["status"] == "loaded" for t in record["tables"])


def test_jobEndsDoneWhenEveryTableFailed(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    monkeypatch.setattr(service, "readChunks", Mock(side_effect=RuntimeError("unreadable")))

    assert service.runBatch(PROJECT, USER, jobId, MYSQL, tables, 0) is None

    record = job.jobs.get(PROJECT)
    assert record["state"] == "done"
    assert [(t["status"], t["reason"]) for t in record["tables"]] == [("failed", "Could not read this table")] * 2


def test_noModelCallDuringTheImport(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(9)})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    task = _FakeTask()

    batch(task, jobId, listed, 0)
    batch(task, jobId, listed, 8)

    assert job.jobs.get(PROJECT)["state"] == "done"
    assert not job.metadata.called and not job.insights.called


def test_crashedDeliveryIsRetriedNotDropped(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(9)})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    # the dead delivery stored t00 and still holds the marker; the broker hands the batch out again at once
    job.jobs.setTable(PROJECT, jobId, "t00", status="loading")
    job.storage.put(PROJECT, "t00.parquet", b"written before the crash")
    job.redis.set(f"import:{PROJECT}:batch", f"{jobId}:0", nx=True, ex=900)

    task = _FakeTask(retries=0)
    with pytest.raises(Retry):
        batch(task, jobId, listed, 0)
    assert task.retried == (300, 6, REDACTED) and task.sent == []

    job.redis.delete(f"import:{PROJECT}:batch")  # the dead delivery's marker runs out
    task = _FakeTask(retries=1)
    batch(task, jobId, listed, 0)

    # t00 was stored by the dead delivery and does not count, so the other eight fit and the job ends
    assert task.sent == []
    assert loadedNames(job) == [f"t{i:02}" for i in range(9)]
    assert job.jobs.get(PROJECT)["state"] == "done"


def test_lateDuplicateOfAFinishedBatchExitsQuietly(job):
    makeTables(job.engine, {f"t{i:02}": (1, 1) for i in range(9)})
    tables, jobId = startJob(job)
    listed = [list(t) for t in tables]
    batch(_FakeTask(), jobId, listed, 0)
    before = job.jobs.get(PROJECT)

    late = _FakeTask()
    batch(late, jobId, listed, 0)

    assert late.sent == [] and late.retried is None
    assert job.jobs.get(PROJECT) == before


def test_busyRetriesSpentWhileHeldExitsQuietly(job):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    job.redis.set(f"import:{PROJECT}:batch", f"{jobId}:0", nx=True, ex=900)
    before = job.jobs.get(PROJECT)

    task = _FakeTask(retries=6)
    batch(task, jobId, [list(t) for t in tables], 0)

    assert task.sent == [] and task.retried is None
    assert job.jobs.get(PROJECT) == before
    assert job.redis.get(f"import:{PROJECT}:batch") == f"{jobId}:0".encode()


SECRET_SOURCES = [
    Source(kind="mysql", host="db-host.test", address="192.0.2.1", port=3306, user="alice-login", password="s3cret-pass", db="d"),
    Source(kind="mongodb", db="d", connectionString="mongodb://alice-login:s3cret-pass@db-host.test/d"),
]


@pytest.mark.parametrize("source", SECRET_SOURCES, ids=lambda source: source.kind)
def test_queuedMessagesHoldNoCredentials(job, monkeypatch, source):
    from nubrix.triggers.celery import celeryApp

    jobId = job.jobs.start(PROJECT, [("a", "a"), ("b", "b")])
    listed = [["a", None, "a"], ["b", None, "b"]]
    given = []
    monkeypatch.setattr(service, "runBatch", lambda *args, **_: given.append(args[3]) or (1 if args[5] == 0 else None))
    messages = []
    monkeypatch.setattr(celeryApp, "producer_or_acquire", lambda producer=None: contextlib.nullcontext(MagicMock()))
    monkeypatch.setattr(celeryApp.amqp, "send_task_message", lambda producer, name, message, **options: messages.append(message))
    task = _FakeTask()
    task.app = celeryApp

    tasks.sendBatch(celeryApp, PROJECT, USER, jobId, source, listed, 0)  # what the route queues
    assert "s3cret-pass" not in json.dumps(messages[0].body, default=dataclasses.asdict)
    tasks.importBatch(task, *messages[0].body[0])  # queues the next batch
    tasks.importBatch(task, *messages[1].body[0])  # the last batch queues nothing

    assert [m.headers["task"] for m in messages] == ["NubrixAI.importBatch", "NubrixAI.importBatch"]
    for message in messages:
        queued = json.dumps([message.headers, message.body, message.sent_event], default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o))
        for private in ("s3cret-pass", "alice-login", "db-host.test"):
            assert private not in queued
        assert (message.headers["argsrepr"], message.headers["kwargsrepr"]) == ("(redacted)", "{}")
        assert message.headers["ignore_result"] is True
    assert given == [source, source]  # the worker still gets the connection


@pytest.mark.parametrize("forgery", ["altered", "otherProject", "otherJob", "otherKey", "plainText"])
def test_sourceNotSealedForThisJobIsRefused(job, monkeypatch, forgery):
    jobId = job.jobs.start(PROJECT, [("a", "a")])
    source = SECRET_SOURCES[0]
    if forgery == "plainText":
        sealedSource = dataclasses.asdict(source)
    else:
        with monkeypatch.context() as sealing:
            if forgery == "otherKey":
                sealing.setenv("SECRET_KEY", "another-secret")
            sealedSource = sealed("another-job" if forgery == "otherJob" else jobId, source,
                                  "another-project" if forgery == "otherProject" else PROJECT, tables=[["a", None, "a"]])
        if forgery == "altered":
            middle = len(sealedSource) // 2
            sealedSource = sealedSource[:middle] + ("A" if sealedSource[middle] != "A" else "B") + sealedSource[middle + 1:]
    run = Mock(return_value=None)
    monkeypatch.setattr(service, "runBatch", run)
    task = _FakeTask()

    with capturedLogs(monkeypatch, tasks) as messages:
        tasks.importBatch(task, PROJECT, USER, jobId, sealedSource, [["a", None, "a"]], 0)

    assert not run.called and task.sent == []
    assert job.jobs.get(PROJECT)["state"] == "failed"
    assert not any("s3cret-pass" in str(m) for m in messages)


@pytest.mark.parametrize("change", ["userId", "tables", "start"])
def test_everyMessageArgumentIsBound(job, monkeypatch, change):
    jobId = job.jobs.start(PROJECT, [("a", "a"), ("b", "b")])
    tables = [["a", None, "a"], ["b", None, "b"]]
    token = sealed(jobId, tables=tables)
    run = Mock(return_value=None)
    monkeypatch.setattr(service, "runBatch", run)
    task = _FakeTask()

    if change == "userId":
        tasks.importBatch(task, PROJECT, "another-user", jobId, token, tables, 0)
    elif change == "tables":
        tasks.importBatch(task, PROJECT, USER, jobId, token, [["users", "auth", "a"], ["b", None, "b"]], 0)
    else:
        tasks.importBatch(task, PROJECT, USER, jobId, token, tables, 99)

    assert not run.called and task.sent == []
    assert job.jobs.get(PROJECT)["state"] == "failed"


def test_storedNameThatLeavesTheProjectFailsTheJob(job, monkeypatch):
    name = "../99999999-0000-4000-8000-000000000000/stolen"
    jobId = job.jobs.start(PROJECT, [("a", name)])
    read = Mock()
    monkeypatch.setattr(service, "readChunks", read)

    batch(_FakeTask(), jobId, [["a", None, name]])

    assert not read.called and job.jobs.get(PROJECT)["state"] == "failed"
    assert not [key for key in job.storage.objects if "stolen" in key]


@pytest.mark.parametrize("key", [None, ""])
def test_nothingIsQueuedWithoutAKey(monkeypatch, key):
    if key is None:
        monkeypatch.delenv("SECRET_KEY")
    else:
        monkeypatch.setenv("SECRET_KEY", key)
    task = _FakeTask()

    with pytest.raises((KeyError, ValueError)):
        tasks.sendBatch(task.app, PROJECT, USER, "job", MYSQL, [], 0)

    assert task.sent == []


def test_unexpectedErrorFailsJobWithoutLeakingIt(job, monkeypatch):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    monkeypatch.setattr(service, "runBatch", Mock(side_effect=RuntimeError("password=hunter2")))
    task = _FakeTask()
    with capturedLogs(monkeypatch, tasks) as messages:
        batch(task, jobId, [list(t) for t in tables], 0)

    record = job.jobs.get(PROJECT)
    assert record["state"] == "failed" and "kpis" not in record
    assert task.sent == []
    assert not any("hunter2" in str(m) for m in messages)


MONGO = Source(kind="mongodb", connectionString="mongodb+srv://u:p@cluster.test", db="d")


def test_chunkSizeSettingsReachTheReader(job, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "readChunks", lambda source, table, *sizes: calls.append((source.kind, table, sizes)) or iter(()))
    jobId = job.jobs.start(PROJECT, [("a", "a")])
    service.runBatch(PROJECT, USER, jobId, MYSQL, [("a", None)], 0)
    job.jobs.finish(PROJECT, jobId, "done")
    jobId = job.jobs.start(PROJECT, [("m", "m")])
    service.runBatch(PROJECT, USER, jobId, MONGO, [("m", None)], 0)

    assert calls == [("mysql", ("a", None), (50000, 2000000)), ("mongodb", ("m", None), (5000, 2000000))]


def eraseProject(job):
    """What an erasure or a project delete leaves: no import keys, no project row, an empty folder."""
    job.jobs.forget(PROJECT)
    job.project.exists = False
    for key in [key for key in job.storage.objects if key.startswith(f"AnalyticsHub/{PROJECT}/")]:
        del job.storage.objects[key]


def projectFiles(job):
    return sorted(key.rsplit("/", 1)[1] for key in job.storage.objects if key.startswith(f"AnalyticsHub/{PROJECT}/"))


def test_importStartedAfterTheProjectIsDeletedWritesNothing(job):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)  # the route listed tables while the delete's own cleanup ran
    job.project.exists = False
    task = _FakeTask()

    batch(task, jobId, [list(t) for t in tables])

    assert projectFiles(job) == [] and task.sent == []
    assert not job.metadata.called and not job.insights.called
    assert job.redis.values == {}


def test_projectCheckErrorLetsTheImportGoOn(job, monkeypatch):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    monkeypatch.setattr(service, "_projectExists", Mock(side_effect=ConnectionError("Server disconnected")))
    task = _FakeTask()

    batch(task, jobId, [list(t) for t in tables])

    assert job.jobs.get(PROJECT)["state"] == "done" and "a.parquet" in projectFiles(job)
    assert task.sent == []


@pytest.mark.parametrize("rows, exists", [([], False), ([{"projectId": PROJECT}], True)])
def test_projectExistsReadsTheProjectsRow(monkeypatch, rows, exists):
    calls = []

    class Query:
        def __getattr__(self, name):
            return lambda *args: calls.append((name, *args)) or self

        def execute(self):
            return types.SimpleNamespace(data=rows)

    monkeypatch.setattr(dataLoadModule.dataLoadService, "client", Query())

    assert service._projectExists(PROJECT) is exists
    assert ("table", "Projects") in calls and ("eq", "projectId", PROJECT) in calls


def test_deleteDuringTheLastProgressWriteLeavesNoKeys(job, monkeypatch):
    makeTables(job.engine, {"a": (1, 1)})
    tables, jobId = startJob(job)
    realSet = job.redis.set

    def deleteThenSet(key, value, **options):
        if key.endswith(":next"):
            eraseProject(job)
        return realSet(key, value, **options)

    monkeypatch.setattr(job.redis, "set", deleteThenSet)
    task = _FakeTask()
    batch(task, jobId, [list(t) for t in tables])

    assert job.redis.values == {} and projectFiles(job) == [] and task.sent == []


def test_forgottenJobStopsWithoutWritingMoreTables(job, monkeypatch):
    makeTables(job.engine, {name: (1, 1) for name in "ab"})
    tables, jobId = startJob(job)
    realRead = service.readChunks

    def readChunks(source, table, *sizes):
        job.jobs.forget(PROJECT)  # the project is deleted while its first table is read
        return realRead(source, table, *sizes)

    monkeypatch.setattr(service, "readChunks", readChunks)
    task = _FakeTask()
    batch(task, jobId, [list(t) for t in tables], 0)

    assert task.sent == [] and task.retried is None
    assert job.jobs.get(PROJECT) is None
    assert not [key for key in job.storage.objects if key.endswith(".parquet")]
    assert not job.metadata.called


def _management(monkeypatch, jobs):
    monkeypatch.setattr(managementModule, "importJobs", jobs)
    monkeypatch.setattr(managementModule, "removeProjectFiles", lambda client, projectId: None)
    management = object.__new__(managementModule.ManagementService)
    management.client = MagicMock()
    return management


def test_deleteProjectForgetsItsImport(monkeypatch):
    jobs = ImportJobs(FakeRedis())
    jobId = jobs.start(PROJECT, [("a", "a")])

    _management(monkeypatch, jobs).deleteProject(PROJECT)

    assert jobs.get(PROJECT) is None
    assert jobs.setTable(PROJECT, jobId, "a", status="loaded") is False


def test_deleteProjectSurvivesARedisFailure(monkeypatch):
    jobs = Mock()
    jobs.forget.side_effect = ConnectionError("redis://:hunter2@redis.test")
    management = _management(monkeypatch, jobs)
    with capturedLogs(monkeypatch, managementModule) as messages:
        management.deleteProject(PROJECT)

    management.client.table.assert_called_with("Projects")
    assert any("ConnectionError" in str(m) for m in messages)
    assert not any("hunter2" in str(m) for m in messages)
