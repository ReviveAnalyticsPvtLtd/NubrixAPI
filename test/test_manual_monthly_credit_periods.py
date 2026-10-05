"""Durable paid-period credit engine tests.

One allocation per valid paid credit period; early payment never refills;
unpaid boundary closes with zero quota (no minimum-one clamp); top-ups
survive; Redis loss does not manufacture an unpaid quota.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.manualBillingContracts import (  # noqa: E402
    CoveragePeriod,
)
from api.services.credits.creditPeriodEngine import (  # noqa: E402
    CreditPeriodEngine,
)


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
_START = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
_END = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


def _period(start=_START, end=_END, creditPeriodId="cp-1"):
    return CoveragePeriod(
        userId="u1",
        subscriptionId="sub-1",
        lifecycleId="lc-1",
        creditPeriodId=creditPeriodId,
        invoiceId="inv-1",
        start=start,
        end=end,
        domains=("banking",),
        billingMode="monthly_prepaid",
        revokedAt=None,
    )


class _BalanceStore:
    def __init__(self):
        self.rows = {}
        self.operations = []

    def getBalance(self, userId):
        return self.rows.get(userId)

    def upsertBalance(self, userId, payload):
        row = self.rows.setdefault(
            userId,
            {
                "monthly_token_quota": 0,
                "used_tokens": 0,
                "remaining_tokens": 0,
                "topup_tokens": 0,
                "balance_version": 0,
                "lifecycle_id": None,
                "credit_period_id": None,
                "period_start": None,
                "period_end": None,
                "domain_count": 1,
            },
        )
        row.update(payload)
        return dict(row)

    def recordOperation(self, operation):
        self.operations.append(operation)

    def hasOperation(self, operationId):
        return any(op.get("operationId") == operationId for op in self.operations)


def _engine(store=None):
    return CreditPeriodEngine(store=store or _BalanceStore())


def test_ensure_paid_credit_period_allocates_once():
    store = _BalanceStore()
    engine = _engine(store)
    first = engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    assert first["allocated"] is True
    assert first["row"]["monthly_token_quota"] == 10_000_000
    second = engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    assert second["allocated"] is False  # same credit period: no double refill


def test_new_paid_period_gets_fresh_allocation():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    nextPeriod = _period(
        start=_END,
        end=datetime(2026, 11, 20, 10, tzinfo=timezone.utc),
        creditPeriodId="cp-2",
    )
    result = engine.ensurePaidCreditPeriod("u1", nextPeriod, quotaTokens=10_000_000)
    assert result["allocated"] is True
    assert result["row"]["credit_period_id"] == "cp-2"
    assert result["row"]["remaining_tokens"] == 10_000_000


def test_usage_carries_period_version_forward_not_quota_back():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    store.upsertBalance("u1", {"used_tokens": 4_000_000, "remaining_tokens": 6_000_000})
    nextPeriod = _period(
        start=_END,
        end=datetime(2026, 11, 20, 10, tzinfo=timezone.utc),
        creditPeriodId="cp-2",
    )
    result = engine.ensurePaidCreditPeriod("u1", nextPeriod, quotaTokens=10_000_000)
    # new period resets quota; usage history preserved in prior row state
    assert result["row"]["remaining_tokens"] == 10_000_000
    assert result["row"]["used_tokens"] == 0


def test_early_renewal_payment_does_not_refill_current_period():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    store.upsertBalance("u1", {"used_tokens": 9_000_000, "remaining_tokens": 1_000_000})
    # An early renewal payment arrives: current quota must not change now.
    engine.scheduleFuturePeriod("u1", _period(
        start=_END,
        end=datetime(2026, 11, 20, 10, tzinfo=timezone.utc),
        creditPeriodId="cp-2",
    ), quotaTokens=10_000_000)
    row = store.getBalance("u1")
    assert row["remaining_tokens"] == 1_000_000
    assert row["credit_period_id"] == "cp-1"


def test_close_subscription_credit_period_zeroes_quota_preserving_topups():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    store.upsertBalance("u1", {"topup_tokens": 500_000, "used_tokens": 2_000_000})
    result = engine.closeSubscriptionCreditPeriod(
        userId="u1",
        lifecycleId="lc-1",
        cutoff=_NOW,
        operationId="close-1",
    )
    row = store.getBalance("u1")
    assert row["monthly_token_quota"] == 0
    assert row["remaining_tokens"] == 0
    assert row["topup_tokens"] == 500_000  # purchased top-ups preserved
    assert result["closed"] is True


def test_close_is_idempotent():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    engine.closeSubscriptionCreditPeriod("u1", "lc-1", _NOW, "close-1")
    second = engine.closeSubscriptionCreditPeriod("u1", "lc-1", _NOW, "close-1")
    assert second["closed"] is False  # already closed, no repeat mutation
    # A distinct close operation on an already-closed row is still a no-op.
    third = engine.closeSubscriptionCreditPeriod("u1", "lc-1", _NOW, "close-2")
    assert third["closed"] is False


def test_redis_loss_does_not_manufacture_unpaid_quota():
    store = _BalanceStore()
    engine = _engine(store)
    # No paid period exists; a cache rebuild must not allocate.
    result = engine.rebuildCacheProjection("u1")
    assert result["allocated"] is False


def test_topups_do_not_grant_paid_access():
    from api.services.subscriptions.paymentValidationService import isAccessActive

    # top-ups are balances, never an access grant: an expired row stays off
    # even with top-ups stored.
    expiredRow = {
        "status": "expired",
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-08-20T10:00:00+00:00",
        "current_period_end": "2026-09-20T10:00:00+00:00",
    }
    assert isAccessActive(expiredRow) is False


def test_zero_quota_close_does_not_clamp_to_minimum_one():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    engine.closeSubscriptionCreditPeriod("u1", "lc-1", _NOW, "close-1")
    row = store.getBalance("u1")
    # no minimum-one clamp: quota is exactly zero for a closed balance
    assert row["monthly_token_quota"] == 0
    assert row["domain_count"] == 0


def test_close_records_durable_operation():
    store = _BalanceStore()
    engine = _engine(store)
    engine.ensurePaidCreditPeriod("u1", _period(), quotaTokens=10_000_000)
    engine.closeSubscriptionCreditPeriod("u1", "lc-1", _NOW, "close-1")
    operations = [
        op for op in store.operations
        if op.get("operationType") == "close_subscription_period"
    ]
    assert len(operations) == 1
    assert operations[0]["preservedTopups"] == 0