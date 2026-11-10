"""A shadow SQL run leaves headroom: it starts only while the sandbox has slots to spare and the project is idle."""

import io
import os
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from utils import codeExecutor, sandboxRunner, sandboxSlots
from utils.sandboxSlots import SandboxBusyError, acquireRunSlot, freeRunSlots, releaseRunSlot, requireHeadroom

OK = sandboxRunner.SandboxResult("", "", 0, b"x", None, None)


@pytest.fixture
def flock(monkeypatch):
    """Real `fcntl.flock` where there is one; on Windows a stand-in with flock's rules (one holder per file, by open handle)."""
    if sys.platform != "win32":
        return
    locks = {}

    def keyOf(fd):
        info = os.fstat(fd)
        return (info.st_dev, info.st_ino)

    def flockFake(fd, operation):
        key = keyOf(fd)
        holder = locks.get(key)
        if holder is not None and holder != fd:
            try:
                stale = keyOf(holder) != key
            except OSError:
                stale = True
            if stale:
                holder = None
        if operation & 8:  # LOCK_UN
            if holder == fd:
                del locks[key]
            return
        if holder is not None and holder != fd:
            raise BlockingIOError()
        locks[key] = fd

    fake = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=flockFake)
    monkeypatch.setitem(sys.modules, "fcntl", fake)
    monkeypatch.setattr(os, "O_CLOEXEC", 0, raising=False)


def _hold(root, maxRuns, count):
    return [acquireRunSlot(str(root), maxRuns, 0) for _ in range(count)]


def _release(fds):
    for fd in fds:
        releaseRunSlot(fd)


def test_freeRunSlotsCountsAndLeavesEverySlotUnlocked(flock, tmp_path):
    assert freeRunSlots(str(tmp_path), 4) == 4
    held = _hold(tmp_path, 4, 3)
    try:
        assert freeRunSlots(str(tmp_path), 4) == 1
        assert freeRunSlots(str(tmp_path), 4) == 1  # counting took nothing
    finally:
        _release(held)
    assert freeRunSlots(str(tmp_path), 4) == 4
    again = _hold(tmp_path, 4, 4)  # every slot can still be taken
    _release(again)


def test_freeRunSlotsIsNoneWhereTheCapIsOff(tmp_path):
    assert freeRunSlots(None, 4) is None
    assert freeRunSlots("", 4) is None
    assert freeRunSlots(str(tmp_path), 0) is None


@pytest.mark.parametrize("maxRuns, held, ok", [(4, 2, True), (4, 3, False), (12, 6, True), (12, 7, False), (3, 2, False), (1, 0, False)])
def test_requireHeadroomWantsTheLargerOfTwoAndHalf(flock, tmp_path, maxRuns, held, ok):
    fds = _hold(tmp_path, maxRuns, held)
    try:
        if ok:
            requireHeadroom(str(tmp_path), maxRuns)
        else:
            with pytest.raises(SandboxBusyError):
                requireHeadroom(str(tmp_path), maxRuns)
    finally:
        _release(fds)


def test_requireHeadroomChecksNothingWhereTheCapIsOff(tmp_path):
    requireHeadroom(None, 12)
    requireHeadroom(str(tmp_path), 0)


def _runWith(monkeypatch, tmp_path, maxRuns):
    monkeypatch.setenv("SANDBOX_RUN_ROOT", str(tmp_path))
    monkeypatch.setenv("SANDBOX_MAX_CONCURRENT_RUNS", str(maxRuns))
    children = []

    def runChild(*args, **kwargs):
        children.append(args)
        return OK

    monkeypatch.setattr(sandboxRunner, "_runChild", runChild)
    monkeypatch.setattr(sandboxRunner, "_claimRun", lambda: (None, str(tmp_path), None))
    monkeypatch.setattr(sandboxRunner, "_clearUid", lambda *a: True)
    monkeypatch.setattr(sandboxRunner, "_prepareFolders", lambda *a: ("tables", "work"))
    monkeypatch.setattr(sandboxRunner, "_removeRun", lambda *a: None)
    return children


def test_shadowSqlRunWithTooFewFreeSlotsNeverStartsAChild(flock, monkeypatch, tmp_path):
    children = _runWith(monkeypatch, tmp_path, 4)
    held = _hold(tmp_path, 4, 3)
    try:
        with pytest.raises(SandboxBusyError):
            sandboxRunner.runSqlSandbox("p1", {"sql": "x"}, 9, slotWaitSeconds=0, requireHeadroom=True)
        assert children == []
    finally:
        _release(held)
    assert freeRunSlots(str(tmp_path), 4) == 4


