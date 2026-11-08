"""Run limits are read from the environment with safe fallbacks."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from nubrix.components.transformationExecutor import TransformationExecutor
from test.test_sandbox_storage_reads import PROJECT, _liveRunUidProcesses, separateUids, storage  # noqa: F401  (fixture)
from utils import sandboxRunner, sandboxSlots
from utils.sandboxLimits import (
    RunLimits,
    _formatBytes,
    currentRunLimits,
    defaultTimeoutSeconds,
)
from utils.sandboxSlots import SandboxBusyError, acquireRunSlot, releaseRunSlot
from utils.sandboxWatchdog import RunWatchdog, workUsage

GIB = 1024**3

VARIABLES = {
    "SANDBOX_RUN_THREADS": "threads",
    "SANDBOX_MAX_RUN_PROCESSES": "maxProcesses",
    "SANDBOX_MAX_PROCESS_MEMORY_BYTES": "maxProcessMemory",
    "SANDBOX_MAX_RUN_MEMORY_BYTES": "maxRunMemory",
    "SANDBOX_OOM_SCORE_ADJ": "oomScoreAdj",
    "SANDBOX_MAX_CPU_SECONDS": "maxCpuSeconds",
    "SANDBOX_MAX_DISK_BYTES": "maxDiskBytes",
    "SANDBOX_MAX_FILES": "maxFiles",
    "SANDBOX_MAX_FILE_BYTES": "maxFileBytes",
    "SANDBOX_BLOCK_NETWORK": "blockNetwork",
    "SANDBOX_MAX_CONCURRENT_RUNS": "maxConcurrentRuns",
    "SANDBOX_SLOT_WAIT_SECONDS": "slotWaitSeconds",
}

DEFAULTS = {
    "threads": 4,
    "maxProcesses": 64,
    "maxProcessMemory": 4 * GIB,
    "maxRunMemory": 4 * GIB,
    "oomScoreAdj": 1000,
    "maxCpuSeconds": 1200,
    "maxDiskBytes": 2 * GIB,
    "maxFiles": 10000,
    "maxFileBytes": 2 * GIB,
    "blockNetwork": True,
    "maxConcurrentRuns": 12,
    "slotWaitSeconds": 120.0,
}


@pytest.fixture(autouse=True)
def cleanEnv(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name in ("TRANSFORMATION_TIMEOUT_SECONDS", "CODE_EXEC_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_defaultsMatchTheAgreedValues():
    limits = currentRunLimits()
    for field, expected in DEFAULTS.items():
        assert getattr(limits, field) == expected, field
    assert isinstance(limits.blockNetwork, bool)


def test_revertValuesRestoreTodaysLimits(monkeypatch):
    revert = {
        "SANDBOX_RUN_THREADS": "0",
        "SANDBOX_MAX_RUN_PROCESSES": "1024",
        "SANDBOX_MAX_PROCESS_MEMORY_BYTES": "0",
        "SANDBOX_MAX_RUN_MEMORY_BYTES": "0",
        "SANDBOX_OOM_SCORE_ADJ": "0",
        "SANDBOX_MAX_CPU_SECONDS": "0",
        "SANDBOX_MAX_DISK_BYTES": "0",
        "SANDBOX_MAX_FILES": "0",
        "SANDBOX_MAX_FILE_BYTES": "4294967296",
        "SANDBOX_BLOCK_NETWORK": "0",
        "SANDBOX_MAX_CONCURRENT_RUNS": "0",
    }
    for name, value in revert.items():
        monkeypatch.setenv(name, value)
    limits = currentRunLimits()
    assert limits.maxProcesses == 1024
    assert limits.maxFileBytes == 4294967296
    assert limits.blockNetwork is False
    for name, field in VARIABLES.items():
        if name in ("SANDBOX_MAX_RUN_PROCESSES", "SANDBOX_MAX_FILE_BYTES",
                    "SANDBOX_BLOCK_NETWORK", "SANDBOX_SLOT_WAIT_SECONDS"):
            continue
        assert getattr(limits, field) == 0, field


@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_badValueFallsBackToTheDefault(monkeypatch, bad):
    for name in VARIABLES:
        monkeypatch.setenv(name, bad)
    limits = currentRunLimits()
    for field, expected in DEFAULTS.items():
        assert getattr(limits, field) == expected, field


def test_fractionalValueIsAcceptedOnlyForTheSlotWait(monkeypatch):
    for name in VARIABLES:
        monkeypatch.setenv(name, "1.5")
    limits = currentRunLimits()
    assert limits.slotWaitSeconds == 1.5
    for field, expected in DEFAULTS.items():
        if field != "slotWaitSeconds":
            assert getattr(limits, field) == expected, field


def test_fractionalSlotWaitIsAccepted(monkeypatch):
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "0.5")
    assert currentRunLimits().slotWaitSeconds == 0.5


def test_overridesWinOverTheEnvironment(monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PROCESS_MEMORY_BYTES", "1")
    assert currentRunLimits(maxProcessMemory=GIB).maxProcessMemory == GIB


def test_timeoutDefaultsTo300AndZeroMeansNone(monkeypatch):
    assert defaultTimeoutSeconds("TRANSFORMATION_TIMEOUT_SECONDS") == 300
    monkeypatch.setenv("TRANSFORMATION_TIMEOUT_SECONDS", "0")
    assert defaultTimeoutSeconds("TRANSFORMATION_TIMEOUT_SECONDS") == 0
    monkeypatch.setenv("CODE_EXEC_TIMEOUT_SECONDS", "45")
    assert defaultTimeoutSeconds("CODE_EXEC_TIMEOUT_SECONDS") == 45
    monkeypatch.setenv("CODE_EXEC_TIMEOUT_SECONDS", "nope")
    assert defaultTimeoutSeconds("CODE_EXEC_TIMEOUT_SECONDS") == 300
    monkeypatch.setenv("CODE_EXEC_TIMEOUT_SECONDS", "-1")
    assert defaultTimeoutSeconds("CODE_EXEC_TIMEOUT_SECONDS") == 300


def test_channelRoundTrip(monkeypatch):
    monkeypatch.setenv("SANDBOX_SLOT_WAIT_SECONDS", "2.5")
    monkeypatch.setenv("SANDBOX_BLOCK_NETWORK", "0")
    limits = currentRunLimits()
    data = limits.toChannel()
    wire = json.loads(json.dumps(data))
    assert RunLimits.fromChannel(wire) == limits


def test_formatBytes():
    assert _formatBytes(4294967296) == "4 GiB"
    assert _formatBytes(2 * GIB) == "2 GiB"
    assert _formatBytes(512 * 1024**2) == "512 MiB"




def test_runSeesTheThreadCaps(monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "2")
    monkeypatch.setenv("POLARS_MAX_THREADS", "32")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "32")
    code = (
        "env = pd.io.common.os.environ\n"
        'print(pl.thread_pool_size(), env.get("OMP_NUM_THREADS"), env.get("OPENBLAS_NUM_THREADS"),'
        ' env.get("MKL_NUM_THREADS"), env.get("NUMEXPR_MAX_THREADS"))\n'
    )
    output = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, code)
    assert output.split() == ["2", "2", "2", "2", "2"]


def test_arrowPoolsAreCapped(monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "2")
    code = (
        'pa = pd.io.common.os.sys.modules["pyarrow"]\n'
        "print(pa.cpu_count(), pa.io_thread_count())\n"
    )
    output = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, code)
    assert output.split() == ["2", "2"]


# Kernel limits in a real run. They apply to a run uid on Linux, so these need a root parent.
READ_STATUS = (
    'os = pd.io.common.os\n'
    'fd = os.open("/proc/self/status", os.O_RDONLY)\n'
    'status = os.read(fd, 65536).decode()\n'
    'os.close(fd)\n'
    'fields = {line.split(":")[0]: line.split(":")[1].strip() for line in status.splitlines() if ":" in line}\n'
)

HONEST_WORKLOAD = (
    'rows = 1_000_000\n'
    'frame = pl.DataFrame({"k": np.arange(rows) % 1000, "v": np.random.default_rng(0).random(rows)})\n'
    'grouped = frame.group_by("k").agg(pl.col("v").sum()).sort("k")\n'
    'path = "work.parquet"\n'
    'frame.write_parquet(path)\n'
    'table = pd.read_parquet(path, engine="pyarrow")\n'
    'total = float(table["v"].sum() + grouped["v"].sum())\n'
    'lazy = pl.scan_parquet(path).filter(pl.col("k") < 10).collect()\n'
    'print("rows", len(lazy))\n'
)


@separateUids
def test_allocationOverTheProcessLimitIsRefused(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PROCESS_MEMORY_BYTES", str(1 << 30))
    code = (
        'try:\n'
        '    a = np.ones((2 << 30) // 8)\n'
        '    print("allocated")\n'
        'except MemoryError:\n'
        '    print("refused")\n'
    )

    output = TransformationExecutor(timeoutSeconds=120).executeInspection(PROJECT, code)

    assert "refused" in output


@separateUids
def test_threadsAndProcessesStopAtTheRunLimit(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_RUN_PROCESSES", "64")
    code = (
        'threading = pd.io.common.os.sys.modules["threading"]\n'
        'started = 0\n'
        'errors = 0\n'
        'for _ in range(200):\n'
        '    try:\n'
        '        threading.Thread(target=time.sleep, args=(5,), daemon=True).start()\n'
        '        started += 1\n'
        '    except RuntimeError:\n'
        '        errors += 1\n'
        'print("started", started, "errors", errors)\n'
    )
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, "import time\n" + code)

    started, errors = [int(output.split()[i]) for i in (1, 3)]
    assert started <= 64
    assert errors >= 136
    assert executor.executeInspection(PROJECT, 'print("ok")').strip() == "ok"


@separateUids
def test_cpuLimitEndsABusyLoop(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_CPU_SECONDS", "2")
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, "while True: pass", timeoutSeconds=60)

    assert result.returncode == -signal.SIGXCPU
    assert result.stopped == "The run was stopped: it used more than 2 CPU-seconds."
    assert time.time() - started < 30


@separateUids
def test_fileOverTheSizeLimitIsRefused(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_FILE_BYTES", "1048576")
    code = (
        'os = pd.io.common.os\n'
        'pid = os.fork()\n'
        'if pid == 0:\n'
        '    try:\n'
        '        fd = os.open("x", os.O_CREAT | os.O_WRONLY)\n'
        '        left = 2 << 20\n'
        '        while left:\n'
        '            left -= os.write(fd, b"x" * left)\n'
        '        os._exit(0)\n'
        '    except OSError as error:\n'
        '        os._exit(100 + error.errno)\n'
        '_, status = os.waitpid(pid, 0)\n'
        'if os.WIFSIGNALED(status):\n'
        '    print("signal", os.WTERMSIG(status))\n'
        'else:\n'
        '    print("exit", os.WEXITSTATUS(status))\n'
    )

    output = TransformationExecutor(timeoutSeconds=120).executeInspection(PROJECT, code).split()

    assert output in (["signal", str(signal.SIGXFSZ)], ["exit", str(100 + 27)])


@separateUids
def test_runIsFirstInLineForTheOomKiller(storage):
    code = (
        'os = pd.io.common.os\n'
        'fd = os.open("/proc/self/oom_score_adj", os.O_RDONLY)\n'
        'print(os.read(fd, 64).decode().strip())\n'
    )

    output = TransformationExecutor(timeoutSeconds=120).executeInspection(PROJECT, code)

    assert "1000" in output


LIMITS_FILE = (
    'os = pd.io.common.os\n'
    'fd = os.open("/proc/self/limits", os.O_RDONLY)\n'
    'text = os.read(fd, 65536).decode()\n'
    'os.close(fd)\n'
    'rows = {}\n'
    'for line in text.splitlines()[1:]:\n'
    '    rows[line[:25].strip()] = line[25:].split()[:2]\n'
    'print(*rows["Max data size"], *rows["Max cpu time"])\n'
)


@separateUids
def test_runGetsTheDataAndCpuLimitsItWasGiven(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PROCESS_MEMORY_BYTES", str(1 << 30))
    monkeypatch.setenv("SANDBOX_MAX_CPU_SECONDS", "100")

    output = TransformationExecutor(timeoutSeconds=120).executeInspection(PROJECT, LIMITS_FILE)

    assert output.split() == [str(1 << 30), str(1 << 30), "100", "110"]


@separateUids
def test_zeroMeansTheDataAndCpuLimitsAreNotSet(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PROCESS_MEMORY_BYTES", "0")
    monkeypatch.setenv("SANDBOX_MAX_CPU_SECONDS", "0")

    output = TransformationExecutor(timeoutSeconds=120).executeInspection(PROJECT, LIMITS_FILE)

    assert output.split() == ["unlimited"] * 4


@separateUids
def test_honestWorkloadFinishesUnderDefaultLimits(storage):
    code = HONEST_WORKLOAD + READ_STATUS + 'print("Threads", fields["Threads"], "VmHWM", fields["VmHWM"])\n'

    output = TransformationExecutor(timeoutSeconds=300).executeInspection(PROJECT, code)

    assert "rows" in output and "Threads" in output, output
    threads = int(output.split("Threads")[1].split()[0])
    assert threads <= 32


@separateUids
def test_chartWhoseRunTakesEightSecondsStillDrawsItsChart(storage, monkeypatch):
    """A big table's hand-off and a fresh child took 5 to 7 seconds on production, so 7 cut honest charts short."""
    from nubrix.workflows import reportingToolWorkflow
    from utils import codeExecutor

    monkeypatch.setattr(reportingToolWorkflow, "buildLlmChain", lambda name, prompt: MagicMock())
    monkeypatch.setattr(reportingToolWorkflow, "QueryRephaser", MagicMock())
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    code = (
        "import datetime, json\n"
        "end = datetime.datetime.now() + datetime.timedelta(seconds=8)\n"
        "while datetime.datetime.now() < end:\n"
        "    pass\n"
        'print(json.dumps({"chartType": "bar", "data": []}))\n'
    )

    state = reportingToolWorkflow.ReportingToolWorkflow()._runInPythonSandbox({"generatedCode": code, "projectId": PROJECT})

    assert json.loads(state["codeOutput"])["chartType"] == "bar", state["codeOutput"]


