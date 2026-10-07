"""Manual monthly lifecycle tests: calendar months, early renewal, boundaries.

Controllable-clock tests for MonthlyCoverageService: calendar clamping,
exact end-instant expiry, early renewal freezing one future month, capture
only initial recovery, replay, expired repurchase, elapsed future periods.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.monthlyCoverageService import (  # noqa: E402
    MonthlyCoverageService,
)


class _MemoryStore:
    def __init__(self):
        self.invoices = []
        self.subscriptions = {}
        self.activations = []
        self.intents = {}

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
            ):
                coverage = (invoice.get("manualBilling") or {}).get("coverageState")
                if coverage in ("scheduled", "active"):
                    return invoice
        return None

    def saveRenewalInvoice(self, invoice):
        self.invoices.append(invoice)
        return invoice

    def recordActivation(self, activation):
        self.activations.append(activation)

    def saveIntent(self, namespace, intent):
        self.intents[namespace] = intent

    def findIntent(self, namespace):
        return self.intents.get(namespace)


def _subscriptionRow(
    *,
    userId="u1",
    status="active",
    billingMode="monthly_prepaid",
    start="2026-09-20T10:00:00+00:00",
    end="2026-10-20T10:00:00+00:00",
    domains=("banking",),
    optOut=False,
    lifecycleId="lc-1",
):
    return {
        "id": "sub-1",
        "user_id": userId,
        "status": status,
        "billing_mode": billingMode,
        "current_period_start": start,
        "current_period_end": end,
        "renewal_due_at": end,
        "subscribed_experts": list(domains),
        "domain_count": len(domains),
        "pending_removals": [],
        "pending_additions": [],
        "billing_state": {"manualBilling": {"lifecycleId": lifecycleId}},
        "renewal_opt_out": optOut,
    }


def _service(store=None, now=None):
    return MonthlyCoverageService(
        store=store or _MemoryStore(),
        now=now or (lambda: datetime(2026, 10, 10, 10, tzinfo=timezone.utc)),
    )


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)


# --- calendar arithmetic ------------------------------------------------------


def test_january_31_clamps_to_february_28():
    service = _service()
    start = datetime(2027, 1, 31, 10, tzinfo=timezone.utc)
    end = service.addCalendarMonth(start)
    assert (end.year, end.month, end.day) == (2027, 2, 28)


def test_february_29_leap_year_clamps_to_march_29():
    service = _service()
    start = datetime(2028, 2, 29, 10, tzinfo=timezone.utc)
    end = service.addCalendarMonth(start)
    assert (end.year, end.month, end.day) == (2028, 3, 29)


def test_january_31_then_march_31_extends_from_stored_expiry():
    service = _service()
    januaryEnd = service.addCalendarMonth(datetime(2027, 1, 31, 10, tzinfo=timezone.utc))
    marchEnd = service.addCalendarMonth(januaryEnd)
    assert (marchEnd.year, marchEnd.month, marchEnd.day) == (2027, 3, 28)


def test_normal_month_rolls_same_day():
    service = _service()
    end = service.addCalendarMonth(datetime(2026, 10, 20, 10, tzinfo=timezone.utc))
    assert (end.year, end.month, end.day) == (2026, 11, 20)


# --- early renewal freezing -----------------------------------------------------


def test_early_renewal_freezes_future_month_from_current_expiry():
    store = _MemoryStore()
    service = _service(store)
    result = service.prepareRenewalInvoice(
        userId="u1",
        subscription=_subscriptionRow(),
        now=_NOW,
    )
    nextPeriod = result["nextPeriod"]
    assert nextPeriod["start"] == "2026-10-20T10:00:00+00:00"
    assert nextPeriod["end"] == "2026-11-20T10:00:00+00:00"
    assert result["creditsRefilled"] is False
    assert result["state"] in ("payment_pending", "invoice_ready")
    # current period unchanged
    assert result["currentPeriod"]["start"] == "2026-09-20T10:00:00+00:00"
    assert result["currentPeriod"]["end"] == "2026-10-20T10:00:00+00:00"


def test_prepare_renewal_reuses_existing_unpaid_revision():
    store = _MemoryStore()
    service = _service(store)
    first = service.prepareRenewalInvoice(
        userId="u1", subscription=_subscriptionRow(), now=_NOW
    )
    second = service.prepareRenewalInvoice(
        userId="u1", subscription=_subscriptionRow(), now=_NOW
    )
    assert first["invoiceId"] == second["invoiceId"]
    assert len(store.invoices) == 1


def test_prepare_renewal_skips_when_opted_out():
    store = _MemoryStore()
    service = _service(store)
    with pytest.raises(Exception):
        service.prepareRenewalInvoice(
            userId="u1",
            subscription=_subscriptionRow(optOut=True),
            now=_NOW,
        )
    assert store.invoices == []


def test_prepare_renewal_skips_when_paid_upcoming_cycle_exists():
    store = _MemoryStore()
    paid = {
        "id": "inv-paid",
        "userId": "u1",
        "billingReason": "renewal",
        "periodStart": "2026-10-20T10:00:00+00:00",
        "periodEnd": "2026-11-20T10:00:00+00:00",
        "status": "PAID",
        "manualBilling": {"coverageState": "scheduled", "domains": ["banking"]},
    }
    store.invoices.append(paid)
    service = _service(store)
    result = service.prepareRenewalInvoice(
        userId="u1", subscription=_subscriptionRow(), now=_NOW
    )
    # The paid future snapshot is returned, not a second payable cycle.
    assert result["invoiceId"] == "inv-paid"
    assert result["state"] == "already_paid"
    assert len(store.invoices) == 1


def test_early_paid_renewal_schedules_coverage_without_refill():
    store = _MemoryStore()
    service = _service(store)
    subscription = _subscriptionRow()
    result = service.applyPaidRenewal(
        userId="u1",
        subscription=subscription,
        invoice={
            "id": "inv-r1",
            "userId": "u1",
            "billingReason": "renewal",
            "periodStart": "2026-10-20T10:00:00+00:00",
            "periodEnd": "2026-11-20T10:00:00+00:00",
            "status": "PAID",
            "domains": ["banking"],
            "totalAmount": 118000,
            "currency": "INR",
        },
        now=_NOW,
    )
    assert result["state"] == "paid_scheduled"
    assert result["creditsRefilled"] is False
    assert result["nextPeriod"]["start"] == "2026-10-20T10:00:00+00:00"
    assert result["nextPeriod"]["end"] == "2026-11-20T10:00:00+00:00"


# --- boundary activation ----------------------------------------------------------


def test_activate_due_coverage_activates_paid_period_once():
    store = _MemoryStore()
    store.invoices.append({
        "id": "inv-r1",
        "userId": "u1",
        "billingReason": "renewal",
        "periodStart": "2026-10-20T10:00:00+00:00",
        "periodEnd": "2026-11-20T10:00:00+00:00",
        "status": "PAID",
        "manualBilling": {
            "coverageState": "scheduled",
            "domains": ["banking"],
            "lifecycleId": "lc-1",
        },
    })
    boundaryNow = datetime(2026, 10, 20, 10, 1, tzinfo=timezone.utc)
    service = _service(store, now=lambda: boundaryNow)
    result = service.activateDueCoverage(
        userId="u1",
        subscription=_subscriptionRow(),
        now=boundaryNow,
    )
    assert result["state"] == "activated"
    assert result["currentPeriod"]["start"] == "2026-10-20T10:00:00+00:00"
    assert result["currentPeriod"]["end"] == "2026-11-20T10:00:00+00:00"
    assert result["currentPeriod"]["domains"] == ["banking"]
    # replay (racing worker reading the same pre-activation row) does not
    # activate twice: the activate operation key is claimed once.
    replay = service.activateDueCoverage(
        userId="u1",
        subscription=_subscriptionRow(),
        now=boundaryNow,
    )
    assert replay["state"] in ("already_finalized", "activated")
    assert len(store.activations) == 1


def test_unpaid_boundary_denies_paid_access_immediately():
    store = _MemoryStore()
    boundaryNow = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    service = _service(store, now=lambda: boundaryNow)
    coverage = service.getCoverage(
        userId="u1",
        subscription=_subscriptionRow(),
        now=boundaryNow,
    )
    # [start, end): at the exact end instant the old period no longer grants.
    assert coverage["paid"] is False


def test_coverage_before_boundary_is_paid():
    store = _MemoryStore()
    before = datetime(2026, 10, 20, 9, 59, 59, tzinfo=timezone.utc)
    service = _service(store, now=lambda: before)
    coverage = service.getCoverage(
        userId="u1", subscription=_subscriptionRow(), now=before
    )
    assert coverage["paid"] is True


def test_elapsed_future_period_materializes_history_without_new_quota():
    store = _MemoryStore()
    # A paid future period was discovered only after its own end: the
    # invoice was scheduled for [10-20, 11-20) and "now" is well past both
    # the current period end and the paid period end.
    store.invoices.append({
        "id": "inv-old",
        "userId": "u1",
        "billingReason": "renewal",
        "periodStart": "2026-10-20T10:00:00+00:00",
        "periodEnd": "2026-11-20T10:00:00+00:00",
        "status": "PAID",
        "manualBilling": {
            "coverageState": "scheduled",
            "domains": ["banking"],
            "lifecycleId": "lc-1",
        },
    })
    lateNow = datetime(2026, 12, 25, 10, tzinfo=timezone.utc)
    service = _service(store, now=lambda: lateNow)
    result = service.activateDueCoverage(
        userId="u1",
        subscription=_subscriptionRow(start="2026-09-20T10:00:00+00:00", end="2026-10-20T10:00:00+00:00"),
        now=lateNow,
    )
    assert result["state"] == "elapsed"
    assert result["creditsRefilled"] is False
    assert result["finalized"] is False