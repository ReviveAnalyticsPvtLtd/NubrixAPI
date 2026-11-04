"""Run limits for the sandbox, read from the environment by the parent.

Why: each run starts with the limits the parent reads here, and the child gets
them in the channel dict, never in its environment. A bad value falls back to
the default and logs a warning, so a typo cannot break the request path.
"""

import math
import os
from dataclasses import asdict, dataclass, fields
from typing import Any

from utils.logger import logger

_GIB = 1024**3
_MIB = 1024**2
_DEFAULT_TIMEOUT_SECONDS = 300


def _readInt(name: str, default: int, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0 or (maximum is not None and value > maximum):
        logger.warning(f"Ignoring {name}={raw!r}: using the default {default}.")
        return default
    return value


def _readFloat(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0:
        logger.warning(f"Ignoring {name}={raw!r}: using the default {default}.")
        return default
    return value


def _readBool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if value in ("0", "1"):
        return value == "1"
    logger.warning(f"Ignoring {name}={raw!r}: using the default {int(default)}.")
    return default


@dataclass(frozen=True)
class RunLimits:
    """What one sandbox run may use. Zero turns a limit off unless noted."""

    threads: int
    maxProcesses: int
    maxProcessMemory: int
    maxRunMemory: int
    oomScoreAdj: int
    maxCpuSeconds: int
    maxDiskBytes: int
    maxFiles: int
    maxFileBytes: int
    blockNetwork: bool
    maxConcurrentRuns: int
    slotWaitSeconds: float

    def toChannel(self) -> dict:
        """A JSON-serialisable dict for the private channel to the child."""
        return asdict(self)

    @classmethod
    def fromChannel(cls, data: dict) -> "RunLimits":
        """Rebuild the limits from exactly what `toChannel` produced."""
        values: dict[str, Any] = {}
        for field in fields(cls):
            raw = data[field.name]
            if field.type == "bool" or field.type is bool:
                values[field.name] = bool(raw)
            elif field.type == "float" or field.type is float:
                values[field.name] = float(raw)
            else:
                values[field.name] = int(raw)
        return cls(**values)


def currentRunLimits(**overrides: Any) -> RunLimits:
    """Read the limits from the environment, then apply keyword overrides."""
    threads = overrides["threads"] if "threads" in overrides else _readInt("SANDBOX_RUN_THREADS", 4)
    values: dict[str, Any] = {
        "threads": threads,
        # With no thread cap, library pools size from the CPU count and each thread counts as a process, so 64 would stop honest runs.
        "maxProcesses": _readInt("SANDBOX_MAX_RUN_PROCESSES", 1024 if threads == 0 else 64),
        "maxProcessMemory": _readInt("SANDBOX_MAX_PROCESS_MEMORY_BYTES", 4 * _GIB),
        "maxRunMemory": _readInt("SANDBOX_MAX_RUN_MEMORY_BYTES", 4 * _GIB),
        "oomScoreAdj": _readInt("SANDBOX_OOM_SCORE_ADJ", 1000, maximum=1000),
        "maxCpuSeconds": _readInt("SANDBOX_MAX_CPU_SECONDS", 1200),
        "maxDiskBytes": _readInt("SANDBOX_MAX_DISK_BYTES", 2 * _GIB),
        "maxFiles": _readInt("SANDBOX_MAX_FILES", 10000),
        "maxFileBytes": _readInt("SANDBOX_MAX_FILE_BYTES", 2 * _GIB),
        "blockNetwork": _readBool("SANDBOX_BLOCK_NETWORK", True),
        "maxConcurrentRuns": _readInt("SANDBOX_MAX_CONCURRENT_RUNS", 12),
        "slotWaitSeconds": _readFloat("SANDBOX_SLOT_WAIT_SECONDS", 120.0),
    }
    unknown = set(overrides) - set(values)
    if unknown:
        raise TypeError(f"Unknown run limit(s): {sorted(unknown)}")
    values.update(overrides)
    return RunLimits(**values)


def defaultTimeoutSeconds(variable: str) -> int:
    """Wall-time limit from `variable`: 300 when unset or bad, 0 means none."""
    return _readInt(variable, _DEFAULT_TIMEOUT_SECONDS)


def _formatBytes(n: int) -> str:
    """Show a byte count for users, e.g. "4 GiB" or "512 MiB"."""
    if n >= _GIB:
        gib = n / _GIB
        return f"{int(gib)} GiB" if n % _GIB == 0 else f"{gib:.1f} GiB"
    return f"{round(n / _MIB)} MiB"
