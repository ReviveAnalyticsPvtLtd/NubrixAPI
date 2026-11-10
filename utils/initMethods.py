"""
initMethods.py

Shared utilities for data serialization and DataFrame fetching.

Performance layering for very-large data:
  1. In-process Arrow-table LRU  — cheapest, avoids repeated parquet deserialization
     and keeps frames in columnar form (2-5× smaller than pandas in RAM).
  2. Redis Arrow-IPC cache  — shared across gunicorn workers; ~2× smaller than
     parquet on the wire and ~10× faster to deserialize than parquet.
  3. Supabase parquet  — cold path. Always read through PyArrow with predicate +
     column pushdown so we never materialize rows/columns the caller doesn't need.

Public API:
  - ``fetch_data``     → ``pd.DataFrame`` (zero-copy from cached Arrow table).
  - ``fetch_data_pl``  → ``pl.DataFrame`` (eager). 5-10× faster than pandas.
  - ``scan_data``      → ``pl.LazyFrame`` (lazy + query optimizer). 10-100× faster
    for very large datasets because Polars pushes filters/projections through
    its query engine and streams the result.
"""

import pandas as pd
import numpy as np
import datetime
import io
import math
import os
import redis
import threading
import time
from collections import OrderedDict
from utils.logger import logger
from utils.runFilters import applyFilters
from utils.projectStorage import downloadTable, objectUrl, readTableBytes
from utils.storedTables import decodeStoredTable
from typing import Callable, Iterable

try:
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - pyarrow is a hard dep
    pa = None
    pc = None
    pq = None

try:
    import polars as pl
except ImportError:
    pl = None

_REDIS_POOL: redis.ConnectionPool | None = None

# The dashboard filters of the run this process serves. Only a sandbox child sets them (see
# ``sandbox_launcher``); the API process and the other launchers leave them empty, so their reads are unfiltered.
_RUN_FILTERS: list | None = None

_SIZING = None


def _sizing():
    """Lazy sizing singleton (avoids psutil import at module-import time)."""
    global _SIZING
    if _SIZING is None:
        from utils.sizing import (
            detect_resources,
            dataframe_cache_bytes,
            dataframe_cache_entries,
            gunicorn_workers,
        )
        resources = detect_resources()
        _SIZING = {
            "resources": resources,
            "df_cache_bytes": dataframe_cache_bytes(workers=gunicorn_workers(resources), resources=resources),
            "df_cache_entries": dataframe_cache_entries(resources),
        }
    return _SIZING


def use_run_filters(filters: list | None) -> None:
    """Make every table this process loads through the data functions narrow to ``filters``."""
    global _RUN_FILTERS
    _RUN_FILTERS = filters if isinstance(filters, list) and filters else None


def _redis_pool() -> redis.ConnectionPool:
    """Lazy module-level connection pool reused across all fetch_data calls."""
    global _REDIS_POOL
    if _REDIS_POOL is None:
        _REDIS_POOL = redis.ConnectionPool(
            host=os.environ["REDIS_HOST"],
            port=int(os.environ["REDIS_PORT"]),
            password=os.environ["REDIS_PASSWORD"],
            max_connections=32,
            decode_responses=False,
            socket_connect_timeout=5,
            socket_timeout=10,
        )
    return _REDIS_POOL


# In-process Arrow table cache: (projectId, tableName) -> (pa.Table, byte_estimate, stored_at).
# Arrow tables are cheap to share (immutable, reference-counted buffers) so we
# cache them here instead of pandas DataFrames. A ``pd.DataFrame`` view is
# built only at the call site.
_DF_CACHE: "OrderedDict[tuple[str, str], tuple[pa.Table, int, float]]" = OrderedDict()
_DF_CACHE_BYTES = 0
_DF_CACHE_LOCK = threading.Lock()

_REDIS_MAX_ROWS = 100000
# The largest file the parent caches for a sandbox. It never decodes one, so all it can go on is the file's size.
_REDIS_MAX_SPOOLED_BYTES = 64 * 1024 * 1024
_REDIS_SPOOLED_TTL_SECONDS = 300

# Set in sandbox children, where the parent process supplies tables.
_TABLE_SOURCE: "Callable[[str], pa.Table] | None" = None


def use_table_source(source: "Callable[[str], pa.Table]") -> None:
    """Make every cold read in this process go to `source`, which takes a table name."""
    global _TABLE_SOURCE
    _TABLE_SOURCE = source


def _df_bytes(table: "pa.Table") -> int:
    """Approximate in-memory size of an Arrow table."""
    try:
        return int(table.nbytes)
    except Exception:
        return 0


