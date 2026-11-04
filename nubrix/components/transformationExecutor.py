"""
transformationExecutor.py

This module executes generated transformation code and persists approved
transformed tables.

Production design: every code execution runs in a subprocess started by
utils/sandboxRunner.py. The child writes its result to a temp file and the
parent reads it back, avoiding the multiprocessing Queue pickle overhead for
large DataFrames. The child also writes the preview. Generated code runs in
that child, so it can write both files itself and exit before the launcher
does, and neither has to obey the launcher's limits. So the parent only moves a
result's bytes, because a file can decode to far more than the parent can hold,
whatever its footer says, and it keeps a preview as rows.
A run ends after 300 seconds by default (TRANSFORMATION_TIMEOUT_SECONDS=0 turns the
timeout off), and the sandbox's memory, CPU, disk and process limits apply to it
(see utils/sandboxLimits.py).
"""

__version__ = "1.1.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["TransformationExecutor"]


from utils.exceptionHandler import CustomException
from utils.initMethods import fetch_data, fetch_data_pl, scan_data, serializer
from utils.sandboxLimits import defaultTimeoutSeconds
from utils.sandboxRunner import runSandbox
from utils.sandboxSlots import BUSY_CODE, BUSY_MESSAGE, SandboxBusyError
from api.commons import client
from utils.logger import logger
import multiprocessing as mp
import pandas as pd
import numpy as np
import datetime
import redis
import json
import math
import os
import re
import struct

_PARQUET_MAGIC = b"PAR1"
# A footer this long is thousands of columns over thousands of row groups, and parsing one has no limit of its own.
_MAX_FOOTER_BYTES = 16 * 1024 * 1024

# Module-level Redis connection pool for reuse
_redis_pool: redis.ConnectionPool | None = None


def _get_redis_pool() -> redis.ConnectionPool:
    """Get or create a shared Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool(
            host=os.environ["REDIS_HOST"],
            port=int(os.environ["REDIS_PORT"]),
            password=os.environ["REDIS_PASSWORD"],
            max_connections=10,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
    return _redis_pool


_ALLOWED_MODULES = frozenset({"pandas", "numpy", "datetime", "math", "re", "time", "sklearn", "scipy", "statsmodels", "polars", "polars.select"})
_DANGEROUS_BUILTINS = frozenset({
    "open", "compile", "eval", "exec", "globals", "locals", "memoryview",
    "input", "breakpoint", "exit", "quit", "help", "license", "copyright",
    "credit", "__import__",
})


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Allow only transformation-safe imports."""
    rootName = name.split(".")[0]
    if rootName not in _ALLOWED_MODULES:
        raise ImportError(f"Import '{name}' is not allowed in transformation code.")
    return __import__(name, globals, locals, fromlist, level)


def _build_safe_builtins() -> dict:
    """Return a constrained builtins map for generated code."""
    import builtins
    safe = {k: v for k, v in builtins.__dict__.items() if k not in _DANGEROUS_BUILTINS}
    safe["__import__"] = _restricted_import
    return safe


def _sandbox_globals(projectId: str) -> dict:
    """Build the execution globals for a transformation/inspection run."""
    g = {
        "__builtins__": _build_safe_builtins(),
        "datetime": datetime,
        "fetch_data": fetch_data,
        "fetch_data_pl": fetch_data_pl,
        "scan_data": scan_data,
        "math": math,
        "np": np,
        "pd": pd,
        "projectId": projectId,
        "serializer": serializer,
    }
    try:
        import polars as pl  # lazy import keeps parent process lean if unused
        g["pl"] = pl
        # Polars read of a pandas DataFrame is the fastest bridge for sandbox code.
        # The LLM can use ``pl.DataFrame(df)`` to upgrade speed if desired.
    except Exception:
        pass
    return g


