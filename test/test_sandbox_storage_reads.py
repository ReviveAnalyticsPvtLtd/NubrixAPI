"""Generated code reads tables from real sandbox subprocesses.

Each launcher runs with the filtered environment its parent builds, and the
parent reads tables for it over a private channel, so these tests fail if a
credential reaches the child or a read stops going through the parent. Another
project's table sits in the same bucket, so a missing project check reads it
instead of failing on a 404.

The tests marked `separateUids` need a root parent on Linux. They run in the
image, not on a Windows checkout.
"""

import glob
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import MagicMock

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nubrix.components import transformationExecutor
from nubrix.components.transformationExecutor import TransformationExecutor
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import codeExecutor, initMethods, projectStorage, sandbox_reaper, sandboxRunner

PROJECT = "11111111-2222-4333-8444-555555555555"
OTHER_PROJECT = "99999999-8888-4777-8666-555555555555"
READERS = ("fetch_data", "fetch_data_pl", "scan_data")
READ_EVERY_WAY = (
    f'a = fetch_data("{PROJECT}", "sales")\n'
    f'b = fetch_data_pl("{PROJECT}", "sales")\n'
    f'c = scan_data("{PROJECT}", "sales").collect()\n'
)
PARENT_SECRETS = (
    "DATABASE_URL", "STORAGE_URL",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENAI_API_KEY",
    "RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "BREVO_API_KEY",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST",
    "LANGSMITH_API_KEY", "LOGTAIL_SOURCE_TOKEN",
)
SECRET_VALUE = "parent-only-secret"
READ_AND_LIST_SECRETS = READ_EVERY_WAY + (
    "env = pd.io.common.os.environ\n"
    f'seen = ",".join(sorted(k for k, v in env.items() if v == "{SECRET_VALUE}"))\n'
)


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1, 2, 3]}).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    fake.put(OTHER_PROJECT, "sales.parquet", buffer.getvalue())
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")
    yield fake
    fake.close()


@pytest.fixture
def parentSecrets(storage, monkeypatch):
    for name in PARENT_SECRETS:
        monkeypatch.setenv(name, SECRET_VALUE)


def _assertReadWithTheServiceKey(storage):
    reads = [r for r in storage.requests if r["method"] == "GET"]
    assert reads
    assert all(r["path"] == f"/storage/v1/object/AnalyticsHub/{PROJECT}/sales.parquet" for r in reads)
    assert all(r["authorization"] == f"Bearer {SERVICE_KEY}" for r in reads)


def test_codeExecutionSandboxReadsTheBucket(storage, monkeypatch):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(READ_EVERY_WAY + "print(len(a), b.height, c.height)", projectId=PROJECT)

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def test_transformationSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    frame = executor._execute_code(PROJECT, READ_EVERY_WAY + "final_df = b")

    assert list(frame["amount"]) == [1, 2, 3]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, READ_EVERY_WAY + "print(len(a), b.height, c.height)")

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def test_codeExecutionSandboxReadsWithoutTheParentSecrets(storage, parentSecrets, monkeypatch):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(READ_AND_LIST_SECRETS + "print(len(a), b.height, c.height, seen)", projectId=PROJECT)

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def test_transformationSandboxReadsWithoutTheParentSecrets(storage, parentSecrets):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = READ_AND_LIST_SECRETS + 'final_df = pd.DataFrame({"rows": [len(a), b.height, c.height], "seen": seen})'

    frame = executor._execute_code(PROJECT, code)

    assert list(frame["rows"]) == [3, 3, 3]
    assert list(frame["seen"]) == ["", "", ""]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxReadsWithoutTheParentSecrets(storage, parentSecrets):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, READ_AND_LIST_SECRETS + "print(len(a), b.height, c.height, seen)")

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def _assertOtherProjectUntouched(storage):
    assert not [path for path in storage.pathsRead() if OTHER_PROJECT in path]


def _assertOtherProjectNotReadWithTheServiceKey(storage):
    keyed = [r for r in storage.requests if OTHER_PROJECT in r["path"] and r["authorization"] == f"Bearer {SERVICE_KEY}"]
    assert not keyed


@pytest.mark.parametrize("reader", READERS)
def test_codeExecutionSandboxRefusesAnotherProject(storage, monkeypatch, reader):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(f'print(type({reader}("{OTHER_PROJECT}", "sales")).__name__)', projectId=PROJECT)

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


@pytest.mark.parametrize("reader", READERS)
def test_transformationSandboxRefusesAnotherProject(storage, reader):
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, f'final_df = {reader}("{OTHER_PROJECT}", "sales")')

    _assertOtherProjectUntouched(storage)