# Row and column counts outlive an expired table, so tables too large for Redis keep their
# size class. invalidate_data_cache drops them with the table.
_DF_SHAPES: "OrderedDict[tuple[str, str], tuple[int, int]]" = OrderedDict()
_DF_SHAPES_MAX = 4096


def _cache_caps():
    """Read cache caps once; env overrides take precedence over auto-sizing."""
    s = _sizing()
    bytes_cap = int(os.environ.get("DF_CACHE_MAX_BYTES", "0") or s["df_cache_bytes"])
    entries_cap = int(os.environ.get("DF_CACHE_MAX_ENTRIES", "0") or s["df_cache_entries"])
    return max(64 * 1024 * 1024, bytes_cap), max(16, entries_cap)


def _cache_put(key: tuple[str, str], table: "pa.Table") -> None:
    bytes_cap, entries_cap = _cache_caps()
    global _DF_CACHE_BYTES
    size = _df_bytes(table)
    with _DF_CACHE_LOCK:
        _DF_SHAPES[key] = (int(table.num_rows), int(table.num_columns))
        _DF_SHAPES.move_to_end(key)
        while len(_DF_SHAPES) > _DF_SHAPES_MAX:
            _DF_SHAPES.popitem(last=False)
    if size > bytes_cap:
        return
    with _DF_CACHE_LOCK:
        if key in _DF_CACHE:
            _DF_CACHE_BYTES -= _DF_CACHE.pop(key)[1]
        _DF_CACHE[key] = (table, size, time.monotonic())
        _DF_CACHE_BYTES += size
        _DF_CACHE.move_to_end(key)
        while (len(_DF_CACHE) > entries_cap or _DF_CACHE_BYTES > bytes_cap) and _DF_CACHE:
            evicted = _DF_CACHE.popitem(last=False)
            _DF_CACHE_BYTES -= evicted[1][1]


def _cache_get(key: tuple[str, str]):
    global _DF_CACHE_BYTES
    # A worker can only clear its own entries, so they expire to limit what the others keep serving.
    ttl = int(os.environ.get("DF_CACHE_TTL_SECONDS") or "60")
    with _DF_CACHE_LOCK:
        entry = _DF_CACHE.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[2] > ttl:
            _DF_CACHE_BYTES -= _DF_CACHE.pop(key)[1]
            return None
        _DF_CACHE.move_to_end(key)
        return entry[0]


def serializer(obj):
    """Serialize NumPy/pandas/polars/datetime/Arrow types to JSON-compatible formats."""
    # NumPy / pandas / Arrow branches (hot path — keep cheap isinstance order)
    if isinstance(obj, (np.integer,)):
        return obj.item()
    if isinstance(obj, (np.floating,)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.datetime64):
        return str(obj)
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict(orient="records")
    if isinstance(obj, pd.Series):
        return obj.tolist()
    # Polars branches — pl.DataFrame / pl.Series
    if pl is not None:
        if isinstance(obj, pl.DataFrame):
            return obj.to_dicts()
        if isinstance(obj, pl.Series):
            return obj.to_list()
    # Datetime / structural
    if isinstance(obj, (pa.TimestampScalar, pa.DurationScalar)):
        return str(obj)
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, complex):
        return {"real": obj.real, "imag": obj.imag}


def _fetch_data_from_supabase(projectId: str, tableName: str) -> "pa.Table":
    """Cold-path reader that pulls the full unfiltered table for caching."""
    if pq is None:
        raise RuntimeError("pyarrow is required.")
    return decodeStoredTable(projectId, tableName, readTableBytes(projectId, tableName))


def _read_redis_bytes(redis_key: str) -> bytes | None:
    r = redis.Redis(connection_pool=_redis_pool())
    try:
        return r.get(redis_key)
    except Exception:
        return None


def _fetch_data_from_redis(redis_key: str, projectId: str, tableName: str) -> "pa.Table | None":
    """The cached table, or None. Its refusals (busy, too large, unreadable) raise rather than read storage again."""
    blob = _read_redis_bytes(redis_key)
    if not blob:
        return None
    if blob[:4] == b"PAR1":
        # The stored file as `spool_table` cached it, so it is decoded as the stored file is.
        return decodeStoredTable(projectId, tableName, blob)
    try:
        # Arrow IPC that `_store_to_redis` wrote: ~2× smaller than parquet bytes and ~10× faster to deserialize.
        return pa.ipc.open_stream(pa.BufferReader(blob)).read_all()
    except Exception:
        return None