# The watchdog: measuring a run and stopping it when it passes a limit.

@pytest.mark.skipif(os.name != "posix", reason="sparse files and block counts are posix")
def test_workUsageCountsAllocatedBlocksNotSparseSize(tmp_path):
    with open(tmp_path / "sparse", "wb") as sparse:
        sparse.truncate(GIB)

    used, entries, over = workUsage(str(tmp_path), 1 << 40, 1000)

    assert used < 1024**2
    assert entries == 1
    assert not over


def test_workUsageStopsWalkingOnceOverTheLimit(tmp_path):
    for number in range(50):
        (tmp_path / f"f{number}").write_bytes(b"")

    _, entries, over = workUsage(str(tmp_path), 1 << 40, 10)

    assert over
    assert entries <= 11


@pytest.mark.skipif(os.name != "posix", reason="creating links needs no privilege on posix")
def test_workUsageDoesNotFollowLinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"x" * (1 << 20))
    work = tmp_path / "work"
    work.mkdir()
    os.symlink(outside, work / "folderLink")
    os.symlink(outside / "big", work / "fileLink")

    used, entries, over = workUsage(str(work), 1 << 40, 1000)

    assert entries == 2
    assert used < 1 << 16
    assert not over


def _forkingCode(sizeMiB: int) -> str:
    return (
        'os = pd.io.common.os\n'
        'for _ in range(3):\n'
        '    if os.fork() == 0:\n'
        f'        block = np.ones(({sizeMiB} << 20) // 8)\n'
        '        pd.io.common.os.sys.modules["time"].sleep(30)\n'
        '        os._exit(0)\n'
        'pd.io.common.os.sys.modules["time"].sleep(30)\n'
    )