def test_shadowSqlRunWithRoomStartsAChild(flock, monkeypatch, tmp_path):
    children = _runWith(monkeypatch, tmp_path, 4)
    held = _hold(tmp_path, 4, 2)
    try:
        assert sandboxRunner.runSqlSandbox("p1", {"sql": "x"}, 9, slotWaitSeconds=0, requireHeadroom=True) is OK
        assert len(children) == 1
    finally:
        _release(held)


def test_servedSqlRunIsUnchangedWhenSlotsAreScarce(flock, monkeypatch, tmp_path):
    children = _runWith(monkeypatch, tmp_path, 4)
    held = _hold(tmp_path, 4, 3)  # one free: too few for a shadow, enough for a served run
    try:
        assert sandboxRunner.runSqlSandbox("p1", {"sql": "x"}, 9) is OK
        assert len(children) == 1
    finally:
        _release(held)


class _Redis:
    def __init__(self, count=0):
        self.count = count
        self.zrem = MagicMock()

    def zremrangebyscore(self, *args):
        pass

    def zcard(self, key):
        return self.count

    def zadd(self, *args):
        pass

    def expire(self, *args):
        pass


def _manager(monkeypatch, redis):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: redis)
    return codeExecutor.REPLManager(timeoutSeconds=5)


def test_shadowWhileTheProjectHasARunningTaskIsBusyAndStartsNothing(monkeypatch):
    manager = _manager(monkeypatch, _Redis(count=1))
    sql = MagicMock(return_value=OK)
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", sql)
    with pytest.raises(SandboxBusyError):
        manager.runSql({"sql": "x"}, "p1", waitForSlot=False)
    sql.assert_not_called()


def test_shadowOnAnIdleProjectAsksForHeadroom(monkeypatch):
    manager = _manager(monkeypatch, _Redis(count=0))
    sql = MagicMock(return_value=OK)
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", sql)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    assert sql.call_args.kwargs["requireHeadroom"] is True and sql.call_args.kwargs["slotWaitSeconds"] == 0


def test_servedRunIsUnchangedWhileTheProjectHasARunningTask(monkeypatch):
    manager = _manager(monkeypatch, _Redis(count=3))
    sql = MagicMock(return_value=OK)
    monkeypatch.setattr(codeExecutor, "runSqlSandbox", sql)
    assert manager.runSql({"sql": "x"}, "p1") is OK
    assert "requireHeadroom" not in sql.call_args.kwargs


# The shadow's grounding read: the reader child takes a slot like a SQL run does, so it leaves headroom too.


@pytest.fixture
def readerRun(flock, monkeypatch, tmp_path):
    from utils import storedTables

    children = _runWith(monkeypatch, tmp_path, 4)
    monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", str(256 * 1024 * 1024))
    monkeypatch.setattr(sandboxRunner, "_writeInput", lambda *a: "input")
    monkeypatch.setattr(storedTables, "_ipcTable", lambda projectId, tableName, answer: "table")
    return children


def _readTable(**kwargs):
    from utils import storedTables

    return storedTables.readUntrustedTable("p1", "t", b"x", **kwargs)


def test_shadowGroundingReadWithTooFewFreeSlotsIsBusyAndStartsNoReader(readerRun, tmp_path):
    held = _hold(tmp_path, 4, 3)
    try:
        with pytest.raises(SandboxBusyError):
            _readTable(leaveHeadroom=True)
        assert readerRun == []
    finally:
        _release(held)
    assert freeRunSlots(str(tmp_path), 4) == 4


def test_shadowGroundingReadWithRoomProceeds(readerRun, tmp_path):
    held = _hold(tmp_path, 4, 2)
    try:
        assert _readTable(leaveHeadroom=True) == "table"
        assert len(readerRun) == 1
    finally:
        _release(held)


def test_servedGroundingReadWaitsAsBefore(readerRun, monkeypatch, tmp_path):
    waits = []
    real = sandboxRunner.acquireRunSlot
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda root, maxRuns, wait: waits.append(wait) or real(root, maxRuns, 0))
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "7")
    held = _hold(tmp_path, 4, 3)  # one free: too few for a shadow, enough for a served read
    try:
        assert _readTable() == "table"
        assert waits == [7]
        assert len(readerRun) == 1
    finally:
        _release(held)