def _store_to_redis(redis_key: str, table: "pa.Table") -> None:
    num_rows = table.num_rows
    if num_rows > _REDIS_MAX_ROWS:
        # Skip caching entirely for large/massive tables to prevent Redis OOM
        return

    if num_rows <= 10000:
        ttl = 600  # 10 minutes for small tables
    else:
        ttl = 300  # 5 minutes for medium tables

    r = redis.Redis(connection_pool=_redis_pool())
    try:
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        r.set(name=redis_key, value=sink.getvalue().to_pybytes(), ex=ttl)
    except Exception:
        pass


def _store_file_in_redis(redis_key: str, path: str) -> None:
    """Cache a parquet file as it stands. `_fetch_data_from_redis` reads either format."""
    if os.path.getsize(path) > _REDIS_MAX_SPOOLED_BYTES:
        return
    r = redis.Redis(connection_pool=_redis_pool())
    try:
        with open(path, "rb") as spooled:
            r.set(name=redis_key, value=spooled.read(), ex=_REDIS_SPOOLED_TTL_SECONDS)
    except Exception:
        pass


def _load_table(projectId: str, tableName: str) -> "pa.Table":
    """Arrow LRU → table source in a sandbox child, Redis → Supabase everywhere else."""
    cache_key = (projectId, tableName)
    table = _cache_get(cache_key)
    if table is None:
        if _TABLE_SOURCE is not None:
            table = _TABLE_SOURCE(tableName)
        else:
            redis_key = f"{projectId}::{tableName}"
            table = _fetch_data_from_redis(redis_key, projectId, tableName)
            if table is None:
                # Cold path: scan without pushdown so the cached table is always the
                # complete dataset. Filters are applied to a copy after the cache hit.
                table = _fetch_data_from_supabase(projectId, tableName)
                _store_to_redis(redis_key, table)
        _cache_put(cache_key, table)
    return table


def spool_table(projectId: str, tableName: str, pathStem: str, cancelled: threading.Event | None = None) -> str:
    """Write a table to disk for a sandbox child and return its path.

    Reads Redis then Supabase like `fetch_data`, but only moves bytes: the child
    decodes them. A stored table can be the output of generated code, and a small
    file with a footer that says anything can decode to far more than this process
    can hold. It skips this process's Arrow LRU, which a write through another
    worker can leave stale.
    """
    objectUrl(projectId, f"{tableName}.parquet")  # Refuses a bad name before it reaches Redis or the disk.
    redis_key = f"{projectId}::{tableName}"
    blob = _read_redis_bytes(redis_key)
    if blob:
        path = f"{pathStem}.parquet" if blob[:4] == b"PAR1" else f"{pathStem}.arrow"
        with open(path, "wb") as spooled:
            spooled.write(blob)
        return path
    path = f"{pathStem}.parquet"
    downloadTable(projectId, tableName, path, cancelled)
    _store_file_in_redis(redis_key, path)
    return path


def fetch_data(projectId: str, tableName: str, baseFilters: list | None = None, *args):
    """
    Fetch a DataFrame from in-process Arrow LRU → Redis Arrow cache → Supabase.

    Returns a fresh ``pd.DataFrame`` so the sandbox can mutate it freely. ``baseFilters`` is never mutated.

    Performance:
      The cached table is always the FULL unfiltered dataset so the same cache entry serves many filter
      combinations. The run's filters, ``baseFilters`` and any list arguments are combined and applied once, to a
      copy, after the cache hit.
    """
    filters: list = list(_RUN_FILTERS or [])
    if baseFilters:
        filters.extend(baseFilters)
    for arg in args:
        if isinstance(arg, list):
            filters.extend(arg)

    return applyFilters(_load_table(projectId, tableName), tableName, filters).to_pandas()


def fetch_data_pl(projectId: str, tableName: str) -> "pl.DataFrame":
    """Return a Polars eager DataFrame for the table, narrowed to the run's filters.

    Zero-copy from the cached Arrow table when nothing filters it (Polars wraps Arrow buffers natively).
    5-10× faster than pandas for the same operations. For massive datasets where
    you want lazy + query-optimizer pushdown, use ``scan_data`` instead.
    """
    if pl is None:
        raise RuntimeError("polars is not installed.")
    return pl.from_arrow(applyFilters(_load_table(projectId, tableName), tableName, _RUN_FILTERS))


def scan_data(projectId: str, tableName: str) -> "pl.LazyFrame":
    """Return a Polars LazyFrame for the table, narrowed to the run's filters.

    Polars' lazy engine pushes filters, projections, and aggregations through
    its query optimizer — only the columns/rows actually consumed by the final
    ``collect()`` are materialized. For massive datasets this is 10-100× faster
    than eager pandas because intermediate frames are never built.

    It goes through ``fetch_data_pl``, which already applied the run's filters, so they are not applied twice.
    """
    if pl is None:
        raise RuntimeError("polars is not installed.")
    return fetch_data_pl(projectId, tableName).lazy()


