"""Unit tests for run payloads, the SQL run kind and the per-project slot (the child and the sandbox are stubbed)."""

import io
import threading
from unittest.mock import MagicMock

import pytest

from utils import codeExecutor, sandboxRunner
from utils.sandboxLimits import currentRunLimits
from utils.sandboxRunner import SandboxResult
from utils.sandboxSlots import SandboxBusyError

OK = SandboxResult("", "", 0, b"x", None, None)


def _stubFreshRun(monkeypatch):
    """Replace the slot, uid and folder handling with a pass-through; return the kinds `_runChild` received."""
    kinds = []

    def runChild(launcherName, projectId, code, timeoutSeconds, withResult, runUid, runFolder, tablesFolder, workFolder, limits, kind=sandboxRunner._RunKind()):
        kinds.append((launcherName, withResult, kind))
        return OK

    def inFreshRun(prepare, run, slotWaitSeconds=None):
        return run(None, "run", "tables", "work", currentRunLimits())

    monkeypatch.setattr(sandboxRunner, "_runChild", runChild)
    monkeypatch.setattr(sandboxRunner, "_inFreshRun", inFreshRun)
    return kinds


def test_runSandboxPassesPayloadToTheChild(monkeypatch):
    kinds = _stubFreshRun(monkeypatch)
    sandboxRunner.runSandbox("sandbox_launcher.py", "p1", "print(1)", 5, payload={"filters": [1]})
    sandboxRunner.runSandbox("sandbox_launcher.py", "p1", "print(1)", 5)
    assert kinds[0][2].payload == {"filters": [1]}
    assert kinds[1][2].payload is None


def test_runSqlSandboxUsesTheSqlRunKind(monkeypatch):
    kinds = _stubFreshRun(monkeypatch)
    result = sandboxRunner.runSqlSandbox("p1", {"sql": "select 1"}, 9)
    assert result is OK
    launcherName, withResult, kind = kinds[0]
    assert launcherName == "sql_launcher.py"
    assert withResult is True
    assert kind.resultName == "result.arrow"
    assert kind.resultCap == sandboxRunner.SQL_RESULT_CAP_BYTES == 32 * 1024 * 1024
    assert kind.previewName is None
    assert kind.servesTables is True
    assert kind.payload == {"request": {"sql": "select 1"}}


def test_runSqlSandboxPassesTheSlotWait(monkeypatch):
    waits = []
    monkeypatch.setattr(sandboxRunner, "_inFreshRun", lambda prepare, run, slotWaitSeconds=None: waits.append(slotWaitSeconds) or OK)
    sandboxRunner.runSqlSandbox("p1", {}, 9, slotWaitSeconds=0)
    sandboxRunner.runSqlSandbox("p1", {}, 9)
    assert waits == [0, None]


def test_slotWaitOverride(monkeypatch):
    waits = []
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda root, maxRuns, wait: waits.append(wait))
    monkeypatch.setattr(sandboxRunner, "releaseRunSlot", lambda slot: None)
    monkeypatch.setattr(sandboxRunner, "_claimRun", MagicMock(side_effect=SandboxBusyError("stop here")))
    for wait in (0, None):
        with pytest.raises(SandboxBusyError):
            sandboxRunner._inFreshRun(lambda uid, folder: None, lambda *args: OK, slotWaitSeconds=wait)
    assert waits == [0, currentRunLimits().slotWaitSeconds]


class _FakeRedis:
    def __init__(self, count=0):
        self.count = count
        self.zcardCalls = 0
        self.zrem = MagicMock()

    def zremrangebyscore(self, *args):
        pass

    def zcard(self, key):
        self.zcardCalls += 1
        return self.count

    def zadd(self, *args):
        pass

    def expire(self, *args):
        pass


def _manager(monkeypatch, redis):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: redis)
    return codeExecutor.REPLManager(timeoutSeconds=5)


def test_replRunPassesFiltersOnlyWhenGiven(monkeypatch):
    calls = []
    monkeypatch.setattr(codeExecutor, "runSandbox", lambda *args, **kwargs: calls.append((args, kwargs)) or ("ok", "", 0, None, None, None))
    manager = _manager(monkeypatch, _FakeRedis())
    manager.run("print(1)", "p1")
    manager.run("print(1)", "p1", filters=[{"a": 1}])
    assert "payload" not in calls[0][1]
    assert calls[1][1] == {"payload": {"filters": [{"a": 1}]}}


def test_runSqlWithoutWaitTriesTheSemaphoreOnce(monkeypatch):
    redis = _FakeRedis(count=50)
    manager = _manager(monkeypatch, redis)
    sql = MagicMock()
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", sql)
    with pytest.raises(SandboxBusyError):
        manager.runSql({"sql": "x"}, "p1", waitForSlot=False)
    assert redis.zcardCalls == 1
    sql.assert_not_called()


def test_runSqlWithoutWaitAsksForNoSlotWait(monkeypatch):
    manager = _manager(monkeypatch, _FakeRedis())
    sql = MagicMock(return_value=OK)
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", sql)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    assert sql.call_args.kwargs["slotWaitSeconds"] == 0
    manager.runSql({"sql": "x"}, "p1")
    assert sql.call_args.kwargs.get("slotWaitSeconds") is None


def test_runSqlReleasesTheSemaphore(monkeypatch):
    redis = _FakeRedis()
    manager = _manager(monkeypatch, redis)
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", lambda *args, **kwargs: OK)
    manager.runSql({}, "p1")
    assert redis.zrem.call_count == 1

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(codeExecutor, "runSqlSandbox", boom)
    with pytest.raises(RuntimeError):
        manager.runSql({}, "p1")
    assert redis.zrem.call_count == 2


def _tablePathAsking(monkeypatch, tmp_path, reply: bytes):
    """`tablePath` wired to a parent that answers with `reply`; returns what the child sent."""
    requests, replies = io.BytesIO(), io.BytesIO(reply)
    lock = threading.Lock()

    def ask(tableName):
        return sandboxRunner._askForPath(requests, replies, lock, str(tmp_path), tableName)

    monkeypatch.setattr(sandboxRunner, "_pathSource", [ask])
    return requests


def test_tablePathReturnsTheSpooledFilesPath(monkeypatch, tmp_path):
    requests = _tablePathAsking(monkeypatch, tmp_path, b'{"file": "0.parquet"}\n')

    assert sandboxRunner.tablePath("sales") == str(tmp_path / "0.parquet")
    assert requests.getvalue() == b'"sales"\n'


def test_tablePathRaisesTheParentsErrorReply(monkeypatch, tmp_path):
    _tablePathAsking(monkeypatch, tmp_path, b'{"error": "Could not read table \'sales\'."}\n')

    with pytest.raises(RuntimeError, match="Could not read table 'sales'"):
        sandboxRunner.tablePath("sales")


def test_tablePathRefusesToRunOutsideASandbox(monkeypatch):
    monkeypatch.setattr(sandboxRunner, "_pathSource", [None])

    with pytest.raises(RuntimeError, match="only available inside a sandbox"):
        sandboxRunner.tablePath("sales")
