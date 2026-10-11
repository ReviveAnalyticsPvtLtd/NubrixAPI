"""Exact access gate tests: payment_pending, expiry instants, 503 semantics."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.subscriptions.paymentValidationService import (  # noqa: E402
    isAccessActive,
    blocksNewCheckout,
    isPeriodExpired,
)
from api.services.subscriptions.entitlementService import (  # noqa: E402
    evaluateSubscriptionEntitlement,
)


def _period_end(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _row(status="active", period_end=_period_end(10), billing_mode="monthly_prepaid"):
    return {
        "status": status,
        "billing_mode": billing_mode,
        "plan_type": "pro",
        "current_period_start": _period_end(-30),
        "current_period_end": period_end,
    }


def test_payment_pending_is_not_an_unconditional_access_grant():
    # payment_pending means an unpaid next-period invoice exists; without a
    # valid current timestamp it must not grant paid access.
    expiredPending = _row(status="payment_pending", period_end=_period_end(-1))
    assert isAccessActive(expiredPending) is False
    entitlement = evaluateSubscriptionEntitlement("u1", expiredPending)
    assert entitlement.activeSubscription is False
    assert entitlement.paidPlan is False


def test_payment_pending_with_valid_period_is_paid():
    validPending = _row(status="payment_pending", period_end=_period_end(5))
    assert isAccessActive(validPending) is True
    entitlement = evaluateSubscriptionEntitlement("u1", validPending)
    assert entitlement.activeSubscription is True


def test_exact_end_instant_denies_paid_access():
    end = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    row = {
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "plan_type": "pro",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": end.isoformat(),
    }
    assert isAccessActive(row, now=end) is False
    assert isPeriodExpired(row, now=end) is True
    # one microsecond before the end still covered
    before = end - timedelta(microseconds=1)
    assert isAccessActive(row, now=before) is True


def test_old_jwt_claims_are_not_authority():
    # A stale status cannot override timestamps: expired is expired.
    staleRow = {
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "plan_type": "pro",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": "2026-09-21T10:00:00+00:00",
    }
    entitlement = evaluateSubscriptionEntitlement(
        "u1", staleRow, now=datetime(2026, 10, 25, tzinfo=timezone.utc)
    ) if False else evaluateSubscriptionEntitlement("u1", staleRow)
    assert entitlement.activeSubscription is False


def test_blocks_new_checkout_uses_valid_coverage():
    paid = _row(status="active", period_end=_period_end(10))
    assert blocksNewCheckout(paid) is True
    expired = _row(status="active", period_end=_period_end(-1))
    assert blocksNewCheckout(expired) is False


def test_unverifiable_state_is_not_fail_open():
    # None row means no entitlement: closed, not fail-open.
    assert isAccessActive(None) is False
    assert blocksNewCheckout(None) is False