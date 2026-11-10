"""A shadow SQL run leaves headroom: it starts only while the sandbox has slots to spare and the project is idle."""

import os
import sys
import types
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
