import json

import pytest

from api.services.importJobs import ImportJobs


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(key in self.values)

    def expire(self, key, seconds):
        if key not in self.values:
            return 0
        self.ttls[key] = seconds
        return 1

    def delete(self, key):
        self.ttls.pop(key, None)
        return int(self.values.pop(key, None) is not None)


TABLES = [("Orders", "orders"), ("Order Items", "order_items")]


def make():
    redis = FakeRedis()
    return ImportJobs(redis), redis


def test_secondStartWhileRunningIsRefused():
    jobs, _ = make()
    assert jobs.start("p1", TABLES)
    assert jobs.start("p1", TABLES) is None
    assert jobs.start("p2", TABLES)


def test_startWritesPendingRecord():
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    record = jobs.get("p1")
    assert record["jobId"] == jobId
    assert record["state"] == "running"
    assert record["kpis"] == "waiting"
    assert record["tables"][1] == {
        "name": "Order Items", "storedAs": "order_items",
        "status": "pending", "rows": None, "reason": None,
    }
    assert redis.ttls["import:p1"] == 604800
    assert redis.ttls["import:p1:running"] == 21600


def test_finishReleasesTheLockAndKeepsTheRecord7Days():
    jobs, redis = make()
    jobs.start("p1", TABLES)
    jobs.finish("p1", "done")
    assert "import:p1:running" not in redis.values
    assert jobs.get("p1")["state"] == "done"
    assert redis.ttls["import:p1"] == 604800
    assert jobs.start("p1", TABLES)


def test_recordHoldsTableProgress():
    jobs, redis = make()
    jobs.start("p1", TABLES)
    jobs.setTable("p1", "orders", status="loaded", rows=42)
    jobs.setTable("p1", "order_items", status="failed", reason="too large")
    jobs.setKpis("p1", "running")
    record = jobs.get("p1")
    assert record["tables"][0]["status"] == "loaded"
    assert record["tables"][0]["rows"] == 42
    assert record["tables"][1]["reason"] == "too large"
    assert record["kpis"] == "running"
    assert redis.ttls["import:p1"] == 604800


def test_recordNeverHoldsCredentials():
    jobs, redis = make()
    jobs.start("p1", TABLES)
    jobs.setTable("p1", "orders", status="loaded", rows=1)
    text = redis.values["import:p1"]
    for key in ("password", "connectionString"):
        assert key not in text
    for fields in ({"password": "x"}, {"connectionString": "x"}, {"name": "x"}):
        with pytest.raises(ValueError):
            jobs.setTable("p1", "orders", **fields)
    with pytest.raises(ValueError):
        jobs.setTable("p1", "orders", status="bogus")
    with pytest.raises(ValueError):
        jobs.setKpis("p1", "bogus")
    with pytest.raises(ValueError):
        jobs.finish("p1", "running")


def test_getReportsFailedWhenWorkerDiedAndLockExpired():
    jobs, redis = make()
    jobs.start("p1", TABLES)
    del redis.values["import:p1:running"]
    assert jobs.get("p1")["state"] == "failed"
    assert json.loads(redis.values["import:p1"])["state"] == "running"


def test_getUnknownProjectIsNone():
    jobs, _ = make()
    assert jobs.get("nope") is None


def test_refreshLockOnlyWhenLockExists():
    jobs, redis = make()
    jobs.refreshLock("p1")
    assert "import:p1:running" not in redis.values
    jobs.start("p1", TABLES)
    redis.ttls["import:p1:running"] = 5
    jobs.refreshLock("p1")
    assert redis.ttls["import:p1:running"] == 21600


def test_importingModuleNeedsNoRedisEnv(monkeypatch):
    for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    import importlib
    import api.services.importJobs as module
    importlib.reload(module)
