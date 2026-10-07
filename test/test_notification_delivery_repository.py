from datetime import datetime, timezone

import pytest

from api.services.notifications.notificationDeliveryRepository import (
    NotificationDeliveryRepository,
)


NOW = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = connection.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(query.lower().split())
        self.connection.executed.append((normalized, params))
        if self.connection.executeError is not None:
            raise self.connection.executeError

    def fetchone(self):
        if not self.connection.fetchoneResults:
            return None
        return self.connection.fetchoneResults.pop(0)

    def fetchall(self):
        if not self.connection.fetchallResults:
            return []
        return self.connection.fetchallResults.pop(0)


class FakeConnection:
    def __init__(
        self,
        *,
        fetchoneResults=None,
        fetchallResults=None,
        rowcount=1,
        executeError=None,
    ):
        self.fetchoneResults = list(fetchoneResults or [])
        self.fetchallResults = list(fetchallResults or [])
        self.rowcount = rowcount
        self.executeError = executeError
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **_kwargs):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def _repository(connection):
    return NotificationDeliveryRepository(connectionFactory=lambda: connection)


def test_enqueueReturnsExistingRowOnDedupeConflict():
    existing = {"id": "delivery-1", "status": "PENDING"}
    connection = FakeConnection(fetchoneResults=[None, existing])

    row, created = _repository(connection).enqueueTrialExpiry(
        "user-1",
        "11111111-1111-1111-1111-111111111111",
        "2026-09-19T00:00:00+00:00",
        (
            "trial_expiry_warning:v1:"
            "11111111-1111-1111-1111-111111111111:"
            "2026-09-19T00:00:00+00:00"
        ),
        {"trialStartDate": "2026-09-07T00:00:00+00:00"},
    )

    assert row == existing
    assert created is False
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1
    assert "on conflict (dedupe_key) do nothing" in connection.executed[0][0]
    assert "where dedupe_key = %s" in connection.executed[1][0]


def test_claimDueForwardsBoundedLeaseArgumentsAndReturnsRows():
    claimed = [{"id": "delivery-1", "status": "SENDING", "attempt_count": 1}]
    connection = FakeConnection(fetchallResults=[claimed])

    rows = _repository(connection).claimDue("worker-a", limit=25, leaseSeconds=180)

    assert rows == claimed
    query, params = connection.executed[0]
    assert "claim_notification_deliveries(%s, %s, %s)" in query
    assert params == ("worker-a", 25, 180)
    assert connection.commits == 1


