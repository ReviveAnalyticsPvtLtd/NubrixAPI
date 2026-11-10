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
        self.scanKwargs = kwargs
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
    with mock.patch.object(tableSchemas, "storedTableSchema", side_effect=lambda p, t, **k: schemas[t]) as read:
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

    def readThenTheWriterReplacesTheTable(projectId, tableName, **kwargs):
        forget_table(projectId, tableName)  # the writer finishes between our read and our cache write
        return SALES

    reader.side_effect = readThenTheWriterReplacesTheTable
    tableSchemas.tableSchemas("p1", ["sales"])
    assert "p1::schema::sales" in redisFake.store  # the stale schema was cached after the delete

    redisFake.now += initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    reader.side_effect = lambda p, t, **k: SALES
    tableSchemas.tableSchemas("p1", ["sales"])
    assert reader.call_count == 2


def test_aFailingRedisIsCalledOnceForTwentyTables(reader):
    broken = mock.Mock()
    broken.get.side_effect = ConnectionError("down")
    broken.set.side_effect = ConnectionError("down")
    names = [f"t{i}" for i in range(20)]
    reader.side_effect = lambda p, t, **k: SALES
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", names)
    assert list(result) == names
    assert broken.get.call_count + broken.set.call_count == 1
    assert reader.call_count == 20


def test_aFailingSetStopsAllLaterRedisCallsInThatCall(reader):
    broken = mock.Mock()
    broken.get.return_value = None
    broken.set.side_effect = ConnectionError("down")
    names = [f"t{i}" for i in range(20)]
    reader.side_effect = lambda p, t, **k: SALES
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        tableSchemas.tableSchemas("p1", names)
    assert broken.get.call_count == 1
    assert broken.set.call_count == 1


def test_theNextCallTriesRedisAgain(redisFake, reader):
    broken = mock.Mock()
    broken.get.side_effect = ConnectionError("down")
    with mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        tableSchemas.tableSchemas("p1", ["sales"])
    tableSchemas.tableSchemas("p1", ["sales"])
    assert "p1::schema::sales" in redisFake.store


def test_projectWideDropScansInBatches(redisFake, reader):
    from utils.initMethods import invalidate_data_cache

    tableSchemas.tableSchemas("p1", ["sales"])
    invalidate_data_cache("p1")
    assert redisFake.scanKwargs == {"count": 1000}


def test_skipUnreadableLeavesOutATableThatFailsAndKeepsTheRest(redisFake, reader):
    warnings = []
    real = tableSchemas.storedTableSchema

    def decode(projectId, tableName, **kwargs):
        if tableName == "legacy":
            raise ValueError("unreadable 'Secret Corp'")
        return SALES

    reader.side_effect = decode
    with mock.patch.object(tableSchemas.logger, "warning", side_effect=lambda message, *a, **k: warnings.append(message)):
        result = tableSchemas.tableSchemas("p1", ["sales", "legacy", "costs"], skipUnreadable=True)
    assert list(result) == ["sales", "costs"]
    assert any("ValueError" in message for message in warnings)
    assert not any("Secret Corp" in message for message in warnings)
    assert real is not None


def test_unreadableTableStillRaisesByDefault(reader):
    reader.side_effect = ValueError("x")
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=mock.Mock(get=lambda k: None)):
        with pytest.raises(ValueError):
            tableSchemas.tableSchemas("p1", ["sales"])


@pytest.mark.parametrize("busy", ["error", "code"])
def test_busyPropagatesEvenWhenSkippingUnreadable(redisFake, reader, busy):
    from utils.sandboxSlots import BUSY_CODE, SandboxBusyError

    error = SandboxBusyError()
    if busy == "code":
        error = RuntimeError("busy")
        error.errorCode = BUSY_CODE
    reader.side_effect = error
    with pytest.raises(type(error)):
        tableSchemas.tableSchemas("p1", ["sales"], skipUnreadable=True)


def test_aFailingRedisGetsOneAttemptWhileSkippingUnreadable(reader):
    broken = mock.Mock()
    broken.get.side_effect = ConnectionError("down")
    broken.set.side_effect = ConnectionError("down")
    names = [f"t{i}" for i in range(20)]

    def decode(projectId, tableName, **kwargs):
        if tableName == "t3":
            raise ValueError("x")
        return SALES

    reader.side_effect = decode
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", names, skipUnreadable=True)
    assert len(result) == 19
    assert broken.get.call_count + broken.set.call_count == 1


def test_aMissReadsOnlyTheSchemaAndPassesHeadroomOnlyForAShadow(redisFake, reader):
    assert not hasattr(tableSchemas, "readTableBytes")
    assert not hasattr(tableSchemas, "decodeStoredTable")
    tableSchemas.tableSchemas("p1", ["sales"])
    tableSchemas.tableSchemas("p2", ["sales"], leaveHeadroom=True)
    assert reader.call_args_list == [mock.call("p1", "sales"), mock.call("p2", "sales", leaveHeadroom=True)]
