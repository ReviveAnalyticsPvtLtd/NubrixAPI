from unittest import mock

import pyarrow as pa
import pytest

from utils import tableSchemas


class FakeRedis:
    """Redis keys and hashes, each key with an expiry on a clock the test moves. Records every call."""

    def __init__(self):
        self.store = {}
        self.expiresAt = {}
        self.now = 0.0
        self.calls = []

    def _live(self, key):
        if key in self.expiresAt and self.now >= self.expiresAt[key]:
            self.store.pop(key, None)
            del self.expiresAt[key]
        return self.store.get(key)

    def hmget(self, key, fields):
        self.calls.append(("hmget", key, list(fields)))
        found = self._live(key) or {}
        return [found.get(field) for field in fields]

    def hset(self, key, field=None, value=None):
        self.calls.append(("hset", key, field))
        self._live(key)
        self.store.setdefault(key, {})[field] = value

    def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        if self._live(key) is not None:
            self.expiresAt[key] = self.now + seconds

    def hdel(self, key, *fields):
        self.calls.append(("hdel", key, *fields))
        found = self._live(key) or {}
        for field in fields:
            found.pop(field, None)
        if not found:
            self.store.pop(key, None)

    def delete(self, *keys):
        self.calls.append(("delete", *keys))
        for key in keys:
            self.store.pop(key, None)

    def scan_iter(self, *args, **kwargs):
        self.calls.append(("scan",))
        return []

    def scan(self, *args, **kwargs):
        self.calls.append(("scan",))
        return 0, []

    def keys(self, *args, **kwargs):
        self.calls.append(("scan",))
        return []

    def pipeline(self, transaction=True):
        return FakePipeline(self, transaction)

    def callNames(self):
        return [call[0] for call in self.calls]


class FakePipeline:
    """Queues commands and applies them on `execute`, which the parent records as one ("pipeline", commands) call."""

    def __init__(self, parent, transaction):
        self.parent = parent
        self.transaction = transaction
        self.queued = []

    def __getattr__(self, name):
        if name not in ("hset", "expire", "hdel", "delete"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.queued.append((name, args, kwargs))

    def execute(self):
        parent = self.parent
        before = len(parent.calls)
        for name, args, kwargs in self.queued:
            getattr(parent, name)(*args, **kwargs)
        del parent.calls[before:]
        parent.calls.append(("pipeline", self.transaction, tuple((name, *args[:2]) if name == "hset" else (name, *args) for name, args, _ in self.queued)))
        return [None] * len(self.queued)


SALES = pa.schema([("id", pa.int64()), ("region", pa.string())])
COSTS = pa.schema([("id", pa.int32())])


@pytest.fixture
def redisFake():
    fake = FakeRedis()
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=fake), \
            mock.patch.object(tableSchemas, "_now", side_effect=lambda: fake.now):
        yield fake


def _cachedTables(fake, projectId):
    return set(fake._live(f"{projectId}::schemas") or {})


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
    assert _cachedTables(redisFake, "p1") == {"sales"}

    second = tableSchemas.tableSchemas("p1", ["sales"])
    assert second["sales"].equals(SALES)
    assert reader.call_count == 1

    invalidate_data_cache("p1", "sales")
    assert _cachedTables(redisFake, "p1") == set()
    tableSchemas.tableSchemas("p1", ["sales"])
    assert reader.call_count == 2


def test_forgetTableAlsoDeletesTheSchemaKey(redisFake, reader):
    from utils.initMethods import forget_table

    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    forget_table("p1", "sales")
    assert _cachedTables(redisFake, "p1") == {"costs"}
    assert redisFake.calls[-1] == ("pipeline", True, (("delete", "p1::sales", "p1::metadata.json", "p1::metadata"), ("hdel", "p1::schemas", "sales")))


def test_invalidateWholeProjectDropsItsSchemas(redisFake, reader):
    from utils.initMethods import invalidate_data_cache

    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    tableSchemas.tableSchemas("p2", ["sales"])
    invalidate_data_cache("p1")
    assert set(redisFake.store) == {"p2::schemas"}


def test_invalidateNeverRaisesWhenRedisIsDown():
    from utils.initMethods import invalidate_data_cache

    with mock.patch("utils.initMethods._redis_pool", side_effect=ConnectionError("down")):
        invalidate_data_cache("p1", "sales")


def test_returnsOnlyTheNamedTables(redisFake, reader):
    result = tableSchemas.tableSchemas("p1", ["costs"])
    assert list(result) == ["costs"]


