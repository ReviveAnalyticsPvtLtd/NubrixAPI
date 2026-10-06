"""Regression tests for review findings C1/C2/C4/I1/I4/I6/I7/M1/M2/M3/M5.

Each test reproduces a material finding from the whole-branch review,
watched RED, then fixed GREEN.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# --- C1: billing engine must accept monthly_prepaid ---------------------------


def test_billing_engine_accepts_monthly_prepaid(monkeypatch):
    from api.services.billing import billingEngine

    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", "plan_pro")
    monthlyPrice = {"amount": 100000, "currency": "INR"}
    monkeypatch.setattr(
        billingEngine, "_getMonthlyBasePrice", lambda: monthlyPrice
    )
    snapshot = billingEngine.computeInvoiceSnapshot(
        billingMode="monthly_prepaid",
        billingReason="initial_purchase",
        domainCount=1,
        customerState=None,
    )
    assert snapshot.total_amount >= 100000
    assert snapshot.billing_mode == "monthly_prepaid"


def test_billing_engine_accepts_monthly_prepaid_renewal_and_proration(monkeypatch):
    from api.services.billing import billingEngine

    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", "plan_pro")
    monthlyPrice = {"amount": 100000, "currency": "INR"}
    monkeypatch.setattr(
        billingEngine, "_getMonthlyBasePrice", lambda: monthlyPrice
    )
    start = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    end = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    renewal = billingEngine.computeInvoiceSnapshot(
        billingMode="monthly_prepaid",
        billingReason="renewal",
        domainCount=2,
        customerState=None,
        periodStart=start,
        periodEnd=end,
    )
    assert renewal.amount_before_tax == 200000
    proration = billingEngine.computeInvoiceSnapshot(
        billingMode="monthly_prepaid",
        billingReason="proration",
        domainCount=1,
        customerState=None,
        prorationAnchorStart=start,
        prorationAnchorEnd=end,
    )
    assert proration.amount_before_tax > 0


# --- C2: activateFreeTrial must not pass removed kwargs -----------------------


def test_activate_free_trial_writes_no_removed_columns():
    source = Path(
        "api/services/subscriptions/subscriptionService.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    # No stale recurring-provider kwargs survive in any upsert call site.
    for stale in ("recurringfailures=", "razorpaycustomerid=", "razorpaytokenid=", "subscriptionanchorday="):
        assert stale not in lowered, f"stale kwarg {stale} still passed"


def test_free_trial_upsert_signature_matches_call():
    from api.services.subscriptions.subscriptionService import SubscriptionService

    service = SubscriptionService.__new__(SubscriptionService)
    # The default _upsertCanonicalSubscription signature must accept the
    # exact kwargs activateFreeTrial passes.
    import inspect

    signature = inspect.signature(
        SubscriptionService._upsertCanonicalSubscription
    )
    accepted = set(signature.parameters)
    source = Path(
        "api/services/subscriptions/subscriptionService.py"
    ).read_text(encoding="utf-8")
    trialBlock = source[source.index("def activateFreeTrial"):source.index("def createSubscription")]
    passed = set()
    for keyword in (
        "userId", "billingMode", "status", "currentPeriodStart",
        "currentPeriodEnd", "renewalDueAt", "autoRenewEnabled",
        "paymentCollectionMode", "subscribedExperts", "domainCount",
        "pendingRemovals", "pendingAdditions", "recurringFailures",
        "billingState", "planType",
    ):
        if f"{keyword}=" in trialBlock:
            passed.add(keyword)
    assert passed <= accepted, f"activateFreeTrial passes unknown kwargs: {passed - accepted}"


# --- I4: webhook must process manual_renewal + initial_subscription --------------


def test_webhook_handles_manual_renewal_capture():
    source = Path("api/services/webhookService.py").read_text(encoding="utf-8")
    assert '"manual_renewal"' in source, (
        "payment.captured must process manual_renewal payments as a "
        "webhook backup for verifyRenewalPayment"
    )


def test_webhook_handles_initial_subscription_capture():
    source = Path("api/services/webhookService.py").read_text(encoding="utf-8")
    assert '"initial_subscription"' in source, (
        "payment.captured must recover captured initial purchases when the "
        "browser never verifies"
    )


# --- I7: renewal session must reject non-current-cycle invoices ----------------


def test_renewal_session_validates_current_cycle_only():
    source = Path(
        "api/services/subscriptions/subscriptionService.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("def createRenewalPaymentSession"):]
    block = block[:block.index("def verifyRenewalPayment")]
    assert 'billing_reason' in block, (
        "renewal session must check the invoice is a renewal invoice"
    )


# --- I6: verifyRenewalPayment must honour proven capture time -------------------


from test.test_manual_billing_runtime import database, seed_payment, read_row, NOW


def test_provider_attested_earlier_capture_can_finalize_after_local_ttl(database):
    from dataclasses import replace
    from datetime import timedelta
    repository,path=database
    evidence=seed_payment(database)
    result=repository.finalizeCapturedPayment(replace(evidence,observedAt=NOW+timedelta(hours=1),provenCaptureAt=NOW,timingVerified=True))
    assert result.finalized and result.currentPeriod.start==NOW


def test_ambiguous_capture_after_local_ttl_is_tracked_without_access(database):
    from dataclasses import replace
    from datetime import timedelta
    repository,path=database
    evidence=seed_payment(database)
    result=repository.finalizeCapturedPayment(replace(evidence,observedAt=NOW+timedelta(hours=1),provenCaptureAt=None,timingVerified=False))
    assert result.state=="requires_reconciliation" and result.anomalyId
    assert read_row(path,"credit_balances") is None


# --- C4/M1: scheduler + resume must persist prepared invoices -------------------


def test_monthly_task_persists_prepared_invoices():
    source = Path("nubrix/triggers/tasks/monthlyRenewalTask.py").read_text(
        encoding="utf-8"
    )
    assert "_persistMonthlyRenewalInvoice" in source or (
        "prepareRenewalInvoice" in source and "self.client.table" in source
    ), "the hourly task must persist prepared renewal invoices to the Invoices table"


def test_resume_persists_fresh_revision():
    source = Path(
        "api/services/subscriptions/subscriptionService.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("def resumeRenewal"):]
    assert "prepareRenewalInvoice" in block


# --- I2: paidFutureEnd must be written when an early renewal is paid -------------




# --- M3: paid-future checks must exclude revoked coverage ------------------------


def test_remove_domain_revoked_coverage_does_not_block():
    source = Path(
        "api/services/subscriptions/subscriptionService.py"
    ).read_text(encoding="utf-8")
    block = source[source.index("def removeDomain"):]
    block = block[:block.index("def cancelPendingAddition")]
    assert "revoked" in block or "coverageState" in block, (
        "the paid-future immutability check must ignore revoked (refunded) "
        "future coverage"
    )


# --- M5: stale recurring references cleaned --------------------------------------


def test_churn_reset_docstring_has_no_stale_columns():
    source = Path(
        "api/services/subscriptions/subscriptionFieldUtils.py"
    ).read_text(encoding="utf-8")
    assert "razorpay_customer_id" not in source


def test_subscription_manager_default_mode_is_manual():
    source = Path("nubrix/components/subscriptionManager.py").read_text(
        encoding="utf-8"
    )
    assert '"monthly_recurring"' not in source, (
        "the daily manager must not treat monthly_recurring as a live mode"
    )


# --- I1: admin API test fixture must match the contracted response ---------------


def test_admin_api_subscription_fixture_is_contract_clean():
    fixture = Path("test/test_admin_api.py").read_text(encoding="utf-8")
    for dropped in (
        "recurring_failures",
        "razorpay_token_id",
        "razorpay_customer_id",
        "subscription_anchor_day",
    ):
        assert dropped not in fixture, (
            f"test_admin_api fixture still returns contracted column {dropped}"
        )


# --- C5: staff refund routes must use the durable repository + real provider ----


def test_refund_routes_use_durable_repository():
    source = Path("api/routers/billingAdmin.py").read_text(encoding="utf-8")
    assert "SubscriptionRefundService()" not in source, (
        "refund routes must not build the service with the "
        "in-memory store and fake provider"
    )


def test_refund_quote_intervals_include_payment_ids():
    source = Path("api/routers/billingAdmin.py").read_text(encoding="utf-8")
    block = source[source.index("def _loadPaidIntervalsForInvoices"):]
    assert "razorpayPaymentId" in block[:block.index("async def")], (
        "paid-interval resolution must select the provider payment id column"
    )