from copy import deepcopy
import fnmatch
import math
import pytest

from api.services.userErasureRepository import ERASURE_STEP_NAMES


class FakeRepository:
    def __init__(self, failedStep=None, retryScheduled=False):
        self.request = {
            "id": "request-1",
            "target_user_id": "user-1",
            "status": "PENDING",
            "steps": [
                {"step_name": name, "status": "PENDING", "attempt_count": 0}
                for name in ERASURE_STEP_NAMES
            ],
        }
        self.failedStep = failedStep
        self.retryScheduled = retryScheduled
        self.calls = []
        self.scrubbed = False

    def claimRequest(self, requestId, workerId, leaseSeconds):
        self.calls.append(("claim", requestId))
        if self.request["status"] == "COMPLETED":
            return None
        self.request["status"] = "IN_PROGRESS"
        return deepcopy(self.request)

    def inventory(self, userId):
        self.calls.append(("inventory-data", userId))
        return {
            "projectIds": ["project-1"],
            "workspaceIds": ["workspace-1"],
            "billingCredentials": [
                {"customerId": "customer-1", "tokenId": "token-1"}
            ],
        }

    def startStep(self, requestId, stepName):
        self.calls.append(("start", stepName))
        for step in self.request["steps"]:
            if step["step_name"] == stepName:
                step["attempt_count"] += 1
                step["status"] = "IN_PROGRESS"

    def completeStep(self, requestId, stepName, details=None, status="COMPLETED"):
        self.calls.append(("complete", stepName))
        for step in self.request["steps"]:
            if step["step_name"] == stepName:
                step["status"] = status

    def failStep(self, requestId, stepName, errorCode, maxAttempts):
        self.calls.append(("failed", stepName, errorCode))
        self.request["status"] = "PARTIALLY_FAILED"
        return self.retryScheduled

    def freezeBilling(self, userId):
        self.calls.append(("billing", userId))

    def saveInventory(self, requestId, projectIds, workspaceIds):
        self.calls.append(("save-inventory", len(projectIds), len(workspaceIds)))
        self.request["resource_manifest"] = {
            "projectIds": list(projectIds),
            "workspaceIds": list(workspaceIds),
        }

    def deleteDatabaseData(self, requestId, userId, projectIds):
        self.calls.append(("database", userId))

    def cancelPendingTrialCreditSync(self, userId):
        self.calls.append(("cancel-trial-credit-sync", userId))
        return 1

    def verifyDatabaseErasure(self, userId):
        self.calls.append(("verify-db", userId))
        return {}

    def finalizeRequest(self, requestId, details):
        self.calls.append(("finalize", requestId))
        self.scrubbed = True
        self.request["target_user_id"] = None
        self.request["status"] = "COMPLETED"
        self.request["steps"][-1]["status"] = "COMPLETED"

    def listClaimable(self, limit):
        return ["request-1"]


class FakeExternalCleanup:
    def __init__(self, failStep=None):
        self.failStep = failStep
        self.calls = []

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.failStep == name:
            raise RuntimeError("provider secret must not be persisted")

    def revokeAccess(self, userId):
        self._call("revoke_access", userId)

    def deleteStorage(self, userId, projectIds):
        self._call("delete_storage", userId, tuple(projectIds))
        return {"objectsDeleted": 3}

    def stopBilling(self, billingCredentials):
        self._call("stop_billing", len(billingCredentials))
        return {"providerTokensDeleted": len(billingCredentials)}

    def deleteTransientState(self, userId, projectIds):
        self._call("delete_transient_state", userId, tuple(projectIds))
        return {"keysDeleted": 2}

    def deleteAuthIdentity(self, userId):
        self._call("delete_auth_identity", userId)

    def verifyExternalErasure(self, userId, projectIds):
        self._call("verify_external", userId, tuple(projectIds))
        return {}


class FakeAudit:
    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)


def _task(repository, cleanup, audit=None):
    from nubrix.triggers.tasks.userErasureTask import UserErasureTask

    return UserErasureTask(
        repository=repository,
        externalCleanup=cleanup,
        auditService=audit or FakeAudit(),
    )


def test_worker_runs_steps_in_order_and_scrubs_identity_only_after_verification():
    repository = FakeRepository()
    cleanup = FakeExternalCleanup()

    result = _task(repository, cleanup).execute("request-1")

    assert result == {"requestId": "request-1", "status": "COMPLETED"}
    assert repository.scrubbed is True
    completed = [call[1] for call in repository.calls if call[0] == "complete"]
    assert completed == list(ERASURE_STEP_NAMES[:-1])
    assert all(step["status"] == "COMPLETED" for step in repository.request["steps"])
    assert cleanup.calls[-1][0] == "verify_external"
    assert repository.calls[-1][0] == "finalize"
    assert ("cancel-trial-credit-sync", "user-1") in repository.calls
    assert [call[0] for call in cleanup.calls].count("delete_transient_state") == 2