def test_redisGetAndSetErrorsFallBackToReading(reader):
    broken = mock.Mock()
    broken.hmget.side_effect = ConnectionError("down")
    broken.pipeline.return_value.execute.side_effect = ConnectionError("down")
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


def test_redisSetErrorStillReturnsTheSchema(redisFake, reader):
    redisFake.pipeline = mock.Mock(side_effect=ConnectionError("down"))
    result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


def test_aColdCallOverThreeTablesWritesInOnePipeline(redisFake, reader):
    from utils import initMethods

    reader.side_effect = lambda p, t, **k: SALES
    tableSchemas.tableSchemas("p1", ["a", "b", "c"])
    ttl = initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    assert redisFake.calls == [
        ("hmget", "p1::schemas", ["a", "b", "c"]),
        ("pipeline", True, (("hset", "p1::schemas", "a"), ("hset", "p1::schemas", "b"), ("hset", "p1::schemas", "c"), ("expire", "p1::schemas", ttl))),
    ]
    assert _cachedTables(redisFake, "p1") == {"a", "b", "c"}


def test_aFullyCachedCallWritesNothing(redisFake, reader):
    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    redisFake.calls.clear()
    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    assert redisFake.callNames() == ["hmget"]


def test_aFailingPipelineIsLoggedOnceWithNoDataAndTheSchemasAreStillReturned(redisFake, reader):
    messages = []
    reader.side_effect = lambda p, t, **k: SALES
    redisFake.pipeline = mock.Mock(return_value=mock.Mock(execute=mock.Mock(side_effect=ConnectionError("secret-host"))))
    with mock.patch("utils.tableSchemas.logger.warning", side_effect=lambda message, *a, **k: messages.append(message)):
        result = tableSchemas.tableSchemas("p1", ["a", "b", "c"])
    assert list(result) == ["a", "b", "c"]
    assert messages == ["Table schema cache write failed: ConnectionError"]
    assert redisFake.pipeline.call_count == 1


def test_theTablesReadBeforeABusyRefusalAreStillCached(redisFake, reader):
    from utils.sandboxSlots import SandboxBusyError

    def read(projectId, tableName, **kwargs):
        if tableName == "b":
            raise SandboxBusyError()
        return SALES

    reader.side_effect = read
    with pytest.raises(SandboxBusyError):
        tableSchemas.tableSchemas("p1", ["a", "b"])
    assert _cachedTables(redisFake, "p1") == {"a"}


def test_poolErrorFallsBackToReading(reader):
    with mock.patch("utils.initMethods._redis_pool", side_effect=KeyError("REDIS_HOST")):
        result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)


@pytest.mark.parametrize(
    "value", [b"not a schema", b"12:not a schema", b"x:" + SALES.serialize().to_pybytes(), b"", b":"],
    ids=["noTime", "notASchema", "badTime", "empty", "onlySeparator"],
)
def test_corruptCachedValueIsAMiss(redisFake, reader, value):
    redisFake.store["p1::schemas"] = {"sales": value}
    result = tableSchemas.tableSchemas("p1", ["sales"])
    assert result["sales"].equals(SALES)
    assert reader.call_count == 1


def test_schemaKeysUseTheTableDataCacheTtl(redisFake, reader):
    from utils import initMethods

    tableSchemas.tableSchemas("p1", ["sales"])
    assert redisFake.calls[-1] == ("pipeline", True, (("hset", "p1::schemas", "sales"), ("expire", "p1::schemas", initMethods._REDIS_SMALL_TABLE_TTL_SECONDS)))
    assert initMethods._REDIS_SMALL_TABLE_TTL_SECONDS >= initMethods._REDIS_MEDIUM_TABLE_TTL_SECONDS


def test_aSchemaCachedAfterForgetTableExpiresWithTheDataCache(redisFake, reader):
    from utils import initMethods
    from utils.initMethods import forget_table

    def readThenTheWriterReplacesTheTable(projectId, tableName, **kwargs):
        forget_table(projectId, tableName)  # the writer finishes between our read and our cache write
        return SALES

    reader.side_effect = readThenTheWriterReplacesTheTable
    tableSchemas.tableSchemas("p1", ["sales"])
    assert _cachedTables(redisFake, "p1") == {"sales"}  # the stale schema was cached after the delete

    redisFake.now += initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    reader.side_effect = lambda p, t, **k: SALES
    tableSchemas.tableSchemas("p1", ["sales"])
    assert reader.call_count == 2


def test_aFailingRedisIsCalledOnceForTwentyTables(reader):
    broken = mock.Mock()
    broken.hmget.side_effect = ConnectionError("down")
    names = [f"t{i}" for i in range(20)]
    reader.side_effect = lambda p, t, **k: SALES
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", names)
    assert list(result) == names
    assert broken.hmget.call_count == 1
    assert broken.method_calls == [mock.call.hmget("p1::schemas", names)]
    assert reader.call_count == 20