@pytest.mark.parametrize("reader", READERS)
def test_inspectionSandboxRefusesAnotherProject(storage, reader):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, f'print(type({reader}("{OTHER_PROJECT}", "sales")).__name__)')

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxRefusesAnotherProjectByKeyword(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, f'final_df = fetch_data_pl(projectId="{OTHER_PROJECT}", tableName="sales")')

    _assertOtherProjectUntouched(storage)


def test_inspectionSandboxRefusesAnotherProjectByKeyword(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, f'print(type(fetch_data_pl(projectId="{OTHER_PROJECT}", tableName="sales")).__name__)')

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxIgnoresARebuiltProjectId(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = f'projectId = "{OTHER_PROJECT}"\nfinal_df = fetch_data_pl(projectId, "sales")'

    with pytest.raises(ValueError, match="PermissionError: Access denied"):
        executor._execute_code(PROJECT, code)

    _assertOtherProjectUntouched(storage)


def test_inspectionSandboxIgnoresARebuiltProjectId(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = f'projectId = "{OTHER_PROJECT}"\nprint(type(fetch_data_pl(projectId, "sales")).__name__)'

    output = executor.executeInspection(PROJECT, code)

    assert "PermissionError: Access denied" in output
    _assertOtherProjectUntouched(storage)


def test_transformationSandboxKeepsEveryOwnProjectCallShape(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = (
        'a = fetch_data_pl(projectId=projectId, tableName="sales")\n'
        'b = scan_data(projectId, tableName="sales").collect()\n'
        'c = fetch_data(projectId, "sales", [{"amount": 2}])\n'
        'd = fetch_data(projectId=projectId, tableName="sales", baseFilters=[{"amount": 3}])\n'
        'final_df = pd.DataFrame({"sizes": [a.height, b.height, len(c), len(d)]})'
    )

    frame = executor._execute_code(PROJECT, code)

    assert list(frame["sizes"]) == [3, 3, 1, 1]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxKeepsEveryOwnProjectCallShape(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = (
        'a = fetch_data_pl(projectId=projectId, tableName="sales")\n'
        'b = scan_data(projectId, tableName="sales").collect()\n'
        'c = fetch_data(projectId, "sales", [{"amount": 2}])\n'
        'd = fetch_data(projectId=projectId, tableName="sales", baseFilters=[{"amount": 3}])\n'
        'print(a.height, b.height, len(c), len(d))'
    )

    output = executor.executeInspection(PROJECT, code)

    assert output.strip() == "3 3 1 1"
    _assertReadWithTheServiceKey(storage)


@pytest.fixture(params=("codeExecution", "inspection", "transformation"))
def runInSandbox(request, storage, monkeypatch):
    """Run code that sets `outcome` and return what the launcher reports, traceback included."""
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)
    executor = TransformationExecutor(timeoutSeconds=120)

    def run(code):
        if request.param == "codeExecution":
            return manager.run(f"{code}\nprint(outcome)", projectId=PROJECT)
        if request.param == "inspection":
            return executor.executeInspection(PROJECT, f"{code}\nprint(outcome)")
        try:
            frame = executor._execute_code(PROJECT, f'{code}\nfinal_df = pd.DataFrame({{"outcome": [str(outcome)]}})')
        except ValueError as error:
            return str(error)
        return frame["outcome"][0]

    return run


def test_sandboxCannotReadTheServiceKeyFromItsEnvironment(runInSandbox):
    output = runInSandbox('outcome = pd.io.common.os.environ["SUPABASE_KEY"]')

    assert "KeyError" in output
    assert SERVICE_KEY not in output


def test_sandboxEnvironmentHoldsNoStorageOrRedisSettings(runInSandbox):
    output = runInSandbox('outcome = sorted(pd.io.common.os.environ)')

    assert "SUPABASE" not in output
    assert "REDIS" not in output


def test_sandboxCannotReadAnotherProjectWithStorageOptions(runInSandbox, storage):
    url = f"{storage.url}/storage/v1/object/AnalyticsHub/{OTHER_PROJECT}/sales.parquet"
    code = (
        'key = pd.io.common.os.environ.get("SUPABASE_KEY", "none")\n'
        f'options = {{"apikey": key, "Authorization": "Bearer " + key}}\n'
        f'outcome = "rows " + str(len(pd.read_parquet("{url}", storage_options=options)))'
    )

    output = runInSandbox(code)

    assert "rows 3" not in output
    _assertOtherProjectNotReadWithTheServiceKey(storage)


def test_sandboxCannotReadAnotherProjectThroughTheRealReader(runInSandbox, storage):
    code = (
        'cells = [cell.cell_contents for cell in fetch_data_pl.__closure__]\n'
        'reader = [cell for cell in cells if hasattr(cell, "__call__")][0]\n'
        f'outcome = reader("{OTHER_PROJECT}", "sales")'
    )

    runInSandbox(code)

    _assertOtherProjectUntouched(storage)


@pytest.mark.parametrize("tableName", (f"../{OTHER_PROJECT}/sales", f"..%2f{OTHER_PROJECT}%2fsales"))
def test_sandboxCannotClimbOutOfItsProject(runInSandbox, storage, tableName):
    output = runInSandbox(f'outcome = fetch_data_pl("{PROJECT}", "{tableName}")')

    assert "Invalid storage path segment" in output
    _assertOtherProjectUntouched(storage)


def test_sandboxReportsAMissingTableWithoutStorageDetail(runInSandbox, storage):
    output = runInSandbox(f'outcome = fetch_data_pl("{PROJECT}", "missing")')

    assert "Could not read table 'missing'" in output
    assert storage.url not in output


def test_sandboxReadsARedisHitWithoutTouchingStorage(runInSandbox, storage, monkeypatch):
    monkeypatch.setattr(initMethods, "_fetch_data_from_redis", lambda _key: pa.table({"amount": [7, 8]}))

    output = runInSandbox(f'outcome = fetch_data_pl("{PROJECT}", "sales").height')

    assert output.strip() == "2"
    assert not storage.requests


def test_parentIgnoresAProjectInTheRequest(storage, tmp_path):
    from utils.sandboxRunner import serveTables

    request = json.dumps({"projectId": OTHER_PROJECT, "tableName": "sales"}).encode()
    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(request + b'\n"sales"\n'), replies, str(tmp_path), threading.Event())

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert "error" in first
    assert (tmp_path / second["file"]).exists()
    _assertOtherProjectUntouched(storage)


def test_parentSpoolsATableOnceInARun(storage, tmp_path):
    from utils.sandboxRunner import serveTables

    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"sales"\n"sales"\n'), replies, str(tmp_path), threading.Event())

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert first == second
    assert len([r for r in storage.requests if r["method"] == "GET"]) == 1


def test_failedDownloadDoesNotBreakTheNextRead(storage, tmp_path, monkeypatch):
    from utils.sandboxRunner import serveTables

    realDownload = initMethods.downloadTable

    def failAfterAPartialWrite(projectId, tableName, path, runEnded=None):
        if tableName == "broken":
            with open(path, "xb") as partial:
                partial.write(b"PAR1")
            raise OSError("connection dropped")
        realDownload(projectId, tableName, path, runEnded)

    monkeypatch.setattr(initMethods, "downloadTable", failAfterAPartialWrite)
    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"broken"\n"sales"\n'), replies, str(tmp_path), threading.Event())

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert first == {"error": "Could not read table 'broken'."}
    assert pl.read_parquet(tmp_path / second["file"]).height == 3


def test_parentCachesASmallTableInRedisAfterAColdRead(storage, tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda key, table: stored.update({key: table.num_rows}))

    initMethods.spool_table(PROJECT, "sales", str(tmp_path / "0"))

    assert stored == {f"{PROJECT}::sales": 3}


def test_parentKeepsAWideTableAsParquetWithoutDecodingIt(storage, tmp_path, monkeypatch):
    monkeypatch.setattr(initMethods, "_REDIS_MAX_DECODED_BYTES", 1)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the parent decoded a table too wide to cache")

    monkeypatch.setattr(pq, "read_table", refuse)

    initMethods.spool_table(PROJECT, "sales", str(tmp_path / "0"))


def test_parentKeepsABigTableAsParquetWithoutDecodingIt(storage, tmp_path, monkeypatch):
    buffer = io.BytesIO()
    pl.DataFrame({"amount": list(range(100_001))}).write_parquet(buffer)
    storage.put(PROJECT, "big.parquet", buffer.getvalue())

    def refuse(*_args, **_kwargs):
        raise AssertionError("the parent decoded a table Redis never caches")

    monkeypatch.setattr(pq, "read_table", refuse)

    path = initMethods.spool_table(PROJECT, "big", str(tmp_path / "0"))

    with open(path, "rb") as spooled:
        assert spooled.read() == buffer.getvalue()


GRAB_THE_CHANNEL = (
    'source = pd.io.common.os.sys.modules["utils.initMethods"]._TABLE_SOURCE\n'
    'pipes = [c.cell_contents for c in source.__closure__ if hasattr(c.cell_contents, "writable")]\n'
    'requests = [pipe for pipe in pipes if pipe.writable()][0]\n'
)


def _finishesWithin(seconds, call):
    """Run `call` on a thread and return its result, failing if it is still blocked at the deadline."""
    outputs = []
    run = threading.Thread(target=lambda: outputs.append(call()), daemon=True)

    run.start()
    run.join(timeout=seconds)

    assert not run.is_alive()
    return outputs[0]


def test_childThatFloodsTheChannelCantHangTheParent(storage):
    code = GRAB_THE_CHANNEL + (
        'for _ in range(100000):\n'
        '    requests.write(b\'"sales"\\n\')\n'
        '    requests.flush()\n'
    )
    executor = TransformationExecutor(timeoutSeconds=15)

    output = _finishesWithin(60, lambda: executor.executeInspection(PROJECT, code))

    assert "timed out" in output


def test_childCantHangTheParentWithABlankLine(storage):
    code = GRAB_THE_CHANNEL + (
        'import time\n'
        'requests.write(b"\\n")\n'
        'requests.flush()\n'
        'time.sleep(1)\n'
        'requests.write(b"x" * 65536)\n'
        'requests.flush()\n'
        'print("exiting")\n'
    )
    executor = TransformationExecutor(timeoutSeconds=10)

    output = _finishesWithin(30, lambda: executor.executeInspection(PROJECT, code))

    assert output.strip() == "exiting"


@pytest.mark.skipif(os.name != "posix", reason="needs fork")
def test_processHoldingTheOutputCantHangTheParent(storage):
    code = (
        'import time\n'
        'os = pd.io.common.os\n'
        'if os.fork() == 0:\n'
        '    os.setsid()\n'
        '    time.sleep(60)\n'
        '    os._exit(0)\n'
        'time.sleep(60)\n'
    )
    executor = TransformationExecutor(timeoutSeconds=5)

    output = _finishesWithin(30, lambda: executor.executeInspection(PROJECT, code))

    assert "timed out" in output


def test_outputIsCappedInTheParent(storage, monkeypatch):
    monkeypatch.setattr(sandboxRunner, "_MAX_OUTPUT_BYTES", 1024)
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, 'print("x" * 100000)')

    assert len(output) < 2048
    assert "truncated" in output


def test_runStopsSpoolingAtTheCap(storage, tmp_path, monkeypatch):
    from utils.sandboxRunner import serveTables

    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1]}).write_parquet(buffer)
    storage.put(PROJECT, "other.parquet", buffer.getvalue())
    monkeypatch.setattr(sandboxRunner, "_MAX_SPOOL_BYTES", 1)
    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"sales"\n"other"\n"sales"\n'), replies, str(tmp_path), threading.Event())

    first, second, third = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert "file" in first
    assert "error" in second
    assert third == first