def _assertStopped(result, sentence: str, started: float, seconds: float = 10) -> None:
    assert sentence in (result.stopped or ""), result
    assert sentence in result.stderr, result
    assert result.stderr.count(sentence) == 1
    assert time.time() - started < seconds
    assert result.returncode != 0


@separateUids
def test_forksThatAddUpPastTheRunLimitAreStopped(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", "536870912")
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, _forkingCode(300), timeoutSeconds=60)

    _assertStopped(result, "used more than 512 MiB of memory", started)
    assert _liveRunUidProcesses() == []


@separateUids
def test_sharedAnonymousMemoryCounts(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", "536870912")
    code = (
        'block = pd.io.common.mmap.mmap(-1, 600 << 20)\n'
        'chunk = b"x" * (1 << 20)\n'
        'for _ in range(600):\n'
        '    block.write(chunk)\n'
        'pd.io.common.os.sys.modules["time"].sleep(30)\n'
    )
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "used more than 512 MiB of memory", started)


@separateUids
def test_diskFillIsStopped(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_DISK_BYTES", "67108864")
    code = (
        'os = pd.io.common.os\n'
        'chunk = b"x" * (1 << 20)\n'
        'fd = os.open("f", os.O_CREAT | os.O_WRONLY)\n'
        'for _ in range(1024):\n'
        '    os.write(fd, chunk)\n'
        '    os.fsync(fd)\n'
        '    os.sys.modules["time"].sleep(0.02)\n'
    )
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "wrote more than 64 MiB to disk", started, 30)


