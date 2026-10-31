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
import subprocess
import tempfile
import threading
import uuid
from unittest.mock import MagicMock

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nubrix.components.transformationExecutor import TransformationExecutor
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import codeExecutor, initMethods

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


@pytest.fixture(autouse=True)
def _windowsSocketsNeedSystemRoot(monkeypatch):
    """Winsock can't load without SYSTEMROOT, which the Linux-only allowlists leave out."""
    if os.name != "nt":
        return
    realPopen = subprocess.Popen

    def popen(*args, env=None, **kwargs):
        return realPopen(*args, env={**env, "SYSTEMROOT": os.environ["SYSTEMROOT"]}, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


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

    serveTables(PROJECT, io.BytesIO(request + b'\n"sales"\n'), replies, str(tmp_path))

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert "error" in first
    assert (tmp_path / second["file"]).exists()
    _assertOtherProjectUntouched(storage)


def test_parentSpoolsATableOnceInARun(storage, tmp_path):
    from utils.sandboxRunner import serveTables

    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"sales"\n"sales"\n'), replies, str(tmp_path))

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert first == second
    assert len([r for r in storage.requests if r["method"] == "GET"]) == 1


def test_failedDownloadDoesNotBreakTheNextRead(storage, tmp_path, monkeypatch):
    from utils.sandboxRunner import serveTables

    realDownload = initMethods.downloadTable

    def failAfterAPartialWrite(projectId, tableName, path):
        if tableName == "broken":
            with open(path, "xb") as partial:
                partial.write(b"PAR1")
            raise OSError("connection dropped")
        realDownload(projectId, tableName, path)

    monkeypatch.setattr(initMethods, "downloadTable", failAfterAPartialWrite)
    replies = io.BytesIO()

    serveTables(PROJECT, io.BytesIO(b'"broken"\n"sales"\n'), replies, str(tmp_path))

    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert first == {"error": "Could not read table 'broken'."}
    assert pl.read_parquet(tmp_path / second["file"]).height == 3


def test_parentCachesASmallTableInRedisAfterAColdRead(storage, tmp_path, monkeypatch):
    stored = {}
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda key, table: stored.update({key: table.num_rows}))

    initMethods.spool_table(PROJECT, "sales", str(tmp_path / "0"))

    assert stored == {f"{PROJECT}::sales": 3}


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


def test_childThatFloodsTheChannelCantHangTheParent(storage):
    code = (
        'source = pd.io.common.os.sys.modules["utils.initMethods"]._TABLE_SOURCE\n'
        'pipes = [c.cell_contents for c in source.__closure__ if hasattr(c.cell_contents, "writable")]\n'
        'requests = [pipe for pipe in pipes if pipe.writable()][0]\n'
        'for _ in range(100000):\n'
        '    requests.write(b\'"sales"\\n\')\n'
        '    requests.flush()\n'
    )
    executor = TransformationExecutor(timeoutSeconds=15)
    outputs = []
    run = threading.Thread(target=lambda: outputs.append(executor.executeInspection(PROJECT, code)), daemon=True)

    run.start()
    run.join(timeout=60)

    assert not run.is_alive()
    assert "timed out" in outputs[0]


def _runFolders():
    root = os.environ.get("SANDBOX_RUN_ROOT") or tempfile.gettempdir()
    return set(glob.glob(os.path.join(root, "sandbox-*")))


@pytest.mark.parametrize("code", (
    'fetch_data_pl(projectId, "sales")\nprint("done")',
    'raise RuntimeError("failed on purpose")',
    'import time\ntime.sleep(120)',
))
def test_runLeavesNoFolderBehind(storage, code):
    executor = TransformationExecutor(timeoutSeconds=20)
    before = _runFolders()

    executor.executeInspection(PROJECT, code)

    assert _runFolders() == before


def test_transformationResultStillComesBack(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    rows, parquetBytes = executor.executeAndPreview(PROJECT, 'final_df = fetch_data_pl(projectId, "sales")', "copy")

    assert rows == [{"amount": 1}, {"amount": 2}, {"amount": 3}]
    assert pl.read_parquet(io.BytesIO(parquetBytes)).height == 3


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


@separateUids
def test_rootParentRefusesToRunWithoutARunRoot(storage, monkeypatch):
    monkeypatch.delenv("SANDBOX_RUN_ROOT", raising=False)
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