def test_blankLineDoesNotEndServing(storage, tmp_path):
    from utils.sandboxRunner import serveTables

    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'\n"sales"\n'), replies, str(tmp_path), threading.Event())

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert "error" in first
    assert "file" in second


def test_serveTablesStopsWhenTheRunEnds(storage, tmp_path):
    from utils.sandboxRunner import serveTables

    runEnded = threading.Event()
    runEnded.set()
    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"sales"\n'), replies, str(tmp_path), runEnded)

    assert not replies.getvalue()
    assert not storage.requests


def test_downloadStopsWhenTheRunEnds(storage, tmp_path):
    runEnded = threading.Event()
    runEnded.set()

    with pytest.raises(InterruptedError):
        projectStorage.downloadTable(PROJECT, "sales", str(tmp_path / "0"), runEnded)


def _runFolders():
    root = os.environ.get("SANDBOX_RUN_ROOT") or tempfile.gettempdir()
    return set(glob.glob(os.path.join(root, "sandbox-*")))


@pytest.mark.parametrize("code", (
    'fetch_data_pl(projectId, "sales")\nprint("done")',
    'raise RuntimeError("failed on purpose")',
    'import time\ntime.sleep(120)',
))
def test_runLeavesNoFolderBehind(storage, code, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    executor = TransformationExecutor(timeoutSeconds=20)
    before = _runFolders()

    executor.executeInspection(PROJECT, code)

    assert _runFolders() == before


def test_transformationResultStillComesBack(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    rows, parquetBytes = executor.executeAndPreview(PROJECT, 'final_df = fetch_data_pl(projectId, "sales")', "copy")

    assert rows == [{"amount": 1}, {"amount": 2}, {"amount": 3}]
    assert pl.read_parquet(io.BytesIO(parquetBytes)).height == 3


def test_previewDecodesOnlyTheRowsItShows(storage):
    executor = TransformationExecutor(timeoutSeconds=120)
    code = 'final_df = pd.DataFrame({"a": range(1000)})'

    rows, parquetBytes = executor.executeAndPreview(PROJECT, code, "copy")

    assert len(rows) == 100
    assert len(executor._execute_code(PROJECT, code)) == 100
    assert pl.read_parquet(io.BytesIO(parquetBytes)).height == 1000


@pytest.mark.parametrize("launcher", ("sandbox_launcher.py", "inspect_launcher.py"))
def test_launcherWithNoResultNeverHasItsResultFileRead(storage, launcher):
    code = (
        'os = pd.io.common.os\n'
        'fd = os.open("result.parquet", os.O_CREAT | os.O_WRONLY, 0o600)\n'
        'os.write(fd, b"x" * 1024)\n'
        'os.close(fd)\n'
    )

    result = sandboxRunner.runSandbox(launcher, PROJECT, code, 120)

    assert result.returncode == 0
    assert result.resultBytes is None


def test_parentRefusesAResultFileOverTheCap(storage, monkeypatch):
    monkeypatch.setattr(sandboxRunner, "_MAX_RESULT_BYTES", 16)
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="too large"):
        executor._execute_code(PROJECT, 'final_df = fetch_data_pl(projectId, "sales")')


def test_parentChecksTheResultFooterBeforeDecoding(storage, monkeypatch):
    monkeypatch.setattr(transformationExecutor, "detect_resources", lambda: {"total_memory_bytes": 2})

    def refuse(*_args, **_kwargs):
        raise AssertionError("the parent decoded a result it should have refused")

    monkeypatch.setattr(pl, "read_parquet", refuse)
    executor = TransformationExecutor(timeoutSeconds=120)

    with pytest.raises(ValueError, match="too large"):
        executor._run_transformation(PROJECT, 'final_df = fetch_data_pl(projectId, "sales")')


@pytest.mark.skipif(os.name != "posix", reason="links and pipes behave differently on Windows")
@pytest.mark.parametrize("plant", (lambda path: os.symlink("/etc/hostname", path), lambda path: os.mkfifo(path)))
def test_parentRefusesAResultThatIsNotARegularFile(tmp_path, plant):
    from utils.sandboxRunner import readResult

    path = tmp_path / "result.parquet"
    plant(path)

    with pytest.raises(ValueError, match="not a regular file"):
        readResult(str(path), None)


separateUids = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="only a root parent on Linux can run a child as another uid",
)
inTheImage = pytest.mark.skipif(not os.environ.get("SANDBOX_RUN_ROOT"), reason="checks the built image")
FORCED_RUN_UID = sandboxRunner._FIRST_RUN_UID + 7
LEAVE_A_PROCESS_BEHIND = (
    'import time\n'
    'os = pd.io.common.os\n'
    'if os.fork() == 0:\n'
    '    os.setsid()\n'
    '    for fd in (0, 1, 2):\n'
    '        os.close(fd)\n'
    '    time.sleep(120)\n'
    '    os._exit(0)\n'
)