@separateUids
def test_fileFloodIsStopped(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_FILES", "500")
    code = (
        'os = pd.io.common.os\n'
        'for number in range(5000):\n'
        '    os.close(os.open(f"f{number}", os.O_CREAT | os.O_WRONLY))\n'
        '    if number % 100 == 0:\n'
        '        os.sys.modules["time"].sleep(0.1)\n'
        'os.sys.modules["time"].sleep(30)\n'
    )
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "created more than 500 files", started, 30)


@separateUids
@pytest.mark.parametrize("blockNetwork", ["1", "0"])
def test_loweringTheOomPriorityStopsTheRun(storage, monkeypatch, blockNetwork):
    monkeypatch.setenv("SANDBOX_BLOCK_NETWORK", blockNetwork)
    code = (
        'os = pd.io.common.os\n'
        'try:\n'
        '    fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)\n'
        '    os.write(fd, b"0")\n'
        '    os.close(fd)\n'
        'except OSError:\n'
        '    pass\n'
        'os.sys.modules["time"].sleep(30)\n'
    )
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "changed its own memory priority", started)


@separateUids
def test_watchdogStopsWithTheRun(storage, monkeypatch):
    started = []
    realStart = RunWatchdog.start
    monkeypatch.setattr(RunWatchdog, "start", lambda self: (started.append(self), realStart(self))[1])

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, 'print("ok")', timeoutSeconds=60)

    assert result.returncode == 0
    assert result.stopped is None
    assert len(started) == 1
    assert not started[0].is_alive()


@pytest.mark.parametrize("returncode, watchdogReason, timeoutNote, expected", [
    (-24, None, "", "The run was stopped: it used more than 7 CPU-seconds."),
    (-9, None, "", "The run was stopped: the server ran out of memory."),
    (-9, None, "\nExecution timed out after 5 seconds.\n", "Execution timed out after 5 seconds."),
    (0, "The run was stopped: it created more than 5 files.", "", None),
    (1, "The run was stopped: it created more than 5 files.", "", "The run was stopped: it created more than 5 files."),
    (-9, "The run was stopped: it created more than 5 files.", "", "The run was stopped: it created more than 5 files."),
    (1, None, "", None),
])
def test_stopReasonNamesTheLimitThatEndedTheRun(returncode, watchdogReason, timeoutNote, expected):
    limits = currentRunLimits(maxCpuSeconds=7)

    assert sandboxRunner._stopReason(returncode, watchdogReason, timeoutNote, limits) == expected