# --- Size classification -------------------------------------------------
#
# Used by the workflow post-processor to deterministically route `fetch_data_pl`
# to `scan_data` (lazy Polars) for tables large enough that lazy execution wins.
# Determined from the cached Arrow table's metadata (`table.num_rows` is O(1))
# and the cached `metadata.json` row counts as a cold fallback.

_ROW_BUCKETS = (
    (10_000, "small"),
    (100_000, "medium"),
    (10_000_000, "large"),
    (float("inf"), "massive"),
)

_SIZE_HINTS = {
    "small": "eager fetch_data_pl is fine; data fits comfortably in RAM",
    "medium": "eager fetch_data_pl preferred; lazy scan_data acceptable",
    "large": "lazy scan_data preferred; explicit predicates recommended",
    "massive": "MUST use scan_data + .collect(streaming=True) — enable pushdown",
}


def _classify_rows(n_rows: int) -> str:
    for limit, label in _ROW_BUCKETS:
        if n_rows < limit:
            return label
    return "massive"


def classify_table_size(projectId: str, tableName: str) -> dict:
    """Return ``{rows_estimate, columns, size_class, hint}`` for the table.

    Falls back to the Redis-cached ``metadata.json`` row count when the Arrow
    table isn't in the in-process LRU, then to this worker's last seen shape,
    then to ``None`` if no signal exists.
    """
    cache_key = (projectId, tableName)
    table = _cache_get(cache_key)
    if table is not None:
        rows = int(table.num_rows)
        cols = int(table.num_columns)
        sz = _classify_rows(rows)
        return {"rows_estimate": rows, "columns": cols, "size_class": sz,
                "hint": _SIZE_HINTS[sz]}
    # metadata.json row counts in the Redis cache come next: clearing a table drops them on every worker.
    redis_key = f"{projectId}::metadata"
    try:
        r = redis.Redis(connection_pool=_redis_pool())
        raw = r.get(redis_key)
    except Exception:
        raw = None
    if raw:
        try:
            import orjson
            md = orjson.loads(raw)
            entry = (md or {}).get(tableName) or (md.get("tables") or {}).get(tableName)
            if isinstance(entry, dict):
                shape = entry.get("shape") or entry.get("rows")
                rows = shape[0] if isinstance(shape, list) else shape
                cols = shape[1] if isinstance(shape, list) else len(entry.get("columns") or [])
                if isinstance(rows, int):
                    sz = _classify_rows(rows)
                    return {"rows_estimate": rows, "columns": cols,
                            "size_class": sz, "hint": _SIZE_HINTS[sz]}
        except Exception:
            pass
    # Then this worker's own record, which outlives the table but not a replacement made elsewhere.
    shape = _DF_SHAPES.get(cache_key)
    if shape is not None:
        rows, cols = shape
        sz = _classify_rows(rows)
        return {"rows_estimate": rows, "columns": cols, "size_class": sz,
                "hint": _SIZE_HINTS[sz]}
    return {"rows_estimate": None, "columns": None, "size_class": "unknown",
            "hint": "size unknown; default to fetch_data_pl"}


def invalidate_data_cache(projectId: str | None = None, tableName: str | None = None) -> None:
    """Drop cached tables for a project/table. Call after transformations apply."""
    global _DF_CACHE_BYTES
    with _DF_CACHE_LOCK:
        if projectId is None and tableName is None:
            _DF_CACHE.clear()
            _DF_SHAPES.clear()
            _DF_CACHE_BYTES = 0
            return
        to_drop = [key for key in _DF_CACHE
                   if (projectId is None or key[0] == projectId)
                   and (tableName is None or key[1] == tableName)]
        for key in to_drop:
            _DF_CACHE_BYTES -= _DF_CACHE.pop(key)[1]
        for key in [key for key in _DF_SHAPES
                    if (projectId is None or key[0] == projectId)
                    and (tableName is None or key[1] == tableName)]:
            del _DF_SHAPES[key]


def forget_table(projectId: str, tableName: str) -> None:
    """Clear a deleted table, and the metadata that lists it, from Redis and this worker.

    Other workers drop their copy when it expires. This worker's copy goes first,
    then Redis, which raises if it fails so a caller that can retry the delete may stop.
    """
    invalidate_data_cache(projectId, tableName)
    r = redis.Redis(connection_pool=_redis_pool())
    r.delete(f"{projectId}::{tableName}", f"{projectId}::metadata.json", f"{projectId}::metadata")
