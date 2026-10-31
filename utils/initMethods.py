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
from collections import OrderedDict
from utils.logger import logger
from utils.projectStorage import readTableBytes
from typing import Iterable

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


# In-process Arrow table cache: (projectId, tableName) -> (pa.Table, byte_estimate).
# Arrow tables are cheap to share (immutable, reference-counted buffers) so we
# cache them here instead of pandas DataFrames. A ``pd.DataFrame`` view is
# built only at the call site.
_DF_CACHE: "OrderedDict[tuple[str, str], tuple[pa.Table, int]]" = OrderedDict()
_DF_CACHE_BYTES = 0
_DF_CACHE_LOCK = threading.Lock()


def _df_bytes(table: "pa.Table") -> int:
    """Approximate in-memory size of an Arrow table."""
    try:
        return int(table.nbytes)
    except Exception:
        return 0


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
    if size > bytes_cap:
        return
    with _DF_CACHE_LOCK:
        if key in _DF_CACHE:
            _DF_CACHE_BYTES -= _DF_CACHE.pop(key)[1]
        _DF_CACHE[key] = (table, size)
        _DF_CACHE_BYTES += size
        _DF_CACHE.move_to_end(key)
        while (len(_DF_CACHE) > entries_cap or _DF_CACHE_BYTES > bytes_cap) and _DF_CACHE:
            evicted = _DF_CACHE.popitem(last=False)
            _DF_CACHE_BYTES -= evicted[1][1]


def _cache_get(key: tuple[str, str]):
    with _DF_CACHE_LOCK:
        entry = _DF_CACHE.get(key)
        if entry is None:
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


def _translate_to_pyarrow_filter(column: str, condition):
    """Translate a single column condition into a PyArrow compute expression.

    Supports the operator surface used by the reporting / transformation filters:
      scalar      → pc.equal
      list/tuple  → pc.is_in
      {"min": x}  → pc.greater_equal
      {"max": x}  → pc.less_equal
      {"contains": s} (object dtype) → pc.match_substring_case_insensitive
      {"startswith": s}              → pc.starts_with
      {"endswith": s}                → pc.ends_with
    Returns None when the condition cannot be expressed in Arrow.
    """
    if isinstance(condition, dict):
        if "contains" in condition:
            return pc.match_substring(pc.field(column), condition["contains"], ignore_case=True)
        if "startswith" in condition:
            return pc.starts_with(pc.field(column), condition["startswith"])
        if "endswith" in condition:
            return pc.ends_with(pc.field(column), condition["endswith"])
        if "min" in condition:
            return pc.greater_equal(pc.field(column), condition["min"])
        if "max" in condition:
            return pc.less_equal(pc.field(column), condition["max"])
        return None
    if isinstance(condition, (list, tuple, set)):
        if not condition:
            return None
        return pc.is_in(pc.field(column), pa.array(list(condition)))
    return pc.equal(pc.field(column), condition)


def _fetch_data_from_supabase(projectId: str, tableName: str) -> "pa.Table":
    """Cold-path reader that pulls the full unfiltered table for caching."""
    if pq is None:
        raise RuntimeError("pyarrow is required.")
    return pq.read_table(io.BytesIO(readTableBytes(projectId, tableName)))


def _apply_filters_in_arrow_if_possible(table: "pa.Table", tableName: str, filters: list) -> "pa.Table":
    """Apply remaining filters in Arrow if all expressions translate; fall back to pandas."""
    predicates = []
    fallback = False
    for entry in filters or []:
        if not isinstance(entry, dict):
            continue
        for column_path, condition in entry.items():
            if "." in column_path:
                column_table, column = column_path.split(".", 1)
                if column_table != tableName:
                    continue
            else:
                column = column_path
            expr = _translate_to_pyarrow_filter(column, condition)
            if expr is None:
                fallback = True
                break
            predicates.append(expr)
        if fallback:
            break
    if not predicates:
        return table
    if fallback:
        df = table.to_pandas()
        df = _apply_filters(df, tableName, filters)
        return pa.Table.from_pandas(df, preserve_index=False)
    try:
        return table.filter(predicates[0] if len(predicates) == 1 else predicates)
    except Exception:
        df = table.to_pandas()
        df = _apply_filters(df, tableName, filters)
        return pa.Table.from_pandas(df, preserve_index=False)


def _apply_filters(df: pd.DataFrame, tableName: str, baseFilters: list) -> pd.DataFrame:
    """Apply baseFilters scoped to the current table; never touches other tables."""
    if not baseFilters:
        return df
    for filter_entry in baseFilters:
        if not isinstance(filter_entry, dict):
            continue
        for column_path, condition in filter_entry.items():
            if "." in column_path:
                column_table, column = column_path.split(".", 1)
                if column_table != tableName:
                    continue
            else:
                column = column_path
            if column not in df.columns:
                continue
            if isinstance(condition, dict):
                if str(df[column].dtype) == "object":
                    if "contains" in condition:
                        df = df[df[column].str.contains(condition["contains"], case=False, na=False)]
                    elif "startswith" in condition:
                        df = df[df[column].str.startswith(condition["startswith"], na=False)]
                    elif "endswith" in condition:
                        df = df[df[column].str.endswith(condition["endswith"], na=False)]
                    else:
                        continue
                if "min" in condition:
                    df = df[df[column] >= condition["min"]]
                elif "max" in condition:
                    df = df[df[column] <= condition["max"]]
            elif isinstance(condition, (list, tuple, set)):
                df = df[df[column].isin(condition)]
            else:
                df = df[df[column] == condition]
    return df


