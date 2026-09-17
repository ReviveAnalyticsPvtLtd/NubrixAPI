from datetime import datetime, timezone

from api.services.notifications.trialExpiryNotificationService import (
    TrialExpiryNotificationService,
    buildTrialExpiryIntent,
)
from nubrix.triggers.tasks.subscriptionExpiryTask import SubscriptionExpiryTask


NOW = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)


def _subscription(days: int, **overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "user_id": "user-1",
        "status": "trial",
        "billing_mode": "none",
        "erasure_pending": False,
        "current_period_start": "2026-09-07T01:00:00+00:00",
        "current_period_end": f"2026-09-{17 + days:02d}T01:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakeRepository:
    def __init__(self, *, created=True):
        self.created = created
        self.calls = []

    def enqueueTrialExpiry(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "delivery-1", "status": "PENDING"}, self.created


class FakeEventLedger:
    def __init__(self):
        self.events = []

    def log_event(self, **kwargs):
        self.events.append(kwargs)


def test_oneAndTwoDayTrialsAreEligible():
    assert buildTrialExpiryIntent(_subscription(1), NOW) is not None
    assert buildTrialExpiryIntent(_subscription(2), NOW) is not None


def test_zeroThreePaidAndErasingSubscriptionsAreNotEligible():
    assert buildTrialExpiryIntent(_subscription(0), NOW) is None
    assert buildTrialExpiryIntent(_subscription(3), NOW) is None
    assert buildTrialExpiryIntent(_subscription(2, status="active"), NOW) is None
    assert (
        buildTrialExpiryIntent(
            _subscription(2, billing_mode="monthly_recurring"), NOW
        )
        is None
    )
    assert (
        buildTrialExpiryIntent(_subscription(2, erasure_pending=True), NOW)
        is None
    )


def test_dedupeKeyIncludesSubscriptionPeriodAndNoRecipientData():
    intent = buildTrialExpiryIntent(_subscription(2), NOW)

    assert intent["dedupeKey"] == (
        "trial_expiry_warning:v1:"
        "11111111-1111-1111-1111-111111111111:"
        "2026-09-19T01:00:00+00:00"
    )
    assert "email" not in intent
    assert "name" not in intent
    assert intent["metadata"] == {
        "trialStartDate": "2026-09-07T01:00:00+00:00"
    }


def test_enqueueEligiblePersistsTheDerivedIntent():
    repository = FakeRepository(created=True)
    ledger = FakeEventLedger()
    service = TrialExpiryNotificationService(
        repository=repository,
        eventService=ledger,
    )

    row, created = service.enqueueEligible(_subscription(2), NOW)

    assert row["id"] == "delivery-1"
    assert created is True
    assert repository.calls == [{
        "userId": "user-1",
        "subscriptionId": "11111111-1111-1111-1111-111111111111",
        "periodEnd": "2026-09-19T01:00:00+00:00",
        "dedupeKey": (
            "trial_expiry_warning:v1:"
            "11111111-1111-1111-1111-111111111111:"
            "2026-09-19T01:00:00+00:00"
        ),
        "metadata": {"trialStartDate": "2026-09-07T01:00:00+00:00"},
    }]
    assert ledger.events[0]["event_type"] == "email.expiry_warning.queued"
    assert ledger.events[0]["event_status"] == "PENDING"
    assert ledger.events[0]["idempotency_key"] == "delivery-1:PENDING:0"
    assert "email" not in ledger.events[0]["metadata"]


def test_duplicateIntentDoesNotWriteAnotherQueuedAudit():
    ledger = FakeEventLedger()
    service = TrialExpiryNotificationService(
        repository=FakeRepository(created=False),
        eventService=ledger,
    )

    _row, created = service.enqueueEligible(_subscription(2), NOW)

    assert created is False
    assert ledger.events == []


def test_subscriptionExpiryTaskReturnsAndAuditsSweepSummary():
    summary = {
        "scanned": 3,
        "expired": 1,
        "enqueued": 1,
        "duplicates": 0,
        "errors": 0,
    }
    ledger = FakeEventLedger()
    task = SubscriptionExpiryTask(
        client=object(),
        sweep=lambda: summary,
        eventServiceFactory=lambda _client: ledger,
    )

    result = task.execute()

    assert result == summary
    assert ledger.events == [{
        "user_id": "system",
        "event_type": "subscription.expiry_sweep.completed",
        "event_status": "COMPLETED",
        "category": "system",
        "metadata": summary,
        "idempotency_key": None,
    }]
