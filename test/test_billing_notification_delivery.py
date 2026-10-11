"""Delivery integration tests: type extension, repository bridge, migration SQL."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.notifications.notificationDeliveryRepository import (  # noqa: E402
    _BILLING_NOTIFICATION_TEMPLATE_VERSIONS,
    NotificationDeliveryRepository,
)
from api.services.notifications import notificationDeliveryRepository as mod  # noqa: E402


def _migrationSql() -> str:
    matches = list(
        Path("supabase/migrations").glob("*_extend_monthly_notifications.sql")
    )
    assert len(matches) == 1
    return matches[0].read_text(encoding="utf-8")


def test_migration_extends_type_check_with_monthly_types():
    sql = _migrationSql()
    assert "monthly_renewal_ready" in sql
    assert "monthly_renewal_reminder" in sql
    assert "monthly_subscription_expired" in sql
    assert "payment_receipt" in sql
    assert "monthly_cancellation_confirmation" in sql
    assert "subscription_refund_initiated" in sql
    assert "subscription_refund_processed" in sql
    assert "trial_expiry_warning" in sql


def test_migration_does_not_add_due_today_or_winback_types():
    sql = _migrationSql().lower()
    for forbidden in (
        "due_today",
        "period_start",
        "win_back",
        "winback",
        "payment_failed_monthly",
    ):
        assert forbidden not in sql


def test_template_versions_cover_exactly_the_monthly_types():
    assert set(_BILLING_NOTIFICATION_TEMPLATE_VERSIONS) == {
        "monthly_renewal_ready",
        "monthly_renewal_reminder",
        "monthly_subscription_expired",
        "payment_receipt",
        "monthly_cancellation_confirmation",
        "subscription_refund_initiated",
        "subscription_refund_processed",
    }


def test_repository_enqueues_billing_intent_with_dedupe():
    repository = NotificationDeliveryRepository(connectionFactory=object)
    captured = {}

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params=None):
            captured.setdefault("queries", []).append((query, params))

        def fetchone(self):
            if not captured.get("rowReturned"):
                captured["rowReturned"] = True
                return None  # conflict path: simulate existing row lookup
            return {
                "id": "delivery-1",
                "notification_type": "monthly_renewal_ready",
                "dedupe_key": "monthly:lc-1:c:ready",
                "status": "PENDING",
            }

    class _Connection:
        def __init__(self):
            self.committed = 0
            self.rolledBack = 0

        def cursor(self, cursor_factory=None):
            return _Cursor()

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rolledBack += 1

        def close(self):
            pass

    connection = _Connection()
    repository.connectionFactory = lambda: connection
    row, created = repository.enqueueBillingNotification(
        userId="u1",
        subscriptionId="sub-1",
        notificationType="monthly_renewal_ready",
        dedupeKey="monthly:lc-1:c:ready",
        periodEnd="2026-10-20T10:00:00+00:00",
        metadata={"cycleId": "c"},
    )
    assert row["dedupe_key"] == "monthly:lc-1:c:ready"
    assert created is False  # conflict path returned the existing row
    assert connection.committed == 1
    insertQueries = [q for q, _ in captured["queries"] if "insert into" in q]
    assert insertQueries, "enqueue must insert through the outbox table"


def test_repository_rejects_unknown_billing_type():
    repository = NotificationDeliveryRepository(connectionFactory=lambda: None)
    with pytest.raises(ValueError):
        repository.enqueueBillingNotification(
            userId="u1",
            subscriptionId="sub-1",
            notificationType="monthly_renewal_due_today",
            dedupeKey="k",
            periodEnd="2026-10-20T10:00:00+00:00",
            metadata={},
        )