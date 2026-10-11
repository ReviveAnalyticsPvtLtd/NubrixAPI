"""Monthly cancellation + resumeRenewal behaviour tests."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from test.test_manual_billing_runtime import database, NOW, USER, read_row
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request, evidence


def test_proven_capture_before_optout_preserves_future_coverage(checkout_database):
    repository, path = checkout_database
    service, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    intent = service.createCheckout(checkout)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(minutes=1)):
        repository.setRenewalOptOut(USER, True, 'finished', 'cancel')
    result = repository.finalizeCapturedPayment(replace(evidence(intent), observedAt=NOW+timedelta(minutes=2)))
    assert result.finalized
    assert result.nextPeriod.start.isoformat() == intent.snapshot['periodStart']
    assert result.renewalOptOut and not result.creditsRefilled


@pytest.mark.parametrize('offset,verified', [(60,True), (61,True), (0,False)])
def test_optout_at_or_after_cutoff_and_unproven_capture_is_audited(checkout_database, offset, verified):
    repository, path = checkout_database
    service, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    intent = service.createCheckout(checkout)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW+timedelta(seconds=60)):
        repository.setRenewalOptOut(USER, True, None, 'cancel')
    result = repository.finalizeCapturedPayment(replace(evidence(intent), provenCaptureAt=NOW+timedelta(seconds=offset),
        timingVerified=verified, observedAt=NOW+timedelta(minutes=2)))
    assert result.state == 'requires_reconciliation' and result.anomalyId

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.monthlyCoverageService import (  # noqa: E402
    MonthlyCoverageService,
)
from api.services.billing.manualBillingRepository import (  # noqa: E402
    ManualBillingRepository,
)


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)


def _subscription(**overrides):
    row = {
        "id": "sub-1",
        "user_id": "u1",
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": "2026-10-20T10:00:00+00:00",
        "subscribed_experts": ["banking", "manufacturing"],
        "pending_removals": [],
        "pending_additions": [],
        "billing_state": {"manualBilling": {"lifecycleId": "lc-1"}},
        "renewal_opt_out": False,
        "cancellation_reason": None,
    }
    row.update(overrides)
    return row


class _OptOutStore:
    def __init__(self):
        self.subscriptions = {}
        self.voidedInvoices = []
        self.audit = []

    def applyOptOut(self, userId, optOut, reason, requestKey):
        row = self.subscriptions.setdefault(
            userId,
            {"renewal_opt_out": False, "cancellation_reason": None},
        )
        row["renewal_opt_out"] = optOut
        row["cancellation_reason"] = reason if optOut else None
        return dict(row)

    def voidUnpaidRenewalInvoices(self, userId, reason):
        voided = []
        for invoice in self.voidedInvoices:
            if invoice.get("userId") == userId and invoice.get("status") in (
                "UPCOMING",
                "PAYMENT_PENDING",
            ):
                invoice["status"] = "VOID"
                invoice["voidReason"] = reason
                voided.append(invoice["id"])
        return voided


def test_monthly_cancellation_optional_reason():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    # No reason provided: cancellation must still succeed.
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=_subscription(),
        reason=None,
        requestKey="cancel-1",
    )
    assert result["renewalOptOut"] is True
    assert result["cancellationReason"] is None


def test_monthly_cancellation_with_reason_preserves_it():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=_subscription(),
        reason="  too expensive  ",
        requestKey="cancel-2",
    )
    assert result["renewalOptOut"] is True
    assert result["cancellationReason"] == "too expensive"


def test_cancellation_effective_end_is_current_period_end():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=_subscription(),
        reason=None,
        requestKey="cancel-3",
    )
    assert result["effectiveAt"] == "2026-10-20T10:00:00+00:00"
    # paid coverage preserved: current experts untouched
    assert result["currentPeriod"]["domains"] == ["banking", "manufacturing"]


def test_cancellation_after_paid_future_extends_effective_end():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    subscription = _subscription()
    subscription["billing_state"]["manualBilling"]["paidFutureEnd"] = "2026-11-20T10:00:00+00:00"
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=subscription,
        reason=None,
        requestKey="cancel-4",
    )
    assert result["effectiveAt"] == "2026-11-20T10:00:00+00:00"


def test_repeated_cancellation_is_noop():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    alreadyCancelled = _subscription(renewal_opt_out=True, cancellation_reason="x")
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=alreadyCancelled,
        reason=None,
        requestKey="cancel-5",
    )
    assert result["renewalOptOut"] is True
    assert result["repeated"] is True


def test_cancellation_voids_unpaid_renewal_invoice():
    store = _OptOutStore()
    service = MonthlyCoverageService(store=store, now=lambda: _NOW)
    unpaid = {
        "id": "inv-r1",
        "userId": "u1",
        "billingReason": "renewal",
        "periodStart": "2026-10-20T10:00:00+00:00",
        "status": "PAYMENT_PENDING",
    }
    store.voidedInvoices.append(unpaid)
    result = service.setRenewalOptOut(
        userId="u1",
        subscription=_subscription(),
        reason=None,
        requestKey="cancel-6",
    )
    assert result["voidedInvoiceIds"] is not None


def test_resume_clears_opt_out_before_final_paid_end():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    cancelled = _subscription(renewal_opt_out=True)
    result = service.resumeRenewal(
        userId="u1",
        subscription=cancelled,
        now=_NOW,
        requestKey="resume-1",
    )
    assert result["renewalOptOut"] is False
    assert result["renewalEligible"] is True


def test_resume_after_final_paid_end_is_denied():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    expired = _subscription(
        status="expired",
        current_period_start=None,
        current_period_end="2026-09-20T10:00:00+00:00",
    )
    with pytest.raises(Exception):
        service.resumeRenewal(
            userId="u1",
            subscription=expired,
            now=_NOW,
            requestKey="resume-2",
        )


def test_resume_of_already_opted_in_is_noop():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    result = service.resumeRenewal(
        userId="u1",
        subscription=_subscription(),
        now=_NOW,
        requestKey="resume-3",
    )
    assert result["renewalOptOut"] is False
    assert result["repeated"] is True


def test_annual_cancellation_still_requires_reason():
    service = MonthlyCoverageService(store=_OptOutStore(), now=lambda: _NOW)
    with pytest.raises(Exception):
        service.setRenewalOptOut(
            userId="u1",
            subscription=_subscription(billing_mode="annual_prepaid"),
            reason=None,
            requestKey="cancel-annual",
        )
