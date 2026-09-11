import importlib
import importlib.util
from datetime import datetime, timezone

import pytest

from api.adminErrors import AdminApiError


NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
KEY = "9e3d768e-9f92-45f4-b816-2a9937ec97f8"


def repositoryClass():
    moduleName = "api.services.adminTrialReductionRepository"
    assert importlib.util.find_spec(moduleName) is not None, (
        "trial-reduction repository is missing"
    )
    return importlib.import_module(moduleName).AdminTrialReductionRepository


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.executions.append((" ".join(query.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, rows):
        self.fakeCursor = FakeCursor(rows)
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **_kwargs):
        return self.fakeCursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def subscription(**overrides):
    row = {
        "id": "subscription-1",
        "billing_mode": "none",
        "plan_type": "free",
        "status": "trial",
        "current_period_start": datetime(
            2026, 9, 1, 10, 0, tzinfo=timezone.utc
        ),
        "current_period_end": datetime(
            2026, 9, 20, 10, 0, tzinfo=timezone.utc
        ),
        "erasure_pending": False,
        "version": 3,
    }
    row.update(overrides)
    return row


def reduction(**overrides):
    row = {
        "id": "reduction-1",
        "idempotency_key": KEY,
        "request_hash": "a" * 64,
        "user_id": "free-user",
        "subscription_id": None,
        "requested_by": "admin-1",
        "days": 3,
        "reason": "Abuse remediation",
        "outcome": "PENDING",
        "days_removed": None,
        "previous_expiry": None,
        "new_expiry": None,
        "access_still_banned": False,
        "error_code": None,
        "created_at": NOW,
        "updated_at": NOW,
        "completed_at": None,
    }
    row.update(overrides)
    return row


def statements(connection):
    return " ".join(
        query.lower() for query, _params in connection.fakeCursor.executions
    )


def test_create_reduction_persists_one_idempotent_operation():
    stored = reduction()
    connection = FakeConnection([
        None,
        {"user_id": "free-user"},
        {"erasure_pending": False},
        stored,
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.createOrGetReduction(
        idempotencyKey=KEY,
        requestHash="a" * 64,
        userId="free-user",
        days=3,
        reason="Abuse remediation",
        adminId="admin-1",
    )

    statementList = [
        query.lower() for query, _params in connection.fakeCursor.executions
    ]
    advisoryIndex = next(
        index for index, query in enumerate(statementList)
        if "pg_advisory_xact_lock" in query
    )
    insertIndex = next(
        index for index, query in enumerate(statementList)
        if "insert into public.admin_free_trial_reductions" in query
    )
    assert advisoryIndex < insertIndex
    assert any('from public."users"' in query for query in statementList)
    assert any(
        "from public.subscriptions" in query
        and "order by updated_at desc, id desc" in query
        for query in statementList
    )
    assert result == stored
    assert connection.commits == 1


def test_create_reduction_rejects_erasure_before_inserting_ledger():
    connection = FakeConnection([
        None,
        {"user_id": "free-user"},
        {"erasure_pending": True},
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    with pytest.raises(AdminApiError) as error:
        repository.createOrGetReduction(
            idempotencyKey=KEY,
            requestHash="a" * 64,
            userId="free-user",
            days=3,
            reason="Abuse remediation",
            adminId="admin-1",
        )

    assert error.value.statusCode == 409
    assert "insert into public.admin_free_trial_reductions" not in statements(
        connection
    )
    assert connection.rollbacks == 1


def test_reduce_user_changes_only_expiry_lifecycle_and_version():
    completed = reduction(
        subscription_id="subscription-1",
        outcome="REDUCED",
        days_removed=3,
        previous_expiry=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        new_expiry=datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc),
        completed_at=NOW,
    )
    connection = FakeConnection([
        reduction(),
        {"is_banned": False},
        subscription(),
        {"current_time": NOW},
        completed,
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.reduceUser(
        reductionId="reduction-1", userId="free-user", days=3
    )

    queryText = statements(connection)
    updateQuery, updateParams = next(
        (query, params)
        for query, params in connection.fakeCursor.executions
        if "update public.subscriptions" in query.lower()
    )
    assert "pg_advisory_xact_lock(hashtextextended(%s, 0))" in queryText
    assert "for update" in queryText
    assert "current_period_end = %s" in updateQuery.lower()
    assert "renewal_due_at = %s" in updateQuery.lower()
    assert "current_period_start =" not in updateQuery.lower()
    assert "insert into public.credit_balances" not in queryText
    assert updateParams[0] == datetime(
        2026, 9, 17, 10, 0, tzinfo=timezone.utc
    )
    assert updateParams[1] == updateParams[0]
    assert result == completed
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_reduction_that_would_end_trial_is_durably_rejected():
    failed = reduction(
        outcome="FAILED",
        error_code="REDUCTION_WOULD_EXPIRE_TRIAL",
        completed_at=NOW,
    )
    connection = FakeConnection([
        reduction(),
        {"is_banned": False},
        subscription(
            current_period_end=datetime(
                2026, 9, 13, 9, 59, tzinfo=timezone.utc
            )
        ),
        {"current_time": NOW},
        failed,
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.reduceUser(
        reductionId="reduction-1", userId="free-user", days=2
    )

    assert result["error_code"] == "REDUCTION_WOULD_EXPIRE_TRIAL"
    assert "update public.subscriptions" not in statements(connection)
    assert connection.commits == 1


def test_expired_trial_cannot_be_reduced_or_reactivated():
    failed = reduction(
        outcome="FAILED",
        error_code="FREE_TRIAL_NOT_ACTIVE",
        completed_at=NOW,
    )
    connection = FakeConnection([
        reduction(),
        {"is_banned": False},
        subscription(
            status="expired",
            current_period_end=datetime(
                2026, 9, 10, 10, 0, tzinfo=timezone.utc
            ),
        ),
        failed,
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.reduceUser(
        reductionId="reduction-1", userId="free-user", days=1
    )

    assert result["error_code"] == "FREE_TRIAL_NOT_ACTIVE"
    assert "update public.subscriptions" not in statements(connection)


def test_paid_and_erasure_pending_subscriptions_are_ineligible():
    repository = repositoryClass()(connectionFactory=lambda: None)

    assert repository.eligibilityError(
        subscription(billing_mode="monthly_recurring", plan_type="pro")
    ) == "PAID_SUBSCRIPTION_NOT_ELIGIBLE"
    assert repository.eligibilityError(
        subscription(erasure_pending=True)
    ) == "USER_ERASURE_PENDING"


def test_completed_reduction_is_replayed_without_updating_subscription():
    completed = reduction(outcome="REDUCED", days_removed=3, completed_at=NOW)
    connection = FakeConnection([completed])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.reduceUser(
        reductionId="reduction-1", userId="free-user", days=3
    )

    assert result == completed
    assert "update public.subscriptions" not in statements(connection)
    assert connection.commits == 1


def test_unexpected_failure_receipt_reads_current_ban_state():
    failed = reduction(
        outcome="FAILED",
        access_still_banned=True,
        error_code="REDUCTION_FAILED",
        completed_at=NOW,
    )
    connection = FakeConnection([failed])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.recordFailure(
        reductionId="reduction-1",
        userId="free-user",
        errorCode="REDUCTION_FAILED",
    )

    updateQuery, updateParams = next(
        (query, params)
        for query, params in connection.fakeCursor.executions
        if "update public.admin_free_trial_reductions" in query.lower()
    )
    assert "pg_advisory_xact_lock" in statements(connection)
    assert 'select "isbanned" from public."users"' in updateQuery.lower()
    assert updateParams == (
        "free-user",
        "REDUCTION_FAILED",
        "reduction-1",
    )
    assert result["access_still_banned"] is True


def test_lock_time_clock_prevents_stale_request_time_from_expiring_trial():
    lockTime = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    failed = reduction(
        outcome="FAILED",
        error_code="REDUCTION_WOULD_EXPIRE_TRIAL",
        completed_at=lockTime,
    )
    connection = FakeConnection([
        reduction(),
        {"is_banned": False},
        subscription(
            current_period_end=datetime(
                2026, 9, 15, 10, 0, tzinfo=timezone.utc
            )
        ),
        {"current_time": lockTime},
        failed,
    ])
    repository = repositoryClass()(connectionFactory=lambda: connection)

    result = repository.reduceUser(
        reductionId="reduction-1", userId="free-user", days=2
    )

    assert result["error_code"] == "REDUCTION_WOULD_EXPIRE_TRIAL"
    assert "update public.subscriptions" not in statements(connection)
    clockIndex = next(
        index
        for index, (query, _params) in enumerate(
            connection.fakeCursor.executions
        )
        if "clock_timestamp()" in query.lower()
    )
    subscriptionLockIndex = next(
        index
        for index, (query, _params) in enumerate(
            connection.fakeCursor.executions
        )
        if "from public.subscriptions" in query.lower()
        and "for update" in query.lower()
    )
    assert subscriptionLockIndex < clockIndex