def test_aFailingWriteIsTriedOnceForTwentyTables(reader):
    broken = mock.Mock()
    names = [f"t{i}" for i in range(20)]
    broken.hmget.return_value = [None] * len(names)
    broken.pipeline.return_value.execute.side_effect = ConnectionError("down")
    reader.side_effect = lambda p, t, **k: SALES
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        tableSchemas.tableSchemas("p1", names)
    assert broken.hmget.call_count == 1
    assert broken.pipeline.call_count == 1
    assert broken.pipeline.return_value.execute.call_count == 1
    assert broken.hset.call_count == broken.expire.call_count == 0


def test_theNextCallTriesRedisAgain(redisFake, reader):
    broken = mock.Mock()
    broken.hmget.side_effect = ConnectionError("down")
    with mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        tableSchemas.tableSchemas("p1", ["sales"])
    tableSchemas.tableSchemas("p1", ["sales"])
    assert _cachedTables(redisFake, "p1") == {"sales"}


def test_dropsMakeNoScan(redisFake, reader):
    from utils.initMethods import forget_table, invalidate_data_cache

    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    redisFake.calls.clear()
    invalidate_data_cache("p1", "sales")
    assert redisFake.calls == [("hdel", "p1::schemas", "sales")]
    redisFake.calls.clear()
    invalidate_data_cache("p1")
    assert redisFake.calls == [("delete", "p1::schemas")]
    redisFake.calls.clear()
    forget_table("p1", "costs")
    assert "scan" not in redisFake.callNames()
    assert redisFake.calls[-1][0] == "pipeline"
    assert ("hdel", "p1::schemas", "costs") in redisFake.calls[-1][2]


def test_skipUnreadableLeavesOutATableThatFailsAndKeepsTheRest(redisFake, reader):
    warnings = []

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


def test_unreadableTableStillRaisesByDefault(reader):
    reader.side_effect = ValueError("x")
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=mock.Mock(hmget=lambda k, f: [None] * len(f))):
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
    broken.hmget.side_effect = ConnectionError("down")
    names = [f"t{i}" for i in range(20)]

    def decode(projectId, tableName, **kwargs):
        if tableName == "t3":
            raise ValueError("x")
        return SALES

    reader.side_effect = decode
    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=broken):
        result = tableSchemas.tableSchemas("p1", names, skipUnreadable=True)
    assert len(result) == 19
    assert broken.method_calls == [mock.call.hmget("p1::schemas", names)]


def test_aMissReadsOnlyTheSchemaAndPassesHeadroomOnlyForAShadow(redisFake, reader):
    assert not hasattr(tableSchemas, "readTableBytes")
    assert not hasattr(tableSchemas, "decodeStoredTable")
    tableSchemas.tableSchemas("p1", ["sales"])
    tableSchemas.tableSchemas("p2", ["sales"], leaveHeadroom=True)
    assert reader.call_args_list == [mock.call("p1", "sales"), mock.call("p2", "sales", leaveHeadroom=True)]


def test_readsUseOneHmgetPerCall(redisFake, reader):
    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    redisFake.calls.clear()
    result = tableSchemas.tableSchemas("p1", ["sales", "costs"])
    assert list(result) == ["sales", "costs"]
    assert redisFake.calls == [("hmget", "p1::schemas", ["sales", "costs"])]
    assert reader.call_count == 2


def test_anEntryOlderThanTheTtlIsReReadEvenWhileTheHashLives(redisFake, reader):
    from utils import initMethods

    ttl = initMethods._REDIS_SMALL_TABLE_TTL_SECONDS
    tableSchemas.tableSchemas("p1", ["sales"])  # written at 0
    redisFake.now = ttl - 100
    tableSchemas.tableSchemas("p1", ["costs"])  # written later, which renews the hash's own expiry
    redisFake.now = ttl + 50
    assert _cachedTables(redisFake, "p1") == {"sales", "costs"}  # the hash still holds the old entry
    reader.reset_mock()
    tableSchemas.tableSchemas("p1", ["sales", "costs"])
    assert [call.args[1] for call in reader.call_args_list] == ["sales"]


def test_aDropNeverRaisesWhenRedisFails(redisFake):
    redisFake.hdel = mock.Mock(side_effect=ConnectionError("down"))
    redisFake.delete = mock.Mock(side_effect=ConnectionError("down"))
    tableSchemas.dropSchemas("p1", "sales")
    tableSchemas.dropSchemas("p1")