def _forceRunUid(monkeypatch):
    """Stands for the random pick landing on a uid a run has used before."""
    monkeypatch.setattr(sandboxRunner.secrets, "randbelow", lambda _count: 7)


def _liveRunUidProcesses():
    processes = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            with open(f"/proc/{pid}/stat") as source:
                state = source.read().rsplit(") ", 1)[1][0]
        except OSError:
            continue
        if sandboxRunner._FIRST_RUN_UID <= uid < sandboxRunner._FIRST_RUN_UID + sandboxRunner._RUN_UID_COUNT and state != "Z":
            processes.append(pid)
    return processes


def _worldWritableFolder():
    folder = os.path.join(tempfile.gettempdir(), f"handoff-{uuid.uuid4().hex}")
    os.mkdir(folder)
    os.chmod(folder, 0o777)
    return folder


@separateUids
def test_runLeavesNoProcessBehind(storage):
    executor = TransformationExecutor(timeoutSeconds=60)

    output = executor.executeInspection(PROJECT, LEAVE_A_PROCESS_BEHIND + 'print("done")')

    assert output.strip() == "done"
    deadline = time.time() + 5
    while _liveRunUidProcesses() and time.time() < deadline:
        time.sleep(0.05)
    assert not _liveRunUidProcesses()


@separateUids
def test_leftoverProcessCantReadALaterRunThatGetsItsUid(storage, monkeypatch):
    _forceRunUid(monkeypatch)
    handoff = _worldWritableFolder()
    attacker = (
        'import time\n'
        'os = pd.io.common.os\n'
        'tables = os.path.join(os.path.dirname(os.getcwd()), "tables")\n'
        'if os.fork() == 0:\n'
        '    os.setsid()\n'
        '    for fd in (0, 1, 2):\n'
        '        os.close(fd)\n'
        '    deadline = time.time() + 30\n'
        '    while time.time() < deadline:\n'
        '        try:\n'
        '            if os.listdir(tables):\n'
        f'                os.close(os.open("{handoff}/stolen", os.O_CREAT | os.O_WRONLY))\n'
        '                os._exit(0)\n'
        '        except OSError:\n'
        '            pass\n'
        '        time.sleep(0.02)\n'
        '    os._exit(0)\n'
    )
    executor = TransformationExecutor(timeoutSeconds=60)

    executor.executeInspection(PROJECT, attacker)
    executor.executeInspection(OTHER_PROJECT, 'fetch_data_pl(projectId, "sales")\nimport time\ntime.sleep(2)')

    assert not os.path.exists(os.path.join(handoff, "stolen"))