HIDE_ENTRY = (
    'os = pd.io.common.os\n'
    'ctypes = np.ctypeslib.ctypes\n'
    'assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0\n'  # Not dumpable: the files in the process's /proc folder are then root's.
    'assert os.stat("/proc/self/status").st_uid == 0\n'
)


@separateUids
def test_runThatHidesItsProcessEntryIsStillCountedForMemory(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", "536870912")
    code = HIDE_ENTRY + 'block = np.ones((600 << 20) // 8)\nos.sys.modules["time"].sleep(30)\n'
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "used more than 512 MiB of memory", started)


@separateUids
def test_runThatHidesItsProcessEntryIsStillCheckedForMemoryPriority(storage):
    lower = 'fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)\nos.write(fd, b"0")\nos.close(fd)\n'
    code = 'os = pd.io.common.os\n' + lower + HIDE_ENTRY + 'os.sys.modules["time"].sleep(30)\n'
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, code, timeoutSeconds=60)

    _assertStopped(result, "changed its own memory priority", started)


@separateUids
def test_failedPriorityWriteFailsTheRunClosed(storage, monkeypatch):
    runRoot = os.environ["SANDBOX_RUN_ROOT"]
    before = set(os.listdir(runRoot))

    def refuse(pid, value):
        raise PermissionError("refused")

    monkeypatch.setattr(sandboxRunner, "_writeOomAdjustment", refuse)

    with pytest.raises(PermissionError):
        sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, 'print("never")', timeoutSeconds=60)

    assert _liveRunUidProcesses() == []
    assert set(os.listdir(runRoot)) == before


@separateUids
def test_watchdogThatFailsStopsTheRunAndLogsIt(storage, monkeypatch):
    messages = []
    monkeypatch.setattr("utils.sandboxWatchdog.logger.exception", lambda message, *args: messages.append(message))

    def broken(self):
        raise RuntimeError("scan failed")

    monkeypatch.setattr(RunWatchdog, "_watch", broken)
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, 'pd.io.common.os.sys.modules["time"].sleep(30)', timeoutSeconds=60)

    assert result.stopped == "The run was stopped: the server ran out of memory."
    assert result.returncode != 0
    assert time.time() - started < 10
    assert any("watchdog failed" in message for message in messages)


STOP_MESSAGE = "The run was stopped: it used more than 4 GiB of memory."


def test_executorsDefaultTo300Seconds(monkeypatch):
    from utils.codeExecutor import REPLManager

    monkeypatch.delenv("TRANSFORMATION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CODE_EXEC_TIMEOUT_SECONDS", raising=False)
    assert TransformationExecutor().timeoutSeconds == 300
    assert REPLManager().timeoutSeconds == 300
    monkeypatch.setenv("TRANSFORMATION_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("CODE_EXEC_TIMEOUT_SECONDS", "0")
    assert TransformationExecutor().timeoutSeconds == 0
    assert REPLManager().timeoutSeconds == 0


class _FakeRedis:
    def zremrangebyscore(self, *args):
        pass

    def zcard(self, *args):
        return 0

    def zadd(self, *args, **kwargs):
        pass

    def expire(self, *args):
        pass

    def zrem(self, *args):
        pass


def _replWith(monkeypatch, result):
    from utils import codeExecutor

    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: _FakeRedis())
    monkeypatch.setattr(codeExecutor, "runSandbox", lambda *args, **kwargs: result)
    return codeExecutor.REPLManager(timeoutSeconds=5)


def test_replManagerKeepsTheStopMessageAfterOutput(monkeypatch):
    result = sandboxRunner.SandboxResult(stdout="partial\n", stderr="", returncode=-9, resultBytes=None, previewBytes=None, stopped=STOP_MESSAGE)
    text = _replWith(monkeypatch, result).run("print(1)", "p1")
    assert "partial" in text
    assert text.count(STOP_MESSAGE) == 1


def test_replManagerReturnsJustTheStopMessageWhenThereIsNoOutput(monkeypatch):
    result = sandboxRunner.SandboxResult(stdout=" \n", stderr=STOP_MESSAGE + "\n", returncode=-9, resultBytes=None, stopped=STOP_MESSAGE)
    assert _replWith(monkeypatch, result).run("print(1)", "p1") == STOP_MESSAGE


def test_replManagerNeverReturnsParseableJsonForAStoppedRun(monkeypatch):
    # Callers accept any valid JSON as a result, so a stopped run's output must not parse, even when it already holds the sentence.
    printed = json.dumps({"data": [1, 2], "note": STOP_MESSAGE})
    result = sandboxRunner.SandboxResult(stdout=printed, stderr="", returncode=-9, resultBytes=None, previewBytes=None, stopped=STOP_MESSAGE)
    text = _replWith(monkeypatch, result).run("print(1)", "p1")
    assert STOP_MESSAGE in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_replManagerReturnsStderrForAFailedRunThatPrintedJson(monkeypatch):
    result = sandboxRunner.SandboxResult(stdout='{"data": [1]}', stderr="Traceback: boom", returncode=1, resultBytes=None, previewBytes=None, stopped=None)
    assert _replWith(monkeypatch, result).run("print(1)", "p1") == "Traceback: boom"


