from unittest import mock

import pyarrow as pa
import pytest

from utils import tableSchemas


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.setCalls = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, *args, **kwargs):
        self.setCalls.append((key, args, kwargs))
        self.store[key] = value

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
    key, args, kwargs = redisFake.setCalls[0]
    assert not args and "ex" not in kwargs and "px" not in kwargs

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
