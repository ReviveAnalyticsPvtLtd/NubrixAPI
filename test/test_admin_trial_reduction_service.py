import importlib
import importlib.util
from datetime import datetime, timezone

import pytest

from api.adminErrors import AdminApiError
from api.adminModels import AdminFreeTrialReductionRequest
from api.services.adminAuthService import AdminContext


NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
KEY = "9e3d768e-9f92-45f4-b816-2a9937ec97f8"
ADMIN = AdminContext(
    adminId="admin-1",
    email="admin@example.com",
    name="Admin",
    sessionId="session-1",
    token="token",
)


def serviceClass():
    moduleName = "api.services.adminTrialReductionService"
    assert importlib.util.find_spec(moduleName) is not None, (
        "trial-reduction service is missing"
    )
    return importlib.import_module(moduleName).AdminTrialReductionService


def request(days=3):
    return AdminFreeTrialReductionRequest(
        userId="free-user",
        days=days,
        reason="Abuse remediation",
        confirmation="REDUCE",
    )


def pendingReduction(userId="free-user"):
    return {
        "id": KEY,
        "idempotency_key": KEY,
        "request_hash": None,
        "user_id": userId,
        "outcome": "PENDING",
        "days_removed": None,
        "previous_expiry": None,
        "new_expiry": None,
        "access_still_banned": False,
        "error_code": None,
    }


def reducedReduction(userId="free-user"):
    return {
        **pendingReduction(userId),
        "outcome": "REDUCED",
        "days_removed": 3,
        "previous_expiry": datetime(
            2026, 9, 20, 10, 0, tzinfo=timezone.utc
        ),
        "new_expiry": datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
    }


def failedReduction(code="REDUCTION_WOULD_EXPIRE_TRIAL"):
    return {
        **pendingReduction(),
        "outcome": "FAILED",
        "error_code": code,
    }


class FakeRepository:
    def __init__(self):
        self.reduction = None
        self.reduceResult = None
        self.createCalls = []
        self.reduceCalls = []
        self.recordedFailures = []

    def createOrGetReduction(self, **kwargs):
        self.createCalls.append(kwargs)
        if self.reduction is None:
            self.reduction = pendingReduction(kwargs["userId"])
            self.reduction["request_hash"] = kwargs["requestHash"]
        return dict(self.reduction)

    def reduceUser(self, **kwargs):
        self.reduceCalls.append(kwargs)
        if isinstance(self.reduceResult, Exception):
            raise self.reduceResult
        self.reduction = dict(self.reduceResult)
        return dict(self.reduction)

    def recordFailure(self, reductionId, userId, errorCode):
        self.recordedFailures.append((reductionId, userId, errorCode))
        self.reduction = failedReduction(errorCode)
        return dict(self.reduction)


class FakeAuditService:
    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)


def buildService(repository, audit=None):
    return serviceClass()(
        repository=repository,
        auditService=audit or FakeAuditService(),
    )


def test_trial_reduction_returns_one_sanitized_result_and_audits_change():
    repository = FakeRepository()
    repository.reduceResult = reducedReduction()
    audit = FakeAuditService()

    result = buildService(repository, audit).reduce(request(), KEY, ADMIN)

    assert result == {
        "reductionId": KEY,
        "userId": "free-user",
        "outcome": "REDUCED",
        "daysRemoved": 3,
        "previousExpiry": "2026-09-20T10:00:00+00:00",
        "newExpiry": "2026-09-17T10:00:00+00:00",
        "accessStillBanned": False,
        "errorCode": None,
    }
    assert repository.reduceCalls == [{
        "reductionId": KEY,
        "userId": "free-user",
        "days": 3,
    }]
    assert audit.calls[0]["action"] == "free_trial.reduce"
    assert audit.calls[0]["changedFields"] == [
        "current_period_end",
        "renewal_due_at",
        "billing_state",
        "version",
    ]


def test_replay_returns_completed_reduction_without_removing_days_twice():
    repository = FakeRepository()
    payload = request()
    service = buildService(repository)
    repository.reduction = reducedReduction()
    repository.reduction["request_hash"] = service.requestHash(payload)

    result = service.reduce(payload, KEY, ADMIN)

    assert result["outcome"] == "REDUCED"
    assert repository.reduceCalls == []


def test_reused_idempotency_key_with_different_reduction_is_conflict():
    repository = FakeRepository()
    service = buildService(repository)
    repository.reduction = pendingReduction()
    repository.reduction["request_hash"] = service.requestHash(request(days=2))

    with pytest.raises(AdminApiError) as error:
        service.reduce(request(days=3), KEY, ADMIN)

    assert error.value.statusCode == 409


def test_reduction_database_failure_is_recorded_without_leaking_details():
    repository = FakeRepository()
    repository.reduceResult = RuntimeError("database password=secret")

    result = buildService(repository).reduce(request(), KEY, ADMIN)

    assert result["outcome"] == "FAILED"
    assert result["errorCode"] == "REDUCTION_FAILED"
    assert repository.recordedFailures == [
        (KEY, "free-user", "REDUCTION_FAILED")
    ]


def test_unpersisted_failure_returns_500_instead_of_claiming_durable_failure():
    repository = FakeRepository()
    repository.reduceResult = RuntimeError("primary write failed")

    def failRecord(reductionId, userId, errorCode):
        assert reductionId == KEY
        assert userId == "free-user"
        assert errorCode == "REDUCTION_FAILED"
        raise RuntimeError("failure ledger write failed")

    repository.recordFailure = failRecord

    with pytest.raises(AdminApiError) as error:
        buildService(repository).reduce(request(), KEY, ADMIN)

    assert error.value.statusCode == 500
    assert error.value.message == "Failed to reduce free trial"


def test_invalid_idempotency_key_is_rejected_before_persistence():
    repository = FakeRepository()

    with pytest.raises(AdminApiError) as error:
        buildService(repository).reduce(request(), "not-a-uuid", ADMIN)

    assert error.value.statusCode == 422
    assert repository.createCalls == []


def test_reduction_persistence_failure_returns_generic_admin_error():
    repository = FakeRepository()

    def failCreate(**_kwargs):
        raise RuntimeError("database password=secret")

    repository.createOrGetReduction = failCreate

    with pytest.raises(AdminApiError) as error:
        buildService(repository).reduce(request(), KEY, ADMIN)

    assert error.value.statusCode == 500
    assert error.value.message == "Failed to reduce free trial"


@pytest.mark.parametrize(
    ("statusCode", "message"),
    [(404, "User not found"), (409, "User erasure is in progress")],
)
def test_pre_admission_user_errors_are_propagated_without_a_ledger_receipt(
    statusCode, message
):
    repository = FakeRepository()

    def reject(**_kwargs):
        raise AdminApiError(statusCode, message)

    repository.createOrGetReduction = reject

    with pytest.raises(AdminApiError) as error:
        buildService(repository).reduce(request(), KEY, ADMIN)

    assert error.value.statusCode == statusCode
    assert error.value.message == message
    assert repository.reduceCalls == []