def test_previewShowsTheStopMessage(monkeypatch):
    from nubrix.components import transformationExecutor as module
    from utils.exceptionHandler import CustomException

    result = sandboxRunner.SandboxResult(stdout="", stderr="Traceback noise\n" + STOP_MESSAGE, returncode=-9, resultBytes=None, stopped=STOP_MESSAGE)
    monkeypatch.setattr(module, "runSandbox", lambda *args, **kwargs: result)
    warned = MagicMock()
    monkeypatch.setattr(module.logger, "warning", warned)
    with pytest.raises(CustomException) as caught:
        TransformationExecutor(timeoutSeconds=5).executeAndPreview("p1", "x = 1", "out")
    assert caught.value.statusCode == 400
    assert caught.value.uiMessage == STOP_MESSAGE
    logged = " ".join(str(part) for call in warned.call_args_list for part in call.args)
    assert "p1" in logged and STOP_MESSAGE in logged and "Traceback noise" in logged


# Container-wide slot cap.
BUSY_COPY = "Too many analyses are running right now. Please try again in a moment."
flockable = pytest.mark.skipif(sys.platform == "win32", reason="Slots use fcntl.flock, which Windows doesn't have.")


@flockable
def test_slotIsRefusedWhenAllAreHeld(tmp_path):
    held = [acquireRunSlot(str(tmp_path), 2, 0) for _ in range(2)]
    try:
        started = time.monotonic()
        with pytest.raises(SandboxBusyError, match="Too many analyses"):
            acquireRunSlot(str(tmp_path), 2, 0.3)
        assert time.monotonic() - started >= 0.3
    finally:
        for fd in held:
            releaseRunSlot(fd)


@flockable
def test_waitOfZeroRefusesAtOnce(tmp_path):
    held = acquireRunSlot(str(tmp_path), 1, 0)
    try:
        started = time.monotonic()
        with pytest.raises(SandboxBusyError):
            acquireRunSlot(str(tmp_path), 1, 0)
        assert time.monotonic() - started < 0.1
    finally:
        releaseRunSlot(held)


@flockable
def test_freedSlotIsReused(tmp_path):
    first = acquireRunSlot(str(tmp_path), 1, 0)
    releaseRunSlot(first)
    second = acquireRunSlot(str(tmp_path), 1, 0)
    assert second is not None
    releaseRunSlot(second)


@flockable
def test_slotIsFreedWhenItsHolderDies(tmp_path):
    code = (
        "import sys, time\n"
        "from utils.sandboxSlots import acquireRunSlot\n"
        "fd = acquireRunSlot(sys.argv[1], 1, 0)\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(SandboxBusyError):
            acquireRunSlot(str(tmp_path), 1, 0)
    finally:
        holder.kill()
        holder.wait()
    started = time.monotonic()
    releaseRunSlot(acquireRunSlot(str(tmp_path), 1, 0))
    assert time.monotonic() - started < 0.5


@flockable
def test_slotsQueueAFanOutInsteadOfRefusingIt(tmp_path):
    lock = threading.Lock()
    inFlight = peak = 0

    def one(_):
        nonlocal inFlight, peak
        started = time.monotonic()
        fd = acquireRunSlot(str(tmp_path), 12, 5)
        waited = time.monotonic() - started
        with lock:
            inFlight += 1
            peak = max(peak, inFlight)
        try:
            time.sleep(0.2)
        finally:
            with lock:
                inFlight -= 1
            releaseRunSlot(fd)
        return waited

    with ThreadPoolExecutor(max_workers=16) as pool:
        waits = list(pool.map(one, range(16)))
    assert len(waits) == 16
    assert peak <= 12
    assert max(waits) >= 0.15  # Some of the 16 had to queue.


@flockable
def test_heldSlotIsNotInheritedByChildren(tmp_path):
    import fcntl

    fd = acquireRunSlot(str(tmp_path), 1, 0)
    try:
        assert os.get_inheritable(fd) is False
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
    finally:
        releaseRunSlot(fd)


@flockable
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() != 0, reason="Needs a root parent.")
def test_looseSlotsFolderIsTightenedForARootParent(tmp_path):
    (tmp_path / "slots").mkdir(mode=0o777)
    os.chmod(tmp_path / "slots", 0o777)
    releaseRunSlot(acquireRunSlot(str(tmp_path), 1, 0))
    assert (tmp_path / "slots").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "slots").stat().st_uid == 0


@flockable
def test_slotsFolderThatCannotBeUsedRefusesInsteadOfRunningUncapped(tmp_path):
    (tmp_path / "slots").write_text("not a folder")
    with pytest.raises(SandboxBusyError):
        acquireRunSlot(str(tmp_path), 1, 0)


def test_noCapMeansNoSlot(tmp_path):
    assert acquireRunSlot(None, 12, 1) is None
    assert acquireRunSlot(str(tmp_path), 0, 1) is None
    releaseRunSlot(None)
    assert not (tmp_path / "slots").exists()


