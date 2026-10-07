"""Expert change safety: removals, additions, cancelled bundle hardening."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.monthlyCoverageService import (  # noqa: E402
    MonthlyCoverageService,
)


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.invoices = []

    def findPayableRenewal(self, userId, cycleStart):
        for invoice in self.invoices:
            if (
                invoice.get("userId") == userId
                and invoice.get("billingReason") == "renewal"
                and invoice.get("periodStart") == cycleStart
                and invoice.get("status") in ("UPCOMING", "PAYMENT_PENDING")
            ):
                return invoice
        return None

    def findPaidFutureInvoice(self, userId, cycleStart):
        for invoice in self.invoices:
            if (
                invoice.get("userId") == userId
                and invoice.get("billingReason") == "renewal"
                and invoice.get("periodStart") == cycleStart
                and invoice.get("status") == "PAID"
                and (invoice.get("manualBilling") or {}).get("coverageState")
                in ("scheduled", "active")
            ):
                return invoice
        return None

    def saveRenewalInvoice(self, invoice):
        self.invoices.append(invoice)

    def recordActivation(self, activation):
        pass


def _subscription(**overrides):
    row = {
        "id": "sub-1",
        "user_id": "u1",
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": "2026-10-20T10:00:00+00:00",
        "subscribed_experts": ["banking", "manufacturing", "telecom"],
        "pending_removals": [],
        "pending_additions": [],
        "billing_state": {"manualBilling": {"lifecycleId": "lc-1"}},
        "renewal_opt_out": False,
    }
    row.update(overrides)
    return row


def _service(store=None):
    return MonthlyCoverageService(store=store or _Store(), now=lambda: _NOW)


# --- removals ---------------------------------------------------------------


def test_unpaid_removal_preserves_current_experts():
    store = _Store()
    service = _service(store)
    subscription = _subscription(pending_removals=["telecom"])
    result = service.prepareRenewalInvoice(
        userId="u1", subscription=subscription, now=_NOW
    )
    # current experts unchanged; renewal carries the reduced selection.
    assert result["currentPeriod"]["domains"] == ["banking", "manufacturing", "telecom"]
    assert result["nextPeriod"]["domains"] == ["banking", "manufacturing"]


def test_removal_cannot_empty_next_selection():
    service = _service()
    subscription = _subscription(
        subscribed_experts=["banking"],
        pending_removals=["banking"],
    )
    with pytest.raises(Exception):
        service.prepareRenewalInvoice(
            userId="u1", subscription=subscription, now=_NOW
        )


def test_paid_future_removal_conflicts():
    store = _Store()
    store.invoices.append({
        "id": "inv-paid",
        "userId": "u1",
        "billingReason": "renewal",
        "periodStart": "2026-10-20T10:00:00+00:00",
        "periodEnd": "2026-11-20T10:00:00+00:00",
        "status": "PAID",
        "domains": ["banking", "manufacturing", "telecom"],
        "manualBilling": {"coverageState": "scheduled", "lifecycleId": "lc-1"},
    })
    service = _service(store)
    with pytest.raises(Exception):
        service.validateRemovalAgainstFuture(
            userId="u1",
            subscription=_subscription(pending_removals=["telecom"]),
            now=_NOW,
        )


def test_unpaid_future_removal_allowed_and_repriced():
    store = _Store()
    service = _service(store)
    # No paid future invoice: removal applies to the unpaid next revision.
    result = service.validateRemovalAgainstFuture(
        userId="u1",
        subscription=_subscription(pending_removals=["telecom"]),
        now=_NOW,
    )
    assert result["allowed"] is True


# --- additions + cancelled bundles ---------------------------------------------


def test_captured_addition_reprices_unpaid_renewal():
    store = _Store()
    service = _service(store)
    subscription = _subscription(subscribed_experts=["banking", "manufacturing", "telecom", "supplychain"])
    # an addition captured mid-period must flow into the unpaid renewal
    result = service.prepareRenewalInvoice(
        userId="u1", subscription=subscription, now=_NOW
    )
    assert len(result["nextPeriod"]["domains"]) == 4


def test_concurrent_additions_respect_four_expert_limit():
    service = _service()
    with pytest.raises(Exception):
        service.validateAdditionCapacity(
            subscription=_subscription(subscribed_experts=["a", "b", "c", "d"]),
            requested=["e"],
        )


def test_pending_addition_not_carried_into_renewal():
    store = _Store()
    service = _service(store)
    subscription = _subscription(
        pending_additions=[{"domain": "supplychain", "state": "awaiting_payment"}],
    )
    result = service.prepareRenewalInvoice(
        userId="u1", subscription=subscription, now=_NOW
    )
    assert "supplychain" not in result["nextPeriod"]["domains"]


def test_cancelled_shared_bundle_cannot_activate_from_late_capture():
    service = _service()
    result = service.evaluateCancelledAdditionCapture(
        pendingAddition={"domain": "supplychain", "state": "cancelled", "orderId": "order_1"},
        capturedNow=_NOW,
    )
    assert result["activate"] is False
    assert result["disposition"] == "reconciliation"