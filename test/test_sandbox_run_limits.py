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
