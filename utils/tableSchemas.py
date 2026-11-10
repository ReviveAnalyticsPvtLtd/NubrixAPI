"""Cached Arrow schemas of stored tables, for the chart generator's grounding.

Each schema lives in Redis at ``<projectId>::schema::<table>`` as ``schema.serialize()`` bytes, with the table data cache's TTL
so a missed invalidation or a read/write race heals as fast as the data cache does;
`invalidate_data_cache` and `forget_table` drop it. A Redis failure means reading without the cache.
"""

from collections.abc import Iterable

import pyarrow as pa

from utils.logger import logger
from utils.projectStorage import readTableBytes
from utils.storedTables import decodeStoredTable


def schemaKey(projectId: str, tableName: str) -> str:
    return f"{projectId}::schema::{tableName}"


def _client():
    from utils import initMethods

    return initMethods.redis.Redis(connection_pool=initMethods._redis_pool())


def _cached(client, key: str) -> pa.Schema | None:
    """The cached schema, None on a miss or a corrupt value. Raises when Redis fails."""
    blob = client.get(key)
    if not blob:
        return None
    try:
        return pa.ipc.read_schema(pa.py_buffer(blob))
    except Exception:
        return None


def _store(client, key: str, schema: pa.Schema) -> None:
    """Cache a schema. Raises when Redis fails."""
    from utils import initMethods

    client.set(key, schema.serialize().to_pybytes(), ex=initMethods._REDIS_SMALL_TABLE_TTL_SECONDS)


def tableSchemas(projectId: str, tableNames: Iterable[str]) -> dict[str, pa.Schema]:
    """The Arrow schema of each named table: from Redis, else read from storage and cached.

    After the first Redis failure in a call, the rest of that call makes no Redis calls: a Redis that is down
    would otherwise cost a connection timeout for every table.
    """
    try:
        client = _client()
    except Exception as exc:
        logger.warning(f"Table schema cache unavailable: {type(exc).__name__}")
        client = None
    schemas: dict[str, pa.Schema] = {}
    for tableName in tableNames:
        key = schemaKey(projectId, tableName)
        schema = None
        if client is not None:
            try:
                schema = _cached(client, key)
            except Exception as exc:
                logger.warning(f"Table schema cache read failed: {type(exc).__name__}")
                client = None
        if schema is None:
            schema = decodeStoredTable(projectId, tableName, readTableBytes(projectId, tableName)).schema
            if client is not None:
                try:
                    _store(client, key, schema)
                except Exception as exc:
                    logger.warning(f"Table schema cache write failed: {type(exc).__name__}")
                    client = None
        schemas[tableName] = schema
    return schemas


def dropSchemas(projectId: str, tableName: str | None = None) -> None:
    """Best effort: delete one table's cached schema, or all of a project's. Never raises."""
    try:
        client = _client()
        if tableName is not None:
            client.delete(schemaKey(projectId, tableName))
            return
        keys = list(client.scan_iter(match=schemaKey(projectId, "*"), count=1000))
        if keys:
            client.delete(*keys)
    except Exception as exc:
        logger.warning(f"Table schema cache drop failed: {type(exc).__name__}")