def test_worker_records_stable_error_code_and_stops_after_transient_failure():
    repository = FakeRepository()
    cleanup = FakeExternalCleanup(failStep="delete_storage")

    result = _task(repository, cleanup).execute("request-1")

    assert result == {"requestId": "request-1", "status": "PARTIALLY_FAILED"}
    assert repository.scrubbed is False
    failure = [call for call in repository.calls if call[0] == "failed"][-1]
    assert failure == ("failed", "delete_storage", "STORAGE_DELETE_FAILED")
    assert "provider secret" not in str(failure)
    assert not any(call[0] == "database" for call in repository.calls)


def test_worker_audits_sanitized_completion():
    repository = FakeRepository()
    audit = FakeAudit()

    result = _task(repository, FakeExternalCleanup(), audit).execute("request-1")

    assert result == {"requestId": "request-1", "status": "COMPLETED"}
    assert audit.calls == [{
        "action": "user.erasure.complete",
        "targetType": "user_erasure_request",
        "targetId": "request-1",
        "changedFields": ["status", "target_user_id", "reason"],
        "outcome": "success",
        "actorEmail": "system",
        "details": {"status": "COMPLETED"},
    }]
    assert "user-1" not in repr(audit.calls)


def test_worker_audits_only_terminal_failure_with_safe_code():
    retryingAudit = FakeAudit()
    _task(
        FakeRepository(retryScheduled=True),
        FakeExternalCleanup(failStep="delete_storage"),
        retryingAudit,
    ).execute("request-1")
    assert retryingAudit.calls == []

    terminalAudit = FakeAudit()
    _task(
        FakeRepository(retryScheduled=False),
        FakeExternalCleanup(failStep="delete_storage"),
        terminalAudit,
    ).execute("request-1")
    assert terminalAudit.calls == [{
        "action": "user.erasure.failed",
        "targetType": "user_erasure_request",
        "targetId": "request-1",
        "changedFields": ["status", "last_error_code"],
        "outcome": "failed",
        "actorEmail": "system",
        "details": {
            "status": "PARTIALLY_FAILED",
            "step": "delete_storage",
            "errorCode": "STORAGE_DELETE_FAILED",
        },
    }]
    assert "provider secret" not in repr(terminalAudit.calls)


def test_worker_skips_completed_steps_when_resuming():
    repository = FakeRepository()
    repository.request["steps"][0]["status"] = "COMPLETED"
    cleanup = FakeExternalCleanup()

    _task(repository, cleanup).execute("request-1")

    assert not any(call[0] == "revoke_access" for call in cleanup.calls)
    assert repository.scrubbed is True


def test_worker_uses_durable_project_inventory_after_database_rows_are_gone():
    repository = FakeRepository()
    repository.request["resource_manifest"] = {
        "projectIds": ["project-before-delete"],
        "workspaceIds": ["workspace-before-delete"],
    }
    repository.inventory = lambda _userId: {
        "projectIds": [],
        "workspaceIds": [],
        "billingCredentials": [],
    }
    cleanup = FakeExternalCleanup()

    _task(repository, cleanup).execute("request-1")

    storageCall = next(call for call in cleanup.calls if call[0] == "delete_storage")
    assert storageCall[2] == ("project-before-delete",)


def test_sweep_queues_claimable_requests_without_exposing_identity():
    queued = []
    task = _task(FakeRepository(), FakeExternalCleanup())
    task.enqueue = queued.append

    result = task.sweep(limit=10)

    assert result == {"queued": 1}
    assert queued == ["request-1"]


def test_stop_billing_deletes_supported_razorpay_tokens_before_local_clear():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    deleted = []

    class TokenApi:
        def delete(self, customerId, tokenId):
            deleted.append((customerId, tokenId))

    class Razorpay:
        token = TokenApi()

    cleanup = UserErasureExternalCleanup(
        client=object(),
        redisClient=object(),
        cacheInvalidator=lambda _projectId: None,
        razorpayClient=Razorpay(),
    )

    result = cleanup.stopBilling(
        [{"customerId": "customer-1", "tokenId": "token-1"}]
    )

    assert result == {"providerTokensDeleted": 1}
    assert deleted == [("customer-1", "token-1")]