def test_busyBecomesA503ForTransformations(monkeypatch):
    from nubrix.components import transformationExecutor as module
    from utils.exceptionHandler import CustomException

    def busy(*args, **kwargs):
        raise SandboxBusyError()

    monkeypatch.setattr(module, "runSandbox", busy)
    executor = TransformationExecutor(timeoutSeconds=5)
    with pytest.raises(CustomException) as caught:
        executor.executeAndPreview("p1", "x = 1", "out")
    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_COPY)
    with pytest.raises(CustomException) as caught:
        executor.executeInspection("p1", "x = 1")
    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_COPY)


def test_busyRaisesFromTheReplManagerAndFreesItsSemaphoreEntry(monkeypatch):
    from utils import codeExecutor

    def busy(*args, **kwargs):
        raise SandboxBusyError()

    monkeypatch.setattr(codeExecutor, "runSandbox", busy)
    redis = MagicMock()
    redis.zcard.return_value = 0
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: redis)
    with pytest.raises(SandboxBusyError) as caught:
        codeExecutor.REPLManager(timeoutSeconds=5, maxConcurrentPerProject=5).run("print(1)", "p1")
    assert str(caught.value) == BUSY_COPY
    redis.zrem.assert_called_once()


def test_runWaitsForASlotBeforeClaimingAUidAndFreesItOnEveryExit(tmp_path, monkeypatch):
    events = []
    monkeypatch.setenv("SANDBOX_RUN_ROOT", str(tmp_path))
    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", lambda *args: events.append(("acquire", args)) or 7)
    monkeypatch.setattr(sandboxRunner, "releaseRunSlot", lambda fd: events.append(("release", fd)))

    def failingClaim():
        events.append(("claim",))
        raise RuntimeError("no uid")

    monkeypatch.setattr(sandboxRunner, "_claimRun", failingClaim)
    with pytest.raises(RuntimeError, match="no uid"):
        sandboxRunner.runSandbox("inspect_launcher.py", "p1", "", 1)
    assert [event[0] for event in events] == ["acquire", "claim", "release"]
    assert events[0][1] == (str(tmp_path), 12, 120.0)
    assert events[2] == ("release", 7)


def test_refusedRunClaimsNothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_ROOT", str(tmp_path))

    def busy(*args):
        raise SandboxBusyError()

    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", busy)
    claim = MagicMock()
    monkeypatch.setattr(sandboxRunner, "_claimRun", claim)
    with pytest.raises(SandboxBusyError):
        sandboxRunner.runSandbox("inspect_launcher.py", "p1", "", 1)
    claim.assert_not_called()


def test_sweepIgnoresTheSlotsFolder(tmp_path, monkeypatch):
    (tmp_path / "slots").mkdir()
    (tmp_path / "slots" / "0").write_text("")
    monkeypatch.setenv("SANDBOX_RUN_ROOT", str(tmp_path))
    clear = MagicMock()
    monkeypatch.setattr(sandboxRunner, "_clearUid", clear)
    sandboxRunner.sweepOrphanedRuns()
    clear.assert_not_called()
    assert (tmp_path / "slots" / "0").exists()


@flockable
def test_slotLockErrorOtherThanHeldRefusesAtOnce(tmp_path, monkeypatch):
    import fcntl

    def broken(*args):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(fcntl, "flock", broken)
    started = time.monotonic()
    with pytest.raises(SandboxBusyError):
        acquireRunSlot(str(tmp_path), 1, 30)
    assert time.monotonic() - started < 1


# A busy sandbox reaches the caller as a 503, and the agent doesn't treat it as a code failure.
class _FakeLlm:
    def __init__(self, calls):
        self.responses = calls
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]


def _agentWith(monkeypatch, responses):
    from langchain_core.messages import AIMessage

    from nubrix.components import transformationExecutor as module
    from nubrix.components.transformationAgent import TransformationAgent
    from utils import llm as llmModule

    def busy(*args, **kwargs):
        raise SandboxBusyError()

    async def noMessages(*args, **kwargs):
        return []

    monkeypatch.setattr(module, "runSandbox", busy)
    monkeypatch.setattr(llmModule, "getLangfuseConfig", lambda **kwargs: {})
    agent = TransformationAgent.__new__(TransformationAgent)
    agent.systemPrompt = "{metadata} {user_request}"
    agent.llm = _FakeLlm([AIMessage(content="", tool_calls=[call]) for call in responses])
    monkeypatch.setattr(agent, "_getMessages", noMessages)
    return agent


def _invoke(agent):
    import asyncio

    return asyncio.run(agent.invoke("p1", "t1", "sum it", {}, []))


def _finalAnswer():
    return {"name": "TransformationAgentResponse", "id": "c1", "args": {"pythonCode": "final_df = 1", "mermaidCode": "graph TD", "userFacingResponse": "ok"}}


def test_busyDuringTheAgentsValidationIsA503AndIsNotRetried(monkeypatch):
    from utils.exceptionHandler import CustomException

    agent = _agentWith(monkeypatch, [_finalAnswer()])
    with pytest.raises(CustomException) as caught:
        _invoke(agent)
    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_COPY)
    assert agent.llm.calls == 1


