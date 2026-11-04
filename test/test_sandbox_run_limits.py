"""Run limits are read from the environment with safe fallbacks."""

import json

import pytest

from nubrix.components.transformationExecutor import TransformationExecutor
from utils.sandboxLimits import (
    RunLimits,
    _formatBytes,
    currentRunLimits,
    defaultTimeoutSeconds,
)

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


@pytest.mark.parametrize("bad", ["abc", "-5", "", "1.5"])
def test_badValueFallsBackToTheDefault(monkeypatch, bad):
    for name, field in VARIABLES.items():
        monkeypatch.setenv(name, bad)
    limits = currentRunLimits()
    if bad == "1.5":
        # Only the slot wait is fractional; every other number stays an int.
        assert limits.slotWaitSeconds == 1.5
        bad_fields = [f for f in DEFAULTS if f != "slotWaitSeconds"]
    else:
        bad_fields = list(DEFAULTS)
    for field in bad_fields:
        assert getattr(limits, field) == DEFAULTS[field], field


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




PROJECT = "11111111-2222-4333-8444-555555555555"


def test_runSeesTheThreadCaps(monkeypatch):
    monkeypatch.setenv("POLARS_MAX_THREADS", "32")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "32")
    code = (
        "env = pd.io.common.os.environ\n"
        'print(pl.thread_pool_size(), env.get("OMP_NUM_THREADS"), env.get("OPENBLAS_NUM_THREADS"),'
        ' env.get("MKL_NUM_THREADS"), env.get("NUMEXPR_MAX_THREADS"))\n'
    )
    output = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, code)
    assert output.split() == ["4", "4", "4", "4", "4"]


def test_arrowPoolsAreCapped():
    code = (
        'pa = pd.io.common.os.sys.modules["pyarrow"]\n'
        "print(pa.cpu_count(), pa.io_thread_count())\n"
    )
    output = TransformationExecutor(timeoutSeconds=60).executeInspection(PROJECT, code)
    assert output.split() == ["4", "4"]


# Kernel limits in a real run. They apply to a run uid on Linux, so these need a root parent.
import os
import signal
import threading
import time

from nubrix.components.transformationExecutor import TransformationExecutor
from test.test_sandbox_storage_reads import PROJECT, separateUids, storage  # noqa: F401  (fixture)
from utils import sandboxRunner

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


@separateUids
def test_honestWorkloadFinishesUnderDefaultLimits(storage):
    code = HONEST_WORKLOAD + READ_STATUS + 'print("Threads", fields["Threads"], "VmHWM", fields["VmHWM"])\n'

    output = TransformationExecutor(timeoutSeconds=300).executeInspection(PROJECT, code)

    print(output)
    assert "rows" in output and "Threads" in output, output
    threads = int(output.split("Threads")[1].split()[0])
    assert threads <= 32


# The watchdog: measuring a run and stopping it when it passes a limit.
from utils.sandboxWatchdog import RunWatchdog, workUsage


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
    from test.test_sandbox_storage_reads import _liveRunUidProcesses

    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", "536870912")
    started = time.time()

    result = sandboxRunner.runSandbox("inspect_launcher.py", PROJECT, _forkingCode(300), timeoutSeconds=60)

    _assertStopped(result, "used more than 512 MiB of memory", started)
    print("memory stop latency", time.time() - started)
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
    print("shared memory stop latency", time.time() - started)


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
    from test.test_sandbox_storage_reads import _liveRunUidProcesses

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