def test_storage_cleanup_paginates_and_removes_in_batches_of_at_most_1000():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Bucket:
        def __init__(self, paths):
            self.paths = set(paths)
            self.removeCalls = []

        def list(self, path="", options=None):
            prefix = f"{path}/" if path else ""
            names = sorted(
                value[len(prefix):]
                for value in self.paths
                if value.startswith(prefix) and "/" not in value[len(prefix):]
            )
            offset = (options or {}).get("offset", 0)
            limit = (options or {}).get("limit", 1000)
            return [
                {"name": name, "id": name, "metadata": {"size": 1}}
                for name in names[offset : offset + limit]
            ]

        def remove(self, paths):
            self.removeCalls.append(list(paths))
            self.paths.difference_update(paths)

    analytics = Bucket(
        [f"project-1/file-{index:04d}.parquet" for index in range(1001)]
    )
    profiles = Bucket(["user-1.png", "user-1.jpg", "user-2.png"])

    class Storage:
        def from_(self, name):
            return {"AnalyticsHub": analytics, "userProfileImages": profiles}[name]

    class Client:
        storage = Storage()

    cleanup = UserErasureExternalCleanup(
        client=Client(), redisClient=object(), cacheInvalidator=lambda _: None
    )

    result = cleanup.deleteStorage("user-1", ["project-1"])

    assert result == {"objectsDeleted": 1003}
    assert analytics.paths == set()
    assert profiles.paths == {"user-2.png"}
    assert all(len(batch) <= 1000 for batch in analytics.removeCalls)


def test_transient_cleanup_deletes_user_and_project_keys_with_one_scan():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Redis:
        def __init__(self):
            self.keys = {
                "credits:v3:user-1",
                "project-1::metadata",
                "semaphore:project-1",
                "transformation-preview:project-1:abc",
                "other-user-key",
            }
            self.scanCalls = 0
            self.deleteCalls = []
            self.unlinkCalls = []

        def scan(self, cursor=0, match=None, count=None):
            self.scanCalls += 1
            pattern = match or "*"
            return 0, sorted(
                key for key in self.keys if fnmatch.fnmatch(key, pattern)
            )

        def delete(self, *keys):
            self.deleteCalls.append(keys)
            self.keys.difference_update(keys)

        def unlink(self, *keys):
            self.unlinkCalls.append(keys)
            existing = sum(key in self.keys for key in keys)
            self.keys.difference_update(keys)
            return existing

        def exists(self, *keys):
            return sum(key in self.keys for key in keys)

    invalidated = []
    redis = Redis()
    cleanup = UserErasureExternalCleanup(
        client=object(), redisClient=redis, cacheInvalidator=invalidated.append
    )

    result = cleanup.deleteTransientState("user-1", ["project-1"])

    assert result == {"keysDeleted": 4}
    assert redis.keys == {"other-user-key"}
    assert invalidated == ["project-1"]
    assert redis.scanCalls == 1
    assert redis.deleteCalls == []
    assert len(redis.unlinkCalls) == 2
    assert set(redis.unlinkCalls[0]) == {
        "credits:v3:user-1",
        "semaphore:project-1",
    }
    assert set(redis.unlinkCalls[1]) == {
        "project-1::metadata",
        "transformation-preview:project-1:abc",
    }


def test_transient_cleanup_redis_client_bounds_network_wait(monkeypatch):
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    monkeypatch.setenv("USER_ERASURE_REDIS_SOCKET_TIMEOUT_SECONDS", "7")

    redis = UserErasureExternalCleanup._buildRedis()
    connectionOptions = redis.connection_pool.connection_kwargs

    assert connectionOptions["socket_connect_timeout"] == 7.0
    assert connectionOptions["socket_timeout"] == 7.0
    assert connectionOptions["health_check_interval"] == 30


@pytest.mark.parametrize(
    "timeoutSeconds",
    [0, -1, math.nan, math.inf, 300],
)
def test_transient_cleanup_rejects_deadlines_outside_worker_lease(
    timeoutSeconds,
):
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    with pytest.raises(
        ValueError,
        match="Redis cleanup timeout must be finite, positive, and shorter than lease",
    ):
        UserErasureExternalCleanup(
            client=object(),
            redisClient=object(),
            cacheInvalidator=lambda _: None,
            redisCleanupTimeoutSeconds=timeoutSeconds,
        )


def test_transient_cleanup_stops_when_its_deadline_is_exceeded():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Redis:
        def __init__(self):
            self.unlinkCalls = 0

        def scan(self, cursor=0, match=None, count=None):
            return 0, ["project-1::metadata"]

        def unlink(self, *keys):
            self.unlinkCalls += 1
            if self.unlinkCalls > 1:
                raise AssertionError("expired cleanup must not continue deleting")
            return 0

    times = iter([0.0, 0.5, 1.5])
    cleanup = UserErasureExternalCleanup(
        client=object(),
        redisClient=Redis(),
        cacheInvalidator=lambda _projectId: None,
        redisCleanupTimeoutSeconds=1,
        monotonic=lambda: next(times),
    )

    with pytest.raises(TimeoutError, match="Redis cleanup deadline exceeded"):
        cleanup.deleteTransientState("user-1", ["project-1"])


