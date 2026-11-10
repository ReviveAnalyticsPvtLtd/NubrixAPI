"""Cached Arrow schemas of stored tables, for the chart generator's grounding.

A project's schemas live in one Redis hash, ``<projectId>::schemas``: one field per table, holding the time it was
written and ``schema.serialize()`` bytes. An entry older than the table data cache's TTL is a miss, so a missed
invalidation or a read/write race heals as fast as the data cache does, and every write also sets the hash's own
expiry to that TTL. `invalidate_data_cache` and `forget_table` drop one field (HDEL) or the whole hash (DEL), so no
drop ever scans the keyspace. A Redis failure means reading without the cache. A miss reads only the schema
(`storedTableSchema`): a table's footer, never a whole-table decode in this process.
"""

import time
from collections.abc import Iterable

import pyarrow as pa

from utils.logger import logger
from utils.sandboxSlots import isBusy
from utils.storedTables import storedTableSchema

# Separates an entry's write time from its schema bytes.
_TIME_SEPARATOR = b":"


def schemasKey(projectId: str) -> str:
    """The Redis hash that holds a project's cached schemas, one field per table."""
    return f"{projectId}::schemas"


def _now() -> float:
    return time.time()


def _ttlSeconds() -> int:
    from utils import initMethods

    return initMethods._REDIS_SMALL_TABLE_TTL_SECONDS


def _client():
    from utils import initMethods

    return initMethods.redis.Redis(connection_pool=initMethods._redis_pool())


def _entry(value, now: float) -> pa.Schema | None:
    """The schema an entry holds, None for a missing, corrupt or out-of-date one."""
    if not value:
        return None
    try:
        written, separator, blob = bytes(value).partition(_TIME_SEPARATOR)
        if not separator or not 0 <= now - float(written) < _ttlSeconds():
            return None
        return pa.ipc.read_schema(pa.py_buffer(blob))
    except Exception:
        return None


def _entryValue(schema: pa.Schema) -> bytes:
    """A cache entry: the time the schema was read, then its bytes."""
    return repr(_now()).encode() + _TIME_SEPARATOR + schema.serialize().to_pybytes()


def _storeAll(client, projectId: str, entries: dict[str, bytes]) -> None:
    """Cache a call's entries and renew the hash's expiry in one transaction. Never raises: a failure is logged once."""
    if not entries:
        return
    try:
        key = schemasKey(projectId)
        pipe = client.pipeline(transaction=True)
        for tableName, value in entries.items():
            pipe.hset(key, tableName, value)
        pipe.expire(key, _ttlSeconds())
        pipe.execute()
    except Exception as exc:
        logger.warning(f"Table schema cache write failed: {type(exc).__name__}")


def tableSchemas(projectId: str, tableNames: Iterable[str], skipUnreadable: bool = False, leaveHeadroom: bool = False) -> dict[str, pa.Schema]:
    """The Arrow schema of each named table: from Redis (one HMGET), else read from storage and cached.

    After the first Redis failure in a call, the rest of that call makes no Redis calls: a Redis that is down
    would otherwise cost a connection timeout for every table. What the call reads is cached at its end, in one
    transaction (the entries and one EXPIRE), and a failure of that write is logged once and never raises.

    With `skipUnreadable`, a table whose read fails is left out of the result and logged by exception class only, and
    the rest are still read. A busy sandbox (`isBusy`) always propagates.

    With `leaveHeadroom` (a shadow run), a table that needs the reader child gets it only without waiting and while the
    sandbox has slots to spare; otherwise the read raises SandboxBusyError, which is never skipped.
    """
    names = list(tableNames)
    try:
        client = _client()
    except Exception as exc:
        logger.warning(f"Table schema cache unavailable: {type(exc).__name__}")
        client = None
    cached: dict[str, pa.Schema | None] = {}
    if client is not None and names:
        try:
            values = client.hmget(schemasKey(projectId), names)
            now = _now()
            cached = {name: _entry(value, now) for name, value in zip(names, values)}
        except Exception as exc:
            logger.warning(f"Table schema cache read failed: {type(exc).__name__}")
            client = None
    schemas: dict[str, pa.Schema] = {}
    fresh: dict[str, bytes] = {}
    try:
        for tableName in names:
            schema = cached.get(tableName)
            if schema is None:
                try:
                    schema = storedTableSchema(projectId, tableName, leaveHeadroom=True) if leaveHeadroom else storedTableSchema(projectId, tableName)
                except Exception as exc:
                    if not skipUnreadable or isBusy(exc):
                        raise
                    logger.warning(f"Table schema unreadable, skipped: {type(exc).__name__}")
                    continue
                if client is not None:
                    fresh[tableName] = _entryValue(schema)
            schemas[tableName] = schema
    finally:
        # One round trip for what this call read, also when a later table raises.
        if client is not None:
            _storeAll(client, projectId, fresh)
    return schemas


def dropSchemas(projectId: str, tableName: str | None = None) -> None:
    """Best effort: delete one table's cached schema (HDEL), or all of a project's (DEL). Never raises."""
    try:
        client = _client()
        if tableName is not None:
            client.hdel(schemasKey(projectId), tableName)
        else:
            client.delete(schemasKey(projectId))
    except Exception as exc:
        logger.warning(f"Table schema cache drop failed: {type(exc).__name__}")