@separateUids
def test_runEndsAnyProcessAlreadyHoldingItsUid(storage, monkeypatch):
    _forceRunUid(monkeypatch)
    leftover = subprocess.Popen(["sleep", "120"], user=FORCED_RUN_UID, group=FORCED_RUN_UID, extra_groups=[])
    try:
        TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, 'print("ran")')

        assert leftover.wait(timeout=5) == -signal.SIGKILL
    finally:
        leftover.kill()


@separateUids
def test_uidStaysClaimedIfItsProcessesCantBeEnded(storage, monkeypatch):
    outcomes = iter((True, False))
    monkeypatch.setattr(sandboxRunner, "_clearUid", lambda _runUid: next(outcomes))
    before = _runFolders()

    output = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, 'fetch_data_pl(projectId, "sales")\nprint("ran")')

    left = _runFolders() - before
    assert output.strip() == "ran"
    assert len(left) == 1
    folder = left.pop()
    try:
        assert os.listdir(folder) == []
    finally:
        os.rmdir(folder)


@separateUids
def test_runRefusesToStartIfItsUidCantBeCleared(storage, monkeypatch):
    monkeypatch.setattr(sandboxRunner, "_clearUid", lambda _runUid: False)
    started = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", started)
    before = _runFolders()

    with pytest.raises(RuntimeError, match="clear"):
        TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, 'print("ran")')

    started.assert_not_called()
    for folder in _runFolders() - before:
        os.rmdir(folder)