def test_transient_cleanup_checks_deadline_after_exact_key_verification():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Redis:
        def unlink(self, *keys):
            return 0

        def scan(self, cursor=0, count=None):
            return 0, []

        def exists(self, *keys):
            return 0

    times = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.1])
    cleanup = UserErasureExternalCleanup(
        client=object(),
        redisClient=Redis(),
        cacheInvalidator=lambda _: None,
        redisCleanupTimeoutSeconds=1,
        monotonic=lambda: next(times),
    )

    with pytest.raises(TimeoutError, match="Redis cleanup deadline exceeded"):
        cleanup.deleteTransientState("user-1", ["project-1"])


def test_transient_cleanup_unlinks_exact_keys_before_wildcard_scan():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Redis:
        def __init__(self):
            self.keys = {
                "credits:v3:user-1",
                "semaphore:project-1",
                "project-1::metadata",
            }

        def scan(self, cursor=0, match=None, count=None):
            raise TimeoutError("Redis scan timed out")

        def unlink(self, *keys):
            existing = sum(key in self.keys for key in keys)
            self.keys.difference_update(keys)
            return existing

    redis = Redis()
    cleanup = UserErasureExternalCleanup(
        client=object(), redisClient=redis, cacheInvalidator=lambda _: None
    )

    with pytest.raises(TimeoutError, match="Redis scan timed out"):
        cleanup.deleteTransientState("user-1", ["project-1"])

    assert redis.keys == {"project-1::metadata"}


def test_transient_cleanup_checks_deadline_between_empty_scan_batches():
    from nubrix.triggers.tasks.userErasureTask import UserErasureExternalCleanup

    class Redis:
        def __init__(self):
            self.scanCalls = 0

        def unlink(self, *keys):
            return 0

        def scan(self, cursor=0, count=None):
            self.scanCalls += 1
            return 1, []

    redis = Redis()
    times = iter([0.0, 0.1, 0.2, 1.1])
    cleanup = UserErasureExternalCleanup(
        client=object(),
        redisClient=redis,
        cacheInvalidator=lambda _: None,
        redisCleanupTimeoutSeconds=1,
        monotonic=lambda: next(times),
    )

    with pytest.raises(TimeoutError, match="Redis cleanup deadline exceeded"):
        cleanup.deleteTransientState("user-1", ["project-1"])

    assert redis.scanCalls == 1


def test_full_erasure_uses_two_redis_scans_regardless_of_project_count():
    from nubrix.triggers.tasks.userErasureTask import (
        UserErasureExternalCleanup,
        UserErasureTask,
    )

    class Redis:
        def __init__(self):
            self.keys = {
                "credits:v3:user-1",
                "project-1::metadata",
                "project-2::table",
                "transformation-preview:project-3:preview-1",
                "other-user-key",
            }
            self.scanCalls = 0
            self.scanIterCalls = 0

        def scan(self, cursor=0, count=None):
            self.scanCalls += 1
            return 0, sorted(self.keys)

        def scan_iter(self, match=None, count=None):
            self.scanIterCalls += 1
            pattern = match or "*"
            return iter(
                sorted(key for key in self.keys if fnmatch.fnmatch(key, pattern))
            )

        def unlink(self, *keys):
            existing = sum(key in self.keys for key in keys)
            self.keys.difference_update(keys)
            return existing

        def exists(self, *keys):
            return sum(key in self.keys for key in keys)

    class Query:
        def update(self, _values):
            return self

        def delete(self):
            return self

        def eq(self, _column, _value):
            return self

        def execute(self):
            return None

    class Bucket:
        def list(self, path="", options=None):
            return []

        def remove(self, _paths):
            return None

    class MissingAuthIdentity(Exception):
        status = 404

    class AuthAdmin:
        def update_user_by_id(self, _userId, _attributes):
            return None

        def delete_user(self, _userId):
            return None

        def get_user_by_id(self, _userId):
            raise MissingAuthIdentity("not found")

    class Client:
        class Storage:
            def from_(self, _name):
                return Bucket()

        class Auth:
            admin = AuthAdmin()

        storage = Storage()
        auth = Auth()

        def table(self, _name):
            return Query()

    repository = FakeRepository()
    repository.inventory = lambda _userId: {
        "projectIds": ["project-1", "project-2", "project-3"],
        "workspaceIds": [],
        "billingCredentials": [],
    }
    redis = Redis()
    cleanup = UserErasureExternalCleanup(
        client=Client(), redisClient=redis, cacheInvalidator=lambda _: None
    )

    result = UserErasureTask(
        repository=repository,
        externalCleanup=cleanup,
        auditService=FakeAudit(),
    ).execute("request-1")

    assert result == {"requestId": "request-1", "status": "COMPLETED"}
    assert redis.keys == {"other-user-key"}
    assert redis.scanCalls == 2
    assert redis.scanIterCalls == 0