def test_shadowGroundingReadTakesItsSlotWithoutWaiting(readerRun, monkeypatch):
    waits = []
    real = sandboxRunner.acquireRunSlot
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda root, maxRuns, wait: waits.append(wait) or real(root, maxRuns, 0))
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "7")
    _readTable(leaveHeadroom=True)
    assert waits == [0]


def _unmarkedTableInStorage(monkeypatch):
    """Storage holds a table with no marker, so its schema is a reader-child read; the child's answer reads as "schema"."""
    from utils import projectStorage, storedTables

    @contextmanager
    def openTableFile(projectId, tableName):
        yield io.BytesIO(b"x")

    monkeypatch.setattr(projectStorage, "openTableFile", openTableFile)
    monkeypatch.setattr(projectStorage, "readTableBytes", lambda p, t: b"x")
    monkeypatch.setattr(storedTables, "_ipcSchema", lambda projectId, tableName, answer: "schema")


def _readSchema(**kwargs):
    from utils import storedTables

    return storedTables.storedTableSchema("p1", "t", **kwargs)


def test_shadowSchemaReadOfAnUnmarkedTableWithTooFewFreeSlotsIsBusyAndStartsNoReader(readerRun, monkeypatch, tmp_path):
    _unmarkedTableInStorage(monkeypatch)
    held = _hold(tmp_path, 4, 3)
    try:
        with pytest.raises(SandboxBusyError):
            _readSchema(leaveHeadroom=True)
        assert readerRun == []
    finally:
        _release(held)
    assert freeRunSlots(str(tmp_path), 4) == 4


def test_shadowSchemaReadWithRoomTakesItsSlotWithoutWaiting(readerRun, monkeypatch, tmp_path):
    _unmarkedTableInStorage(monkeypatch)
    waits = []
    real = sandboxRunner.acquireRunSlot
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda root, maxRuns, wait: waits.append(wait) or real(root, maxRuns, 0))
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "7")
    held = _hold(tmp_path, 4, 2)
    try:
        assert _readSchema(leaveHeadroom=True) == "schema"
        assert waits == [0]
        assert len(readerRun) == 1
    finally:
        _release(held)


def test_servedSchemaReadWaitsAsBefore(readerRun, monkeypatch, tmp_path):
    _unmarkedTableInStorage(monkeypatch)
    waits = []
    real = sandboxRunner.acquireRunSlot
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda root, maxRuns, wait: waits.append(wait) or real(root, maxRuns, 0))
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "7")
    held = _hold(tmp_path, 4, 3)  # one free: too few for a shadow, enough for a served read
    try:
        assert _readSchema() == "schema"
        assert waits == [7]
        assert len(readerRun) == 1
    finally:
        _release(held)


def test_shadowTableSchemasLetBusyPropagateEvenWhenSkippingUnreadable(readerRun, monkeypatch, tmp_path):
    from utils import tableSchemas

    _unmarkedTableInStorage(monkeypatch)
    monkeypatch.setattr(tableSchemas, "_client", lambda: (_ for _ in ()).throw(RuntimeError("no redis")))
    held = _hold(tmp_path, 4, 3)
    try:
        with pytest.raises(SandboxBusyError):
            tableSchemas.tableSchemas("p1", ["t"], skipUnreadable=True, leaveHeadroom=True)
        assert readerRun == []
    finally:
        _release(held)


@pytest.fixture
def warnings(monkeypatch):
    seen = []
    monkeypatch.setattr(sandboxSlots, "_shadowDisabledWarned", False)
    monkeypatch.setattr(sandboxSlots.logger, "warning", lambda message, *a, **k: seen.append(str(message)))
    return seen


def test_aCapBelowTwoRefusesEveryShadowAndWarnsOncePerProcess(flock, tmp_path, warnings):
    for _ in range(3):
        with pytest.raises(SandboxBusyError):
            requireHeadroom(str(tmp_path), 1)
    assert warnings == ['chart_shadow_disabled {"reason": "sandbox cap below 2"}']


def test_aBusySandboxWithACapOfTwoOrMoreDoesNotWarn(flock, tmp_path, warnings):
    held = _hold(tmp_path, 2, 1)
    try:
        with pytest.raises(SandboxBusyError):
            requireHeadroom(str(tmp_path), 2)
    finally:
        _release(held)
    requireHeadroom(str(tmp_path), 2)
    requireHeadroom(None, 1)  # no cap here: nothing to say
    assert warnings == []


# The project-idle check against a Redis that keeps real sorted sets (fakeredis is not a dependency here).


