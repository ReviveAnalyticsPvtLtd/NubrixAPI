"""Tests for how long a deleted table can stay in the data caches."""

from unittest import mock

import pyarrow as pa
import pytest

import utils.initMethods as initMethods


KEY = ("project_1", "sales")


@pytest.fixture(autouse=True)
def _emptyCache():
    initMethods.invalidate_data_cache()
    yield
    initMethods.invalidate_data_cache()


def _table():
    return pa.table({"amount": [1, 2, 3]})


def test_freshEntryIsServed():
    table = _table()
    initMethods._cache_put(KEY, table)

    assert initMethods._cache_get(KEY) is table


def test_entryIsDroppedOnceItIsOlderThanTheLimit():
    with mock.patch("utils.initMethods.time.monotonic", return_value=1000.0):
        initMethods._cache_put(KEY, _table())

    with mock.patch("utils.initMethods.time.monotonic", return_value=1061.0):
        assert initMethods._cache_get(KEY) is None

    assert initMethods._DF_CACHE == {}
    assert initMethods._DF_CACHE_BYTES == 0


def test_readingAnEntryDoesNotExtendItsLife():
    with mock.patch("utils.initMethods.time.monotonic", return_value=1000.0):
        initMethods._cache_put(KEY, _table())

    with mock.patch("utils.initMethods.time.monotonic", return_value=1059.0):
        assert initMethods._cache_get(KEY) is not None

    with mock.patch("utils.initMethods.time.monotonic", return_value=1061.0):
        assert initMethods._cache_get(KEY) is None


def test_limitCanBeRaisedFromTheEnvironment():
    with mock.patch("utils.initMethods.time.monotonic", return_value=1000.0):
        initMethods._cache_put(KEY, _table())

    with (
        mock.patch.dict("os.environ", {"DF_CACHE_TTL_SECONDS": "600"}),
        mock.patch("utils.initMethods.time.monotonic", return_value=1061.0),
    ):
        assert initMethods._cache_get(KEY) is not None


def test_forgetTableClearsRedisAndThisWorker():
    redisClient = mock.Mock()
    otherTable = ("project_1", "orders")
    initMethods._cache_put(KEY, _table())
    initMethods._cache_put(otherTable, _table())

    with mock.patch("utils.initMethods._redis_pool"), mock.patch("utils.initMethods.redis.Redis", return_value=redisClient):
        initMethods.forget_table("project_1", "sales")

    redisClient.delete.assert_called_once_with("project_1::sales", "project_1::metadata.json", "project_1::metadata")
    assert initMethods._cache_get(KEY) is None
    assert initMethods._cache_get(otherTable) is not None


def test_forgetTableReportsARedisFailure():
    redisClient = mock.Mock()
    redisClient.delete.side_effect = ConnectionError("redis unavailable")

    with (
        mock.patch("utils.initMethods._redis_pool"),
        mock.patch("utils.initMethods.redis.Redis", return_value=redisClient),
        pytest.raises(ConnectionError),
    ):
        initMethods.forget_table("project_1", "sales")


def test_emptyLimitFallsBackToTheDefault():
    with mock.patch("utils.initMethods.time.monotonic", return_value=1000.0):
        initMethods._cache_put(KEY, _table())

    with mock.patch.dict("os.environ", {"DF_CACHE_TTL_SECONDS": ""}):
        with mock.patch("utils.initMethods.time.monotonic", return_value=1059.0):
            assert initMethods._cache_get(KEY) is not None
        with mock.patch("utils.initMethods.time.monotonic", return_value=1061.0):
            assert initMethods._cache_get(KEY) is None


def test_forgetTableClearsThisWorkerEvenIfRedisFails():
    redisClient = mock.Mock()
    redisClient.delete.side_effect = ConnectionError("redis unavailable")
    initMethods._cache_put(KEY, _table())

    with (
        mock.patch("utils.initMethods._redis_pool"),
        mock.patch("utils.initMethods.redis.Redis", return_value=redisClient),
        pytest.raises(ConnectionError),
    ):
        initMethods.forget_table("project_1", "sales")

    assert initMethods._cache_get(KEY) is None


def _sizeClass():
    # Redis is down, so only this worker's own record can answer.
    with mock.patch("utils.initMethods._redis_pool", side_effect=ConnectionError("redis unavailable")):
        return initMethods.classify_table_size(*KEY)["size_class"]


def test_sizeClassOutlivesTheExpiredTable():
    # Tables this large are never stored in Redis, so its metadata copy is often gone too.
    with mock.patch("utils.initMethods.time.monotonic", return_value=1000.0):
        initMethods._cache_put(KEY, pa.table({"amount": pa.array(range(150_000))}))

    with mock.patch("utils.initMethods.time.monotonic", return_value=1061.0):
        assert initMethods._cache_get(KEY) is None
        assert _sizeClass() == "large"


def test_forgettingATableDropsItsSizeClass():
    initMethods._cache_put(KEY, pa.table({"amount": pa.array(range(150_000))}))

    initMethods.invalidate_data_cache("project_1", "sales")

    assert _sizeClass() == "unknown"