@separateUids
def test_runRemovesItsFilesFromSharedTempFolders(storage):
    code = (
        'os = pd.io.common.os\n'
        'for folder in ("/tmp", "/var/tmp", "/dev/shm"):\n'
        '    name = f"{folder}/left-{os.getuid()}"\n'
        '    os.close(os.open(name, os.O_CREAT | os.O_WRONLY))\n'
        '    os.mkdir(name + "-folder")\n'
        '    os.close(os.open(name + "-folder/inside", os.O_CREAT | os.O_WRONLY))\n'
        'print(os.getuid())\n'
    )

    uid = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, code).strip()

    assert not glob.glob(f"/tmp/left-{uid}*") + glob.glob(f"/var/tmp/left-{uid}*") + glob.glob(f"/dev/shm/left-{uid}*")


@pytest.mark.parametrize(("realUid", "named"), ((0, "0"), (0, "20007"), (20008, "20007")))
def test_reaperNeverSignalsAsAnyOtherUid(monkeypatch, realUid, named):
    kill = MagicMock()
    monkeypatch.setattr(sandbox_reaper.os, "getuid", lambda: realUid, raising=False)
    monkeypatch.setattr(sandbox_reaper.os, "kill", kill)
    monkeypatch.setattr(sandbox_reaper.sys, "argv", ["sandbox_reaper.py", named])

    with pytest.raises(SystemExit) as refused:
        sandbox_reaper.main()

    assert refused.value.code
    kill.assert_not_called()


def test_logFileNeverRecordsLocalVariables(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        'from utils.logger import logger\n'
        'def fail():\n'
        '    localSecret = "local-" + "variable-value"\n'
        '    raise ValueError(len(localSecret))\n'
        'try:\n'
        '    fail()\n'
        'except ValueError:\n'
        '    logger.exception("logged")\n'
    )

    (tmp_path / "failing.py").write_text(script)

    subprocess.run([sys.executable, "failing.py"], cwd=tmp_path, env={**os.environ, "PYTHONPATH": root}, check=True, capture_output=True)

    written = (tmp_path / "logs" / "runLogs.log").read_text()
    assert "logged" in written
    assert "local-variable-value" not in written


