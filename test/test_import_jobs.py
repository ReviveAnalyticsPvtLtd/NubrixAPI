import json

import pytest

import api.services.importJobs as module
from api.services.importJobs import ImportJobs


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.failSetOn = None

    def set(self, key, value, nx=False, ex=None):
        if key == self.failSetOn:
            raise RuntimeError("redis unavailable")
        if nx and key in self.values:
            return None
        self.values[key] = value.encode() if isinstance(value, str) else value
        self.ttls.pop(key, None)  # a plain SET clears the TTL, like real Redis
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

    def delete(self, *keys):
        for key in keys:
            self.ttls.pop(key, None)
        return sum(self.values.pop(key, None) is not None for key in keys)


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
    assert redis.values["import:p1:running"] == jobId.encode()


def test_startReleasesTheLockWhenTheRecordWriteFails():
    jobs, redis = make()
    redis.failSetOn = "import:p1"
    with pytest.raises(RuntimeError):
        jobs.start("p1", TABLES)
    assert "import:p1:running" not in redis.values
    redis.failSetOn = None
    assert jobs.start("p1", TABLES)


def test_finishReleasesTheLockAndKeepsTheRecord7Days():
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    assert jobs.finish("p1", jobId, "done") is True
    assert "import:p1:running" not in redis.values
    assert jobs.get("p1")["state"] == "done"
    assert redis.ttls["import:p1"] == 604800
    assert jobs.start("p1", TABLES)


def test_recordHoldsTableProgress():
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    assert jobs.setTable("p1", jobId, "orders", status="loaded", rows=42) is True
    jobs.setTable("p1", jobId, "order_items", status="failed", reason="too large")
    jobs.setKpis("p1", jobId, "running")
    record = jobs.get("p1")
    assert record["tables"][0]["status"] == "loaded"
    assert record["tables"][0]["rows"] == 42
    assert record["tables"][1]["reason"] == "too large"
    assert record["kpis"] == "running"
    assert redis.ttls["import:p1"] == 604800


def test_onlyProgressFieldsAndKnownStatusesAreAccepted():
    jobs, _ = make()
    jobId = jobs.start("p1", TABLES)
    for fields in ({"password": "x"}, {"connectionString": "x"}, {"name": "x"}):
        with pytest.raises(ValueError):
            jobs.setTable("p1", jobId, "orders", **fields)
    with pytest.raises(ValueError):
        jobs.setTable("p1", jobId, "orders", status="bogus")
    with pytest.raises(ValueError):
        jobs.setTable("p1", jobId, "missing", status="loaded")
    with pytest.raises(ValueError):
        jobs.setKpis("p1", jobId, "bogus")
    with pytest.raises(ValueError):
        jobs.finish("p1", jobId, "running")


def test_staleJobCannotTouchANewerJob():
    jobs, redis = make()
    jobA = jobs.start("p1", TABLES)
    del redis.values["import:p1:running"]  # A's lock ran out
    jobB = jobs.start("p1", TABLES)
    assert jobB and jobB != jobA
    redis.ttls["import:p1:running"] = 5
    before = dict(redis.values), dict(redis.ttls)
    assert jobs.setTable("p1", jobA, "orders", status="loaded", rows=1) is False
    assert jobs.setKpis("p1", jobA, "done") is False
    assert jobs.refreshLock("p1", jobA) is False
    assert jobs.finish("p1", jobA, "failed") is False
    assert (dict(redis.values), dict(redis.ttls)) == before
    assert jobs.get("p1")["state"] == "running"
    assert redis.values["import:p1:running"] == jobB.encode()
    assert jobs.setTable("p1", jobB, "orders", status="loaded", rows=1) is True


def test_missingRecordReturnsFalse():
    jobs, _ = make()
    assert jobs.setTable("p1", "j", "orders", status="loaded") is False
    assert jobs.setKpis("p1", "j", "done") is False
    assert jobs.finish("p1", "j", "done") is False
    assert jobs.refreshLock("p1", "j") is False


def test_getReportsFailedWhenWorkerDiedAndLockExpired():
    jobs, redis = make()
    jobs.start("p1", TABLES)
    del redis.values["import:p1:running"]
    assert jobs.get("p1")["state"] == "failed"
    assert json.loads(redis.values["import:p1"])["state"] == "running"


@pytest.mark.parametrize("kpis, reported", [("waiting", "failed"), ("running", "failed"), ("done", "done"), ("skipped", "skipped")])
def test_lockExpiredJobReportsUnfinishedKpisFailed(kpis, reported):
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    jobs.setKpis("p1", jobId, kpis)
    del redis.values["import:p1:running"]
    assert (jobs.get("p1")["state"], jobs.get("p1")["kpis"]) == ("failed", reported)
    assert json.loads(redis.values["import:p1"])["kpis"] == kpis


def test_finishedJobKeepsItsKpis():
    jobs, _ = make()
    jobId = jobs.start("p1", TABLES)
    jobs.finish("p1", jobId, "done")
    assert jobs.get("p1")["kpis"] == "waiting"


def test_forgetRemovesEveryKeyOfTheProject():
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    jobs.start("p2", TABLES)
    redis.set("import:p1:batch", f"{jobId}:0")
    redis.set("import:p1:next", f"{jobId}:8")
    jobs.forget("p1")
    assert sorted(redis.values) == ["import:p2", "import:p2:running"]
    assert jobs.get("p1") is None
    assert (jobs.setTable("p1", jobId, "orders", status="loaded"), jobs.refreshLock("p1", jobId)) == (False, False)


def test_getUnknownProjectIsNone():
    jobs, _ = make()
    assert jobs.get("nope") is None


def test_refreshLockExtendsOnlyTheOwnersLock():
    jobs, redis = make()
    jobId = jobs.start("p1", TABLES)
    redis.ttls["import:p1:running"] = 5
    assert jobs.refreshLock("p1", jobId) is True
    assert redis.ttls["import:p1:running"] == 21600


def test_defaultClientIsBuiltLazilyFromEnv(monkeypatch):
    for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    created = []
    fake = FakeRedis()
    monkeypatch.setattr(module.redis, "Redis", lambda **kw: created.append(kw) or fake)
    jobs = ImportJobs()  # needs no env
    assert created == []
    monkeypatch.setenv("REDIS_HOST", "redis.invalid")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_PASSWORD", "pw")
    assert jobs.get("p1") is None
    jobs.get("p1")
    assert created == [{"host": "redis.invalid", "port": 6380, "password": "pw"}]