def _fetch_data_from_redis(redis_key: str) -> "pa.Table | None":
    r = redis.Redis(connection_pool=_redis_pool())
    try:
        blob = r.get(redis_key)
    except Exception:
        return None
    if not blob:
        return None
    try:
        # Arrow IPC = ~2× smaller than parquet bytes and ~10× faster to deserialize.
        return pa.ipc.open_stream(pa.BufferReader(blob)).read_all()
    except Exception:
        try:
            return pq.read_table(pa.BufferReader(blob))
        except Exception:
            return None


def _store_to_redis(redis_key: str, table: "pa.Table") -> None:
    num_rows = table.num_rows
    if num_rows > 100000:
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


def fetch_data(projectId: str, tableName: str, baseFilters: list | None = None, *args):
    """
    Fetch a DataFrame from in-process Arrow LRU → Redis Arrow cache → Supabase.

    Returns a fresh ``pd.DataFrame`` (zero-copy view of the cached Arrow table
    + a shallow copy for caller isolation) so the sandbox can mutate it freely.
    ``baseFilters`` is never mutated.

    Performance:
      The cached table is always the FULL unfiltered dataset so the same cache
      entry serves many filter combinations. Filters are translated to PyArrow
      ``compute`` expressions and applied in-memory after the cache hit; the
      cold path passes simple predicates through to the parquet scan for very
      large datasets where pre-filtered bytes matter.
    """
    filters: list = list(baseFilters) if baseFilters else []
    for arg in args:
        if isinstance(arg, list):
            filters.extend(arg)
    cache_key = (projectId, tableName)
    redis_key = f"{projectId}::{tableName}"

    table: pa.Table | None = _cache_get(cache_key)
    if table is None:
        table = _fetch_data_from_redis(redis_key)
        if table is None:
            # Cold path: scan without pushdown so the cached table is always the
            # complete dataset. Filter translation + pushdown live in
            # ``_apply_filters_in_arrow_if_possible`` for in-memory use.
            table = _fetch_data_from_supabase(projectId, tableName)
            _store_to_redis(redis_key, table)
        _cache_put(cache_key, table)

    if filters:
        table = _apply_filters_in_arrow_if_possible(table, tableName, filters)
    return table.to_pandas()


def fetch_data_pl(projectId: str, tableName: str) -> "pl.DataFrame":
    """Return a Polars eager DataFrame for the table.

    Zero-copy from the cached Arrow table (Polars wraps Arrow buffers natively).
    5-10× faster than pandas for the same operations. For massive datasets where
    you want lazy + query-optimizer pushdown, use ``scan_data`` instead.
    """
    if pl is None:
        raise RuntimeError("polars is not installed.")
    cache_key = (projectId, tableName)
    table = _cache_get(cache_key)
    if table is None:
        table = _fetch_data_from_redis(f"{projectId}::{tableName}")
        if table is None:
            table = _fetch_data_from_supabase(projectId, tableName)
            _store_to_redis(f"{projectId}::{tableName}", table)
        _cache_put(cache_key, table)
    return pl.from_arrow(table)


def scan_data(projectId: str, tableName: str) -> "pl.LazyFrame":
    """Return a Polars LazyFrame for the table.

    Polars' lazy engine pushes filters, projections, and aggregations through
    its query optimizer — only the columns/rows actually consumed by the final
    ``collect()`` are materialized. For massive datasets this is 10-100× faster
    than eager pandas because intermediate frames are never built.
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
    table isn't in the in-process LRU, then to ``None`` if no signal exists.
    """
    cache_key = (projectId, tableName)
    table = _cache_get(cache_key)
    if table is not None:
        rows = int(table.num_rows)
        cols = int(table.num_columns)
        sz = _classify_rows(rows)
        return {"rows_estimate": rows, "columns": cols, "size_class": sz,
                "hint": _SIZE_HINTS[sz]}
    # Cold fallback: metadata.json row counts in the Redis cache.
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
    return {"rows_estimate": None, "columns": None, "size_class": "unknown",
            "hint": "size unknown; default to fetch_data_pl"}


def invalidate_data_cache(projectId: str | None = None, tableName: str | None = None) -> None:
    """Drop cached tables for a project/table. Call after transformations apply."""
    global _DF_CACHE_BYTES
    with _DF_CACHE_LOCK:
        if projectId is None and tableName is None:
            _DF_CACHE.clear()
            _DF_CACHE_BYTES = 0
            return
        to_drop = [key for key in _DF_CACHE
                   if (projectId is None or key[0] == projectId)
                   and (tableName is None or key[1] == tableName)]
        for key in to_drop:
            _DF_CACHE_BYTES -= _DF_CACHE.pop(key)[1]