@separateUids
def test_sandboxCannotReadTheApplicationLogs(runInSandbox):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    output = runInSandbox(f'outcome = pd.io.common.os.listdir("{root}/logs")')

    assert "PermissionError" in output


@separateUids
def test_sandboxHasHardLimits(runInSandbox):
    code = (
        'resource = pd.io.common.os.sys.modules["resource"]\n'
        'limits = {name: resource.getrlimit(getattr(resource, name)) for name in ("RLIMIT_NPROC", "RLIMIT_FSIZE", "RLIMIT_CORE", "RLIMIT_NOFILE")}\n'
        'outcome = pd.io.common.os.sys.modules["json"].dumps(limits)'
    )

    limits = json.loads(runInSandbox(code).strip())

    assert limits["RLIMIT_NPROC"] == [sandboxRunner._MAX_RUN_PROCESSES] * 2
    assert limits["RLIMIT_FSIZE"] == [sandboxRunner._MAX_CHILD_FILE_BYTES] * 2
    assert limits["RLIMIT_CORE"] == [0, 0]
    assert limits["RLIMIT_NOFILE"] == [sandboxRunner._MAX_RUN_OPEN_FILES] * 2


@separateUids
def test_sandboxCantRaiseItsLimits(runInSandbox):
    code = (
        'resource = pd.io.common.os.sys.modules["resource"]\n'
        f'resource.setrlimit(resource.RLIMIT_NPROC, ({sandboxRunner._MAX_RUN_PROCESSES + 1},) * 2)\n'
        'outcome = "raised"'
    )

    assert "ValueError" in runInSandbox(code)


@separateUids
def test_sandboxCantWriteAFilePastTheLimit(runInSandbox):
    code = (
        'os = pd.io.common.os\n'
        'fd = os.open("big", os.O_CREAT | os.O_WRONLY)\n'
        f'os.lseek(fd, {sandboxRunner._MAX_CHILD_FILE_BYTES}, os.SEEK_SET)\n'
        'outcome = os.write(fd, b"x")'
    )

    assert "File too large" in runInSandbox(code)


@separateUids
def test_sandboxCantGainPrivileges(runInSandbox):
    code = (
        'os = pd.io.common.os\n'
        'fd = os.open("/proc/self/status", os.O_RDONLY)\n'
        'status = os.read(fd, 65536).decode()\n'
        'outcome = [line.split()[1] for line in status.splitlines() if line.startswith("NoNewPrivs")][0]'
    )

    assert runInSandbox(code).strip() == "1"


@separateUids
@inTheImage
def test_imageHasNoSetuidOrSetgidFiles():
    found = subprocess.run(["find", "/", "-xdev", "-perm", "/6000", "-type", "f"], capture_output=True, text=True)

    assert found.stdout.split() == []


@separateUids
def test_launcherDiesWithTheWorkerThatStartedIt(storage):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    handoff = _worldWritableFolder()
    before = _runFolders()
    code = (
        'import time\n'
        'os = pd.io.common.os\n'
        f'fd = os.open("{handoff}/pid.tmp", os.O_CREAT | os.O_WRONLY)\n'
        'os.write(fd, str(os.getpid()).encode())\n'
        'os.close(fd)\n'
        f'os.rename("{handoff}/pid.tmp", "{handoff}/pid")\n'
        'time.sleep(120)\n'
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", f"from utils.sandboxRunner import runSandbox\nrunSandbox('inspect_launcher.py', {PROJECT!r}, {code!r}, 90)"],
        cwd=root,
    )
    try:
        deadline = time.time() + 30
        while not os.path.exists(f"{handoff}/pid") and time.time() < deadline:
            time.sleep(0.05)
        with open(f"{handoff}/pid") as source:
            launcher = source.read()

        worker.kill()
        worker.wait()

        deadline = time.time() + 5
        while launcher in _liveRunUidProcesses() and time.time() < deadline:
            time.sleep(0.05)
        assert launcher not in _liveRunUidProcesses()
    finally:
        worker.kill()
        for folder in _runFolders() - before:
            shutil.rmtree(folder, ignore_errors=True)


