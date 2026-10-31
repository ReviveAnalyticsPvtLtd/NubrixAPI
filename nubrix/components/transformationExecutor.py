"""
transformationExecutor.py

This module executes generated transformation code and persists approved
transformed tables.

Production design: every code execution runs in a forked child process.
The child writes its result to a temp file and the parent reads it back,
avoiding the multiprocessing Queue pickle overhead for large DataFrames.
No hard timeout/SIGKILL is enforced by default; set TRANSFORMATION_TIMEOUT_SECONDS
env var to re-enable one.
"""

__version__ = "1.1.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["TransformationExecutor"]


from utils.exceptionHandler import CustomException
from utils.initMethods import fetch_data, fetch_data_pl, scan_data, serializer
from api.commons import client
from utils.logger import logger
import multiprocessing as mp
import subprocess
import sys
import pandas as pd
import numpy as np
import datetime
import redis
import json
import math
import io
import os
import re
import tempfile
import polars as pl

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


def _run_subprocess(launcher_name: str, payload: dict, timeoutSeconds: int) -> tuple[str, str, int]:
    """Run a launcher script in a subprocess with close_fds=True (no inherited sockets)."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    launcher_path = os.path.join(repo_root, "utils", launcher_name)
    inherit_envs = (
        "PATH", "PYTHONPATH", "HOME", "LANG", "LC_ALL", "TZ",
        "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "REDIS_DB",
        "SUPABASE_URL", "SUPABASE_KEY", "DATABASE_URL",
        "STORAGE_URL",
        "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "POLARS_MAX_THREADS", "OPENBLAS_NUM_THREADS",
        "DF_CACHE_MAX_BYTES", "DF_CACHE_MAX_ENTRIES",
    )
    clean_env = {k: os.environ[k] for k in inherit_envs if k in os.environ}
    clean_env["PYTHONPATH"] = repo_root + os.pathsep + clean_env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-s", launcher_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=clean_env,
        close_fds=True,
    )
    try:
        if timeoutSeconds and timeoutSeconds > 0:
            stdout, stderr = proc.communicate(input=json.dumps(payload), timeout=timeoutSeconds)
        else:
            stdout, stderr = proc.communicate(input=json.dumps(payload))
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        stderr += f"\nExecution timed out after {timeoutSeconds} seconds.\n"
    return stdout, stderr, proc.returncode


class TransformationExecutor:
    """
    Execute pandas/polars transformation code and persist approved transformed tables.
    Uses subprocess isolation (close_fds=True) to avoid fork+Redis deadlocks.
    """
    def __init__(self, timeoutSeconds: int | None = None):
        """Initialize the executor. timeoutSeconds=0/None means no hard timeout."""
        if timeoutSeconds is None:
            env_val = os.environ.get("TRANSFORMATION_TIMEOUT_SECONDS", "0")
            timeoutSeconds = int(env_val) if env_val else 0
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

    def _execute_code(self, projectId: str, pythonCode: str) -> pd.DataFrame:
        """Execute generated code and return `final_df` (validation run, result discarded)."""
        fd, result_path = tempfile.mkstemp(suffix=".parquet", prefix="transform_")
        os.close(fd)
        try:
            _, stderr, rc = _run_subprocess(
                "transform_launcher.py",
                {"projectId": projectId, "code": pythonCode, "result_path": result_path},
                self.timeoutSeconds,
            )
            if rc != 0:
                raise ValueError(stderr.strip() or "Transformation execution failed.")
            if not os.path.exists(result_path) or os.path.getsize(result_path) == 0:
                raise ValueError("Transformation produced no final_df.")
            return pd.read_parquet(result_path)
        finally:
            try:
                os.unlink(result_path)
            except Exception:
                pass

    def executeInspection(self, projectId: str, pythonCode: str) -> str:
        """Execute inspection code in the sandbox and return stdout + stderr."""
        stdout, stderr, rc = _run_subprocess(
            "inspect_launcher.py",
            {"projectId": projectId, "code": pythonCode},
            self.timeoutSeconds,
        )
        if rc != 0 and stderr:
            return f"Stdout:\n{stdout}\nStderr/Errors:\n{stderr}"
        return stdout if stdout else "Inspection executed successfully with no output."

    def executeAndPreview(self, projectId: str, pythonCode: str, tableName: str) -> tuple[list[dict], bytes]:
        """Execute code and return preview rows plus parquet bytes.
        Uses Polars to read only the first 100 rows for the preview, so heavy
        datasets don't need to be fully loaded into pandas.
        """
        try:
            self._validate_table_name(tableName)
            fd, result_path = tempfile.mkstemp(suffix=".parquet", prefix="transform_")
            os.close(fd)
            try:
                _, stderr, rc = _run_subprocess(
                    "transform_launcher.py",
                    {"projectId": projectId, "code": pythonCode, "result_path": result_path},
                    self.timeoutSeconds,
                )
                if rc != 0:
                    raise ValueError(stderr.strip() or "Transformation execution failed.")
                if not os.path.exists(result_path) or os.path.getsize(result_path) == 0:
                    raise ValueError("Transformation produced no final_df.")
                with open(result_path, "rb") as f:
                    parquetBytes = f.read()
                previewRecords = (
                    pl.read_parquet(io.BytesIO(parquetBytes))
                    .head(100)
                    .to_dicts()
                )
                previewRows = json.loads(json.dumps(previewRecords, default=serializer))
                return previewRows, parquetBytes
            finally:
                try:
                    os.unlink(result_path)
                except Exception:
                    pass
        except Exception as e:
            exception = CustomException(e, statusCode=400, uiMessage=str(e))
            logger.error(exception)
            raise exception

    def apply(self, projectId: str, parquetBytes: bytes, tableName: str) -> None:
        """Upload parquet bytes to Supabase storage and refresh the fetch_data cache."""
        try:
            self._validate_table_name(tableName)
            storagePath = f"{projectId}/{tableName}.parquet"
            self.client.storage.from_("AnalyticsHub").upload(
                path=storagePath,
                file=parquetBytes,
                file_options={"upsert": "true"},
            )
            redisClient = self._redis_client()
            redisClient.set(name=f"{projectId}::{tableName}", value=parquetBytes, ex=300)
            try:
                from utils.initMethods import invalidate_data_cache
                invalidate_data_cache(projectId=projectId, tableName=tableName)
            except Exception:
                pass
        except Exception as e:
            exception = CustomException(e, statusCode=500, uiMessage="Failed to apply transformation.")
            logger.error(exception)
            raise exception