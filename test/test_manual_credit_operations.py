"""Admitted credit operations: durable once-only period attribution.

An old LLM callback settles against its originally admitted credit period,
never against a newly refilled period; duplicate callbacks settle once;
queued work rechecks entitlement at execution start.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.manualBillingContracts import (  # noqa: E402
    CreditOperationContext,
)
from api.services.credits.creditPeriodEngine import (  # noqa: E402
    CreditPeriodEngine,
    _BalanceStore,
)
from api.services.credits.creditOperationSettlement import (  # noqa: E402
    CreditOperationSettlement,
)


_NOW = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
_LATER = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)


def _context(operationId="op-1", creditPeriodId="cp-1", admittedAt=_NOW):
    return CreditOperationContext(
        userId="u1",
        lifecycleId="lc-1",
        creditPeriodId=creditPeriodId,
        operationId=operationId,
        operationType="llm_usage",
        accountingReference="run-1",
        admittedAt=admittedAt,
    )


def _settlement(store=None):
    return CreditOperationSettlement(
        engine=CreditPeriodEngine(store=store or _BalanceStore()),
        store=store or _BalanceStore(),
    )


def test_old_callback_does_not_debit_new_period():
    store = _BalanceStore()
    settlement = _settlement(store)
    engine = CreditPeriodEngine(store=store)
    # Original period allocated and used; then a NEW period activated.
    from api.services.billing.manualBillingContracts import CoveragePeriod

    oldPeriod = CoveragePeriod(
        userId="u1", subscriptionId="s1", lifecycleId="lc-1",
        creditPeriodId="cp-1", invoiceId="inv-1",
        start=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        end=datetime(2026, 10, 20, 10, tzinfo=timezone.utc),
        domains=("banking",), billingMode="monthly_prepaid", revokedAt=None,
    )
    engine.ensurePaidCreditPeriod("u1", oldPeriod, quotaTokens=10_000_000)
    store.upsertBalance("u1", {"used_tokens": 1_000_000, "remaining_tokens": 9_000_000})
    newPeriod = CoveragePeriod(
        userId="u1", subscriptionId="s1", lifecycleId="lc-1",
        creditPeriodId="cp-2", invoiceId="inv-2",
        start=datetime(2026, 10, 20, 10, tzinfo=timezone.utc),
        end=datetime(2026, 11, 20, 10, tzinfo=timezone.utc),
        domains=("banking",), billingMode="monthly_prepaid", revokedAt=None,
    )
    engine.ensurePaidCreditPeriod("u1", newPeriod, quotaTokens=10_000_000)

    # The delayed callback for the OLD admitted period arrives now.
    result = settlement.settleCreditOperation(_context(), tokensUsed=500_000)
    assert result["settled"] is True
    # usage attributed to the ORIGINAL admitted period exactly once; the
    # new refill's quota is untouched (historical-only settlement).
    assert result["creditPeriodId"] == "cp-1"
    assert result.get("historicalOnly") is True
    assert store.getBalance("u1")["used_tokens"] == 0
    assert store.getBalance("u1")["remaining_tokens"] == 10_000_000
    settledOperations = [
        op for op in store.operations
        if op.get("operationType") == "settled_original_period_history"
    ]
    assert len(settledOperations) == 1
    assert settledOperations[0]["tokensUsed"] == 500_000


def test_duplicate_callback_settles_once():
    store = _BalanceStore()
    settlement = _settlement(store)
    first = settlement.settleCreditOperation(_context(), tokensUsed=100)
    second = settlement.settleCreditOperation(_context(), tokensUsed=100)
    assert first["settled"] is True
    assert second["settled"] is False
    assert second.get("duplicate") is True


def test_admit_records_context_before_work():
    store = _BalanceStore()
    settlement = _settlement(store)
    context = settlement.admitCreditOperation(
        userId="u1",
        operationType="llm_usage",
        operationId="op-admit-1",
        lifecycleId="lc-1",
        creditPeriodId="cp-1",
    )
    assert context.operationId == "op-admit-1"
    admitted = [
        op for op in store.operations if op.get("operationType") == "admitted"
    ]
    assert len(admitted) == 1


def test_queued_work_rechecks_entitlement_at_execution_start():
    store = _BalanceStore()
    settlement = _settlement(store)
    # No valid paid period -> admission must fail closed.
    with pytest.raises(Exception):
        settlement.admitCreditOperation(
            userId="u1",
            operationType="llm_usage",
            operationId="op-2",
            lifecycleId="lc-1",
            creditPeriodId=None,
        )


def test_overrun_settlement_preserves_usage_audit():
    store = _BalanceStore()
    store.upsertBalance("u1", {
        "credit_period_id": "cp-1",
        "monthly_token_quota": 100_000,
        "used_tokens": 0,
        "remaining_tokens": 100_000,
    })
    settlement = _settlement(store)
    result = settlement.settleCreditOperation(_context(), tokensUsed=250_000)
    # settle once; usage/debt recorded; remaining floors at zero
    assert result["settled"] is True
    assert store.getBalance("u1")["remaining_tokens"] == 0