@separateUids
def test_sandboxRunsAsAnotherUidWithNoGroups(runInSandbox):
    output = runInSandbox('os = pd.io.common.os\noutcome = [os.getuid(), os.getgid(), os.getgroups()]')

    uid, gid, groups = json.loads(output.strip())
    assert uid != 0
    assert gid != 0
    assert groups == []


@separateUids
def test_sandboxCannotReadTheParentEnvironmentFromProc(runInSandbox):
    code = (
        'os = pd.io.common.os\n'
        'fd = os.open(f"/proc/{os.getppid()}/environ", os.O_RDONLY)\n'
        'outcome = os.read(fd, 1 << 20)'
    )

    output = runInSandbox(code)

    assert "PermissionError" in output
    assert SERVICE_KEY not in output


@separateUids
def test_sandboxCannotWriteToTheApplication(runInSandbox):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        'os = pd.io.common.os\n'
        f'outcome = os.open("{root}/planted.py", os.O_CREAT | os.O_WRONLY)'
    )

    output = runInSandbox(code)

    assert "PermissionError" in output
    assert not os.path.exists(os.path.join(root, "planted.py"))


@separateUids
def test_concurrentRunsCannotReachEachOther(storage):
    handoff = os.path.join(tempfile.gettempdir(), f"handoff-{uuid.uuid4().hex}")
    os.mkdir(handoff)
    os.chmod(handoff, 0o777)
    executor = TransformationExecutor(timeoutSeconds=60)
    first = (
        'import time\n'
        'os = pd.io.common.os\n'
        'fetch_data_pl(projectId, "sales")\n'
        'tables = os.path.join(os.path.dirname(os.getcwd()), "tables")\n'
        f'fd = os.open("{handoff}/paths.tmp", os.O_CREAT | os.O_WRONLY)\n'
        'os.write(fd, f"{tables} {os.getcwd()} /proc/{os.getpid()}/fd".encode())\n'
        'os.close(fd)\n'
        f'os.rename("{handoff}/paths.tmp", "{handoff}/paths")\n'
        f'while not os.path.exists("{handoff}/done"):\n'
        '    time.sleep(0.1)\n'
    )
    second = (
        'import time\n'
        'os = pd.io.common.os\n'
        f'while not os.path.exists("{handoff}/paths"):\n'
        '    time.sleep(0.1)\n'
        f'fd = os.open("{handoff}/paths", os.O_RDONLY)\n'
        'outcomes = []\n'
        'for target in os.read(fd, 4096).decode().split():\n'
        '    try:\n'
        '        outcomes.append(os.listdir(target))\n'
        '    except PermissionError:\n'
        '        outcomes.append("denied")\n'
        f'os.close(os.open("{handoff}/done", os.O_CREAT | os.O_WRONLY))\n'
        'print(outcomes)\n'
    )
    outputs = {}
    firstRun = threading.Thread(target=lambda: outputs.update(first=executor.executeInspection(PROJECT, first)))
    firstRun.start()

    outputs["second"] = executor.executeInspection(OTHER_PROJECT, second)
    firstRun.join()

    assert outputs["second"].strip() == "['denied', 'denied', 'denied']"


@pytest.mark.skipif(os.name == "posix" and os.geteuid() == 0, reason="a root parent refuses either way")
def test_sameUidRunsNeedAnExplicitOptIn(storage, monkeypatch):
    monkeypatch.delenv("SANDBOX_RUN_ROOT", raising=False)
    monkeypatch.delenv("SANDBOX_ALLOW_SAME_UID", raising=False)
    started = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", started)

    with pytest.raises(RuntimeError, match="SANDBOX_ALLOW_SAME_UID"):
        TransformationExecutor(timeoutSeconds=20).executeInspection(PROJECT, 'print("ran")')

    started.assert_not_called()


@separateUids
def test_rootParentRefusesToRunWithoutARunRoot(storage, monkeypatch):
    monkeypatch.delenv("SANDBOX_RUN_ROOT", raising=False)
    monkeypatch.setenv("SANDBOX_ALLOW_SAME_UID", "1")
    started = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", started)

    with pytest.raises(RuntimeError, match="SANDBOX_RUN_ROOT"):
        TransformationExecutor(timeoutSeconds=20).executeInspection(PROJECT, 'print("ran")')

    started.assert_not_called()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a parent that can't change uid")
def test_parentThatCantChangeUidRefusesToRunWhenTheImageRequiresIt(storage, monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_RUN_ROOT", str(tmp_path))
    started = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", started)

    with pytest.raises(PermissionError):
        TransformationExecutor(timeoutSeconds=20).executeInspection(PROJECT, 'print("ran")')

    started.assert_not_called()