def test_busyDuringTheAgentsInspectionIsA503AndIsNotFedToTheModel(monkeypatch):
    from utils.exceptionHandler import CustomException

    inspect = {"name": "inspect_dataset", "id": "c0", "args": {"pythonCode": "print(1)"}}
    agent = _agentWith(monkeypatch, [inspect, _finalAnswer()])
    with pytest.raises(CustomException) as caught:
        _invoke(agent)
    assert (caught.value.statusCode, caught.value.errorCode, caught.value.uiMessage) == (503, "SANDBOX_BUSY", BUSY_COPY)
    assert agent.llm.calls == 1


def test_sweepSkipsTheSlotsFolderWithoutAWarning(tmp_path, monkeypatch):
    warned = MagicMock()
    monkeypatch.setattr(sandboxRunner.logger, "warning", warned)
    sandboxRunner._sweepRun(str(tmp_path), "slots")
    warned.assert_not_called()


# Each limit has to be safe to revert on its own.

def test_processLimitDefaultsTo1024WhenThreadsAreUncapped(monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "0")
    assert currentRunLimits().maxProcesses == 1024
    assert currentRunLimits(threads=0).maxProcesses == 1024
    monkeypatch.setenv("SANDBOX_MAX_RUN_PROCESSES", "nope")
    assert currentRunLimits().maxProcesses == 1024


def test_anExplicitProcessLimitAlwaysWins(monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "0")
    monkeypatch.setenv("SANDBOX_MAX_RUN_PROCESSES", "64")
    assert currentRunLimits().maxProcesses == 64
    assert currentRunLimits(maxProcesses=7).maxProcesses == 7


def test_processLimitStaysAt64WhileThreadsAreCapped(monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "4")
    assert currentRunLimits().maxProcesses == 64


def test_childKeepsTheParentsThreadSettingsWhenTheCapIsOff(monkeypatch, tmp_path):
    import socket

    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "2")
    monkeypatch.setenv("POLARS_MAX_THREADS", "3")
    monkeypatch.setenv("OMP_NUM_THREADS", "9")
    popen = MagicMock()
    monkeypatch.setattr(sandboxRunner.subprocess, "Popen", popen)
    left, right = socket.socketpair()
    with left, right:
        sandboxRunner._startChild("inspect_launcher.py", None, str(tmp_path), right, MagicMock(), MagicMock(), currentRunLimits(threads=0))
    env = popen.call_args.kwargs["env"]
    assert env["OPENBLAS_NUM_THREADS"] == "2"
    assert env["POLARS_MAX_THREADS"] == "3"
    assert "OMP_NUM_THREADS" not in env


def test_childGetsTheCapNotTheParentsThreadSettingsWhenCapped(monkeypatch, tmp_path):
    import socket

    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "32")
    popen = MagicMock()
    monkeypatch.setattr(sandboxRunner.subprocess, "Popen", popen)
    left, right = socket.socketpair()
    with left, right:
        sandboxRunner._startChild("inspect_launcher.py", None, str(tmp_path), right, MagicMock(), MagicMock(), currentRunLimits(threads=2))
    assert popen.call_args.kwargs["env"]["OPENBLAS_NUM_THREADS"] == "2"


@separateUids
def test_honestWorkloadFinishesWithTheThreadCapReverted(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "0")
    code = HONEST_WORKLOAD + LIMITS_FILE + 'print("nproc", rows["Max processes"][0])\n'

    output = TransformationExecutor(timeoutSeconds=300).executeInspection(PROJECT, code)

    assert "rows" in output, output
    assert "nproc 1024" in output, output


@pytest.mark.parametrize("value", ["1001", "99999999", "-1", "abc"])
def test_oomScoreAdjOutsideTheKernelsRangeFallsBackToTheDefault(monkeypatch, value):
    monkeypatch.setenv("SANDBOX_OOM_SCORE_ADJ", value)
    assert currentRunLimits().oomScoreAdj == 1000


@pytest.mark.parametrize("value, expected", [("0", 0), ("500", 500), ("1000", 1000)])
def test_oomScoreAdjInRangeIsKept(monkeypatch, value, expected):
    monkeypatch.setenv("SANDBOX_OOM_SCORE_ADJ", value)
    assert currentRunLimits().oomScoreAdj == expected


def test_noWatchdogStartsWhenItsFourLimitsAreOff(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(RunWatchdog, "start", lambda self: started.append(self))
    off = currentRunLimits(maxRunMemory=0, oomScoreAdj=0, maxDiskBytes=0, maxFiles=0)
    assert sandboxRunner._watchRun(MagicMock(), 70000, str(tmp_path), off) is None
    assert started == []
    for field in ("maxRunMemory", "oomScoreAdj", "maxDiskBytes", "maxFiles"):
        sandboxRunner._watchRun(MagicMock(), 70000, str(tmp_path), currentRunLimits(**{**dict(maxRunMemory=0, oomScoreAdj=0, maxDiskBytes=0, maxFiles=0), field: 1}))
    assert len(started) == 4