class TransformationExecutor:
    """
    Execute pandas/polars transformation code and persist approved transformed tables.
    Uses subprocess isolation (close_fds=True) to avoid fork+Redis deadlocks.
    """
    def __init__(self, timeoutSeconds: int | None = None):
        """Initialize the executor. timeoutSeconds=0 means no hard timeout; None uses the 300 second default."""
        if timeoutSeconds is None:
            timeoutSeconds = defaultTimeoutSeconds("TRANSFORMATION_TIMEOUT_SECONDS")
        self.timeoutSeconds = timeoutSeconds
        self.client = client

    def _redis_client(self) -> redis.Redis:
        """Create a Redis client using shared connection pool."""
        return redis.Redis(connection_pool=_get_redis_pool())

    def _validate_table_name(self, tableName: str) -> str:
        """Validate the output table name."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", tableName):
            raise ValueError("Table name must start with a letter and contain only letters, numbers, hyphens, and underscores.")
        return tableName

    def _execute_code(self, projectId: str, pythonCode: str) -> None:
        """Execute generated code and raise if it fails. A validation run, so nothing it wrote is kept."""
        try:
            self._run_transformation(projectId, pythonCode)
        except SandboxBusyError as e:
            raise self._busy(e)

    def _run_transformation(self, projectId: str, pythonCode: str) -> tuple[list[dict], bytes]:
        """Run transformation code and return the preview rows and the parquet bytes of `final_df`."""
        _, stderr, rc, parquetBytes, previewBytes, stopped = runSandbox("transform_launcher.py", projectId, pythonCode, self.timeoutSeconds, withResult=True)
        if stopped:
            logger.warning(f"Transformation run for project {projectId} was stopped: {stopped} stderr tail: {(stderr or '')[-2000:]}")
            raise ValueError(stopped)
        if rc != 0:
            raise ValueError(stderr.strip() or "Transformation execution failed.")
        if not parquetBytes:
            raise ValueError("Transformation produced no final_df.")
        self._check_parquet_shape(parquetBytes)
        return self._preview(previewBytes), parquetBytes

    def _check_parquet_shape(self, parquetBytes: bytes) -> None:
        """Check the file's frame without parsing it. Whatever reads this table later will parse its footer."""
        if len(parquetBytes) < 12 or parquetBytes[:4] != _PARQUET_MAGIC or parquetBytes[-4:] != _PARQUET_MAGIC:
            raise ValueError("Transformation result is not a parquet file.")
        footerBytes = struct.unpack("<I", parquetBytes[-8:-4])[0]
        if footerBytes > _MAX_FOOTER_BYTES:
            raise ValueError(f"Transformation result's footer is too large: {footerBytes} bytes.")
        if footerBytes > len(parquetBytes) - 12:
            raise ValueError("Transformation result is not a parquet file.")

    def _preview(self, previewBytes: bytes | None) -> list[dict]:
        """Read the rows the run left. A run can write them itself, so only their size and the shape checked here hold.

        Never make a frame of them: rows that each have their own keys make one as wide as it is long.
        """
        try:
            rows = json.loads(previewBytes)
        except (TypeError, ValueError, RecursionError) as e:
            raise ValueError("Transformation produced no readable preview.") from e
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Transformation produced no readable preview.")
        return rows

    def _busy(self, error: SandboxBusyError) -> CustomException:
        """Every run slot stayed taken: tell the caller to retry."""
        exception = CustomException(error, statusCode=503, uiMessage=BUSY_MESSAGE, errorCode=BUSY_CODE)
        logger.error(exception)
        return exception

    def executeInspection(self, projectId: str, pythonCode: str) -> str:
        """Execute inspection code in the sandbox and return stdout + stderr."""
        try:
            stdout, stderr, rc, *_ = runSandbox("inspect_launcher.py", projectId, pythonCode, self.timeoutSeconds)
        except SandboxBusyError as e:
            raise self._busy(e)
        if rc != 0 and stderr:
            return f"Stdout:\n{stdout}\nStderr/Errors:\n{stderr}"
        return stdout if stdout else "Inspection executed successfully with no output."

    def executeAndPreview(self, projectId: str, pythonCode: str, tableName: str) -> tuple[list[dict], bytes]:
        """Execute code and return preview rows plus parquet bytes. The parquet is never decoded here."""
        try:
            self._validate_table_name(tableName)
            return self._run_transformation(projectId, pythonCode)
        except SandboxBusyError as e:
            raise self._busy(e)
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    def apply(self, projectId: str, parquetBytes: bytes, tableName: str) -> None:
        """Upload parquet bytes to Supabase storage and drop the table from the fetch_data caches."""
        try:
            self._validate_table_name(tableName)
            storagePath = f"{projectId}/{tableName}.parquet"
            self.client.storage.from_("AnalyticsHub").upload(
                path=storagePath,
                file=parquetBytes,
                file_options={"upsert": "true"},
            )
            self._redis_client().delete(f"{projectId}::{tableName}")
            try:
                from utils.initMethods import invalidate_data_cache
                invalidate_data_cache(projectId=projectId, tableName=tableName)
            except Exception:
                pass
        except Exception as e:
            exception = CustomException(e, statusCode=500, uiMessage="Failed to apply transformation.")
            logger.error(exception)
            raise exception