def test_markAcceptedRequiresMatchingLeaseAndSchedulesReconciliation():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).markAccepted(
        "delivery-1", "worker-a", "message-1", NOW.isoformat()
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "status = 'accepted'" in query
    assert "next_reconcile_at = %s::timestamptz + interval '5 minutes'" in query
    assert "where id = %s and status = 'sending' and lease_owner = %s" in query
    assert params[-2:] == ("delivery-1", "worker-a")


def test_reconciliationSelectionDoesNotUseDispatchTimestamp():
    connection = FakeConnection(fetchallResults=[[{"id": "delivery-1"}]])

    rows = _repository(connection).listForReconciliation(limit=10)

    assert rows == [{"id": "delivery-1"}]
    query, params = connection.executed[0]
    assert "next_reconcile_at <= now()" in query
    assert "next_attempt_at <= now()" not in query
    assert params == (10,)


def test_ambiguousRowsRemainUnavailableForImmediateResend():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).scheduleRetry(
        "delivery-1",
        "worker-a",
        "AMBIGUOUS_SEND",
        "2026-09-17T01:30:00+00:00",
        "2026-09-17T01:05:00+00:00",
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "next_attempt_at = %s" in query
    assert "next_reconcile_at = %s" in query
    assert params[3:5] == (
        "2026-09-17T01:30:00+00:00",
        "2026-09-17T01:05:00+00:00",
    )


def test_cancelAndScrubClearsIdentifiersWithoutDeletingHistory():
    connection = FakeConnection(rowcount=2)

    changed = _repository(connection).cancelAndScrubUser("user-1")

    assert changed == 2
    query, params = connection.executed[0]
    assert "user_id = null" in query
    assert "subscription_id = null" in query
    assert "when status in ('pending', 'retry_pending', 'sending')" in query
    assert params == ("user-1",)


def test_terminalCleanupDeletesOnlyOldTerminalRows():
    connection = FakeConnection(rowcount=3)

    changed = _repository(connection).deleteTerminalBefore(
        "2026-06-19T01:00:00+00:00"
    )

    assert changed == 3
    query, params = connection.executed[0]
    for status in ("delivered", "bounced", "blocked", "failed", "cancelled"):
        assert f"'{status}'" in query
    assert "terminal_at < %s" in query
    assert params == ("2026-06-19T01:00:00+00:00",)


def test_markTerminalFromDispatchRequiresLeaseOwnerAndClearsSchedules():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).markTerminal(
        "delivery-1",
        "FAILED",
        errorCode="INVALID_RECIPIENT",
        providerStatus="invalid",
        leaseOwner="worker-a",
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "next_reconcile_at = null" in query
    assert "lease_owner = null" in query
    assert "where id = %s and status = 'sending' and lease_owner = %s" in query
    assert params[-2:] == ("delivery-1", "worker-a")


def test_listAmbiguousSelectsOnlyDueAmbiguousRows():
    connection = FakeConnection(fetchallResults=[[{"id": "delivery-1"}]])

    rows = _repository(connection).listAmbiguous(limit=5)

    assert rows == [{"id": "delivery-1"}]
    query, params = connection.executed[0]
    assert "status = 'retry_pending'" in query
    assert "last_error_code = 'ambiguous_send'" in query
    assert "next_reconcile_at <= now()" in query
    assert params == (5,)


def test_attachRecoveredMessageIdMakesAmbiguousRowNonDispatchable():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).attachRecoveredMessageId(
        "delivery-1", "message-1"
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "status = 'accepted'" in query
    assert "provider_message_id = %s" in query
    assert "where id = %s and status = 'retry_pending'" in query
    assert params == ("message-1", "delivery-1")


def test_recoverClaimedAmbiguousRequiresMatchingLease():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).recoverClaimedAmbiguous(
        "delivery-1",
        "worker-a",
        "message-1",
        NOW.isoformat(),
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "status = 'accepted'" in query
    assert "last_error_code = null" in query
    assert "status = 'sending' and lease_owner = %s" in query
    assert params[-2:] == ("delivery-1", "worker-a")


def test_advanceReconciliationNeverChangesDispatchTimestamp():
    connection = FakeConnection(rowcount=1)

    changed = _repository(connection).advanceReconciliation(
        "delivery-1",
        "deferred",
        "2026-09-17T01:05:00+00:00",
        "PROVIDER_DEFERRED",
    )

    assert changed is True
    query, params = connection.executed[0]
    assert "next_reconcile_at = %s" in query
    assert "next_attempt_at" not in query
    assert "status in ('accepted', 'retry_pending')" in query
    assert params == (
        "deferred",
        "PROVIDER_DEFERRED",
        "2026-09-17T01:05:00+00:00",
        "delivery-1",
    )


def test_collectHealthReturnsAggregateWithoutRecipientData():
    health = {
        "pending": 2,
        "retryPending": 1,
        "acceptedUnresolved": 1,
        "expiredLeases": 0,
        "delivered": 8,
        "terminalFailures": 2,
        "oldestPendingMinutes": 20,
    }
    connection = FakeConnection(fetchoneResults=[health])

    result = _repository(connection).collectHealth(NOW.isoformat())

    assert result == health
    query, params = connection.executed[0]
    assert "count(*) filter" in query
    assert "email" not in query
    assert "next_attempt_at <= %s" in query
    assert params == (NOW.isoformat(), NOW.isoformat(), NOW.isoformat())


def test_validateClaimCapabilityChecksFunctionAndExecutePrivilege():
    connection = FakeConnection(fetchoneResults=[{"available": True}])

    _repository(connection).validateClaimCapability()

    query, params = connection.executed[0]
    assert "to_regprocedure" in query
    assert "has_function_privilege" in query
    assert params is None


def test_writeFailureRollsBackAndClosesConnection():
    connection = FakeConnection(executeError=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        _repository(connection).cancelAndScrubUser("user-1")

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed == 1
