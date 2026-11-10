from unittest import mock

import pyarrow as pa
import pytest

from utils import tableSchemas


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expiresAt = {}
        self.now = 0.0
        self.setCalls = []

    def get(self, key):
        if key in self.expiresAt and self.now >= self.expiresAt[key]:
            self.store.pop(key, None)
            del self.expiresAt[key]
        return self.store.get(key)

    def set(self, key, value, *args, ex=None, **kwargs):
        self.setCalls.append((key, args, dict(kwargs, ex=ex)))
        self.store[key] = value
        if ex is not None:
            self.expiresAt[key] = self.now + ex
        else:
            self.expiresAt.pop(key, None)

    def delete(self, *keys):
        for key in keys:
            self.store.pop(key, None)

    def scan_iter(self, match=None, **kwargs):
        prefix = match.rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]


SALES = pa.schema([("id", pa.int64()), ("region", pa.string())])
COSTS = pa.schema([("id", pa.int32())])


@pytest.fixture
def redisFake():
    fake = FakeRedis()
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=fake):
        yield fake


@pytest.fixture
def reader():
    schemas = {"sales": SALES, "costs": COSTS}
    with mock.patch.object(tableSchemas, "readTableBytes", side_effect=lambda p, t: b"bytes-" + t.encode()) as read, \
            mock.patch.object(
                tableSchemas, "decodeStoredTable",
                side_effect=lambda p, t, data: schemas[t].empty_table(),
            ):
        yield read


def test_schemaCachedAndInvalidated(redisFake, reader):
    from utils.initMethods import invalidate_data_cache

    first = tableSchemas.tableSchemas("p1", ["sales"])
    assert first["sales"].equals(SALES)
    assert reader.call_count == 1
    assert "p1::schema::sales" in redisFake.store
    assert redisFake.setCalls[0][0] == "p1::schema::sales"

    second = tableSchemas.tableSchemas("p1", ["sales"])
    assert second["sales"].equals(SALES)
    assert reader.call_count == 1

    invalidate_data_cache("p1", "sales")
    assert "p1::schema::sales" not in redisFake.store
    tableSchemas.tableSchemas("p1", ["sales"])
    assert reader.call_count == 2


def test_forgetTableAlsoDeletesTheSchemaKey(redisFake, reader):
    from utils.initMethods import forget_table

    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    forget_table("p1", "sales")
    assert "p1::schema::sales" not in redisFake.store
    assert "p1::schema::costs" in redisFake.store


def test_invalidateWholeProjectDropsItsSchemas(redisFake, reader):
    from utils.initMethods import invalidate_data_cache

    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    tableSchemas.tableSchemas("p2", ["sales"])
    invalidate_data_cache("p1")
    assert set(redisFake.store) == {"p2::schema::sales"}


def test_invalidateNeverRaisesWhenRedisIsDown():
    from utils.initMethods import invalidate_data_cache

    with mock.patch("utils.initMethods._redis_pool", side_effect=ConnectionError("down")):
        invalidate_data_cache("p1", "sales")


def test_returnsOnlyTheNamedTables(redisFake, reader):
    result = tableSchemas.tableSchemas("p1", ["costs"])
    assert list(result) == ["costs"]


def test_redisGetAndSetErrorsFallBackToReading(reader):
    broken = mock.Mock()
    broken.get.side_effect = ConnectionError("down")
    broken.set.side_effect = ConnectionError("down")
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


def test_redisSetErrorStillReturnsTheSchema(redisFake, reader):
    redisFake.set = mock.Mock(side_effect=ConnectionError("down"))
    result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


def test_poolErrorFallsBackToReading(reader):
    with mock.patch("utils.initMethods._redis_pool", side_effect=KeyError("REDIS_HOST")):
        result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


def test_corruptCachedValueIsAMiss(redisFake, reader):
    redisFake.store["p1::schema::sales"] = b"not a schema"
    result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)
    assert reader.call_count == 1


def test_schemaKeysUseTheTableDataCacheTtl(redisFake, reader):
    from utils import initMethods

    tableSchemas.tableSchemas("p1", ["sales"])
    key, args, kwargs = redisFake.setCalls[0]
    assert kwargs["ex"] == initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    assert kwargs["ex"] >= initMethods._REDIS_MEDIUM_TABLE_TTL_SECONDS


def test_aSchemaCachedAfterForgetTableExpiresWithTheDataCache(redisFake, reader):
    from utils import initMethods
    from utils.initMethods import forget_table

    def readThenTheWriterReplacesTheTable(projectId, tableName):
        forget_table(projectId, tableName)  # the writer finishes between our read and our cache write
        return b"old-bytes"

    reader.side_effect = readThenTheWriterReplacesTheTable
    tableSchemas.tableSchemas("p1", ["sales"])
    assert "p1::schema::sales" in redisFake.store  # the stale schema was cached after the delete

    redisFake.now += initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    reader.side_effect = lambda p, t: b"new-bytes"
    tableSchemas.tableSchemas("p1", ["sales"])
    assert reader.call_count == 2