class ZsetRedis:
    """The sorted-set commands the project semaphore uses, with Redis's semantics. `down` makes every command fail."""

    def __init__(self, down: bool = False):
        self.sets: dict[str, dict[str, float]] = {}
        self.down = down

    def _check(self):
        if self.down:
            raise ConnectionError("Redis is down")

    def zadd(self, key, mapping):
        self._check()
        members = self.sets.setdefault(key, {})
        added = sum(member not in members for member in mapping)
        members.update({member: float(score) for member, score in mapping.items()})
        return added

    def zrem(self, key, *members):
        self._check()
        found = self.sets.get(key, {})
        removed = sum(found.pop(member, None) is not None for member in members)
        if key in self.sets and not found:
            del self.sets[key]
        return removed

    def zcard(self, key):
        self._check()
        return len(self.sets.get(key, {}))

    def zremrangebyscore(self, key, minimum, maximum):
        self._check()
        found = self.sets.get(key, {})
        gone = [member for member, score in found.items() if float(minimum) <= score <= float(maximum)]
        for member in gone:
            del found[member]
        if key in self.sets and not found:
            del self.sets[key]
        return len(gone)

    def expire(self, key, seconds):
        self._check()
        return key in self.sets


def _sqlSpy(monkeypatch, redis, key="semaphore:p1"):
    calls = []

    def run(**kwargs):
        calls.append((kwargs, None if redis.down else redis.zcard(key)))
        return OK

    monkeypatch.setattr(codeExecutor, "runSqlSandbox", run)
    return calls


def test_aShadowOnAnIdleProjectRunsAndGivesItsEntryBack(monkeypatch):
    redis = ZsetRedis()
    manager = _manager(monkeypatch, redis)
    calls = _sqlSpy(monkeypatch, redis)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    [(kwargs, entriesDuringTheRun)] = calls
    assert kwargs["requireHeadroom"] is True and kwargs["slotWaitSeconds"] == 0
    assert entriesDuringTheRun == 1
    assert redis.zcard("semaphore:p1") == 0


def test_aShadowOnABusyProjectIsRefusedAndLeavesTheOtherEntry(monkeypatch):
    import time

    redis = ZsetRedis()
    redis.zadd("semaphore:p1", {"a-served-run": time.time()})
    manager = _manager(monkeypatch, redis)
    calls = _sqlSpy(monkeypatch, redis)
    with pytest.raises(SandboxBusyError):
        manager.runSql({"sql": "x"}, "p1", waitForSlot=False)
    assert calls == []
    assert redis.sets["semaphore:p1"].keys() == {"a-served-run"}
    # A served run of the same project still runs beside it, as before.
    assert manager.runSql({"sql": "x"}, "p1") is OK
    assert calls[0][1] == 2 and "requireHeadroom" not in calls[0][0]
    assert redis.sets["semaphore:p1"].keys() == {"a-served-run"}


def test_aStaleEntryDoesNotKeepAProjectBusy(monkeypatch):
    redis = ZsetRedis()
    redis.zadd("semaphore:p1", {"a-run-that-died": 0.0})
    manager = _manager(monkeypatch, redis)
    calls = _sqlSpy(monkeypatch, redis)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    assert calls[0][1] == 1 and redis.zcard("semaphore:p1") == 0


def test_aRunningShadowMakesASecondShadowOfTheProjectBusy(monkeypatch):
    redis = ZsetRedis()
    manager = _manager(monkeypatch, redis)
    nested = []

    def run(projectId, **kwargs):
        if projectId == "p1":
            with pytest.raises(SandboxBusyError):
                manager.runSql({"sql": "y"}, "p1", waitForSlot=False)
            nested.append(manager.runSql({"sql": "z"}, "p2", waitForSlot=False))  # another project is idle
        return OK

    monkeypatch.setattr(codeExecutor, "runSqlSandbox", run)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    assert nested == [OK]
    assert redis.sets == {}


def test_withRedisDownAShadowRunsAsAServedRunDoes(monkeypatch):
    redis = ZsetRedis(down=True)
    manager = _manager(monkeypatch, redis)
    calls = _sqlSpy(monkeypatch, redis)
    assert manager.runSql({"sql": "x"}, "p1", waitForSlot=False) is OK
    assert manager.runSql({"sql": "x"}, "p1") is OK
    assert len(calls) == 2
    assert calls[0][0]["requireHeadroom"] is True and "requireHeadroom" not in calls[1][0]
