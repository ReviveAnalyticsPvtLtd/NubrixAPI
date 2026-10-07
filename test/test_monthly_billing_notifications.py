"""Monthly billing notification schedule + eligibility tests."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test.test_manual_billing_runtime import database, sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request, evidence


@pytest.mark.parametrize('mode',['monthly_prepaid','annual_prepaid'])
@pytest.mark.parametrize('purpose',['initial_purchase','renewal','expert_addition','topup'])
def test_all_eligible_captures_commit_one_payment_receipt(checkout_database,mode,purpose):
    manual,request=paid_then_request(checkout_database,mode,purpose)
    intent=manual.createCheckout(request)
    manual.finalizeCapturedPayment(evidence(intent,'receipt-payment'))
    manual.finalizeCapturedPayment(evidence(intent,'receipt-payment'))
    with sqlTransaction(checkout_database[1]) as connection:
        rows=connection.execute("SELECT metadata_json FROM billing_events WHERE idempotency_key='notification:receipt:receipt-payment'").fetchall()
    assert len(rows)==1
    import json
    receipt=json.loads(rows[0][0])
    assert receipt['dedupeKey']=='receipt:receipt-payment'
    assert receipt['metadata']['invoiceId']==intent.invoiceId
    assert receipt['metadata']['amount']==intent.amount

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.notifications.billingNotificationService import (  # noqa: E402
    buildBillingNotificationIntent,
    isBillingNotificationEligible,
)
from api.services.notifications import billingNotificationService as mod  # noqa: E402


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
_END = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize('kind',['monthly_renewal_ready','monthly_renewal_reminder'])
def test_dispatch_cannot_send_before_approved_milestone_window(kind):
    snapshot={'subscription':_subscription(),'invoice':{'status':'UPCOMING'}}
    delivery={'notification_type':kind,'period_end':_END.isoformat(),'metadata_json':{}}
    assert not isBillingNotificationEligible(delivery,snapshot,_NOW)


def _subscription(optOut=False, status="active", end=_END):
    return {
        "id": "sub-1",
        "user_id": "u1",
        "status": status,
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": end.isoformat(),
        "renewal_opt_out": optOut,
        "erasure_pending": False,
        "billing_state": {"manualBilling": {"lifecycleId": "lc-1"}},
    }


def _invoice(status="PAYMENT_PENDING"):
    return {
        "id": "inv-r1",
        "userId": "u1",
        "billing_reason": "renewal",
        "status": status,
        "period_start": "2026-10-20T10:00:00+00:00",
        "period_end": "2026-11-20T10:00:00+00:00",
        "metadata_json": {
            "manualBilling": {"lifecycleId": "lc-1", "coverageState": "estimated"}
        },
    }


# --- schedule milestones ------------------------------------------------------


def test_t7_invoice_ready_intent():
    now = datetime(2026, 10, 13, 10, tzinfo=timezone.utc)  # T-7 of Oct 20
    intent = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert intent is not None
    assert intent["notificationType"] == "monthly_renewal_ready"
    assert intent["dedupeKey"] == "monthly:lc-1:2026-10-20T10:00:00+00:00:ready"


def test_t1_reminder_intent():
    now = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)  # T-1
    intent = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_reminder"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert intent is not None
    assert intent["dedupeKey"] == "monthly:lc-1:2026-10-20T10:00:00+00:00:reminder"


def test_expiry_notice_intent_suppressed_for_opted_out():
    intent = buildBillingNotificationIntent(
        event={"type": "monthly_subscription_expired"},
        snapshot={
            "subscription": _subscription(optOut=True),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=_END,
    )
    assert intent is None


def test_expiry_notice_intent_for_unpaid_uncancelled():
    intent = buildBillingNotificationIntent(
        event={"type": "monthly_subscription_expired"},
        snapshot={
            "subscription": _subscription(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=_END,
    )
    assert intent is not None
    assert intent["dedupeKey"] == "monthly:lc-1:2026-10-20T10:00:00+00:00:expired"


def test_no_due_today_or_period_start_email_types_exist():
    # The registered types are exactly the audited set: no due-today, no
    # period-start, no failure, no win-back.
    assert "monthly_renewal_due_today" not in mod.SUPPORTED_BILLING_NOTIFICATION_TYPES
    assert "monthly_period_started" not in mod.SUPPORTED_BILLING_NOTIFICATION_TYPES
    assert "monthly_renewal_failed" not in mod.SUPPORTED_BILLING_NOTIFICATION_TYPES
    assert set(mod.SUPPORTED_BILLING_NOTIFICATION_TYPES) == {
        "monthly_renewal_ready",
        "monthly_renewal_reminder",
        "monthly_subscription_expired",
        "payment_receipt",
        "monthly_cancellation_confirmation",
        "subscription_refund_initiated",
        "subscription_refund_processed",
    }


def test_catch_up_sends_only_most_recent_milestone():
    # Down for T-7 and T-1 both; at T-1 only the reminder is sent.
    now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    ready = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert ready is None  # missed T-7 is suppressed by a later milestone


def test_no_unpaid_milestone_at_or_after_end():
    now = _END
    ready = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    reminder = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_reminder"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert ready is None
    assert reminder is None


def test_payment_suppresses_reminder_but_receipt_is_deliverable():
    paidInvoice = _invoice(status="PAID")
    reminder = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_reminder"},
        snapshot={
            "subscription": _subscription(),
            "invoice": paidInvoice,
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=datetime(2026, 10, 19, 10, tzinfo=timezone.utc),
    )
    assert reminder is None
    receipt = buildBillingNotificationIntent(
        event={"type": "payment_receipt", "paymentId": "pay_1", "purpose": "renewal"},
        snapshot={
            "subscription": _subscription(),
            "invoice": paidInvoice,
            "providerPaymentId": "pay_1",
        },
        now=datetime(2026, 10, 19, 10, tzinfo=timezone.utc),
    )
    assert receipt is not None
    assert receipt["dedupeKey"] == "receipt:pay_1"


# --- dispatch-time eligibility ------------------------------------------------


def test_dispatch_suppresses_reminder_after_opt_out():
    now = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    eligible = isBillingNotificationEligible(
        delivery={"notification_type": "monthly_renewal_reminder", "status": "PENDING", "period_end":"2026-10-20T10:00:00+00:00"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert eligible is True
    suppressed = isBillingNotificationEligible(
        delivery={"notification_type": "monthly_renewal_reminder", "status": "PENDING", "period_end":"2026-10-20T10:00:00+00:00"},
        snapshot={
            "subscription": _subscription(optOut=True),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert suppressed is False


def test_dispatch_suppresses_reminder_after_payment():
    now = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    eligible = isBillingNotificationEligible(
        delivery={"notification_type": "monthly_renewal_reminder", "status": "PENDING", "period_end":"2026-10-20T10:00:00+00:00"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(status="PAID"),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=now,
    )
    assert eligible is False


def test_receipt_remains_deliverable_after_expiry_or_cancellation():
    expiredSubscription = _subscription(status="expired", end=datetime(2026, 9, 20, 10, tzinfo=timezone.utc))
    eligible = isBillingNotificationEligible(
        delivery={"notification_type": "payment_receipt", "status": "PENDING"},
        snapshot={
            "subscription": expiredSubscription,
            "providerPaymentId": "pay_1",
        },
        now=_NOW,
    )
    assert eligible is True


def test_erasure_pending_suppresses_all_billing_emails():
    erasing = _subscription()
    erasing["erasure_pending"] = True
    assert buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={"subscription": erasing, "invoice": _invoice(), "cycleId": "c"},
        now=_NOW,
    ) is None
    assert isBillingNotificationEligible(
        delivery={"notification_type": "payment_receipt", "status": "PENDING"},
        snapshot={"subscription": erasing, "providerPaymentId": "pay_1"},
        now=_NOW,
    ) is False


def test_repricing_keeps_same_logical_identity():
    # Repricing creates a new invoice revision; the milestone identity is the
    # cycle, not the revision.
    first = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={
            "subscription": _subscription(),
            "invoice": _invoice(),
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
    )
    repricedInvoice = _invoice()
    repricedInvoice["id"] = "inv-r2"
    repricedInvoice["metadata_json"]["manualBilling"]["revision"] = 2
    second = buildBillingNotificationIntent(
        event={"type": "monthly_renewal_ready"},
        snapshot={
            "subscription": _subscription(),
            "invoice": repricedInvoice,
            "cycleId": "2026-10-20T10:00:00+00:00",
        },
        now=datetime(2026, 10, 13, 10, tzinfo=timezone.utc),
    )
    assert first["dedupeKey"] == second["dedupeKey"]
