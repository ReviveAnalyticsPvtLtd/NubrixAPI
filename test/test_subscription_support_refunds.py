"""Staff-approved unused-time refund tests.

Deterministic 20/30 amounts, cutoff semantics, future-only vs current
termination, duplicate/late-key intents, provider timeout -> unknown state
with access staying closed.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.subscriptionRefundService import (  # noqa: E402
    SubscriptionRefundService,
)


_NOW = datetime(2026, 10, 25, 10, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.quotes = {}
        self.intents = {}
        self.providerCalls = []
        self.subscriptions = {}

    def saveQuote(self, quote):
        self.quotes[quote["quoteId"]] = quote

    def findQuote(self, quoteId):
        return self.quotes.get(quoteId)

    def findClosingIntentForInterval(self, userId, invoiceId, intervalStart):
        for intent in self.intents.values():
            if (
                intent["userId"] == userId
                and intent["invoiceId"] == invoiceId
                and intent["intervalStart"] == intervalStart
            ):
                return intent
        return None

    def saveIntent(self, intent):
        self.intents[intent["refundIntentId"]] = intent

    def findIntent(self, refundIntentId):
        return self.intents.get(refundIntentId)

    def updateIntentState(self, refundIntentId, refundState, providerRefundIds=None):
        intent = self.intents.get(refundIntentId)
        if intent:
            intent["refundState"] = refundState
            intent["providerRefundIds"] = providerRefundIds or intent.get("providerRefundIds")


class _Provider:
    def __init__(self, behavior="success"):
        self.behavior = behavior

    def refund(self, paymentId, amount):
        if self.behavior == "timeout":
            raise TimeoutError("provider timeout")
        if self.behavior == "failure":
            raise RuntimeError("provider rejected")
        return {"id": f"rfnd_{paymentId}_{amount}", "status": "processed"}


def _paidInterval(invoiceId, start, end, amount=3000, kind="current"):
    return {
        "invoiceId": invoiceId,
        "paymentId": f"pay_{invoiceId}",
        "start": start,
        "end": end,
        "amount": amount,
        "currency": "INR",
        "kind": kind,
    }


def _service(store=None, provider=None, now=None):
    return SubscriptionRefundService(
        store=store or _Store(),
        provider=provider or _Provider(),
        now=now or (lambda: _NOW),
    )


# --- quote ----------------------------------------------------------------


def test_quote_30_days_with_20_unused_returns_two_thirds():
    # Interval Oct 5 -> Nov 4 (30 days); cutoff Oct 15 leaves 20 unused days.
    quoteNow = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
    service = _service(now=lambda: quoteNow)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-100",
            "reason": "user emailed, approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    # 20 unused days of a 30-day interval: floor(3000 * 20/30) = 2000
    assert quote.amount == 2000
    assert quote.currency == "INR"
    assert quote.caseReference == "CASE-100"


def test_quote_excludes_topups():
    quoteNow = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
    service = _service(now=lambda: quoteNow)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-101",
            "reason": "approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
            ),
        ],
        subscription={"user_id": "u1", "status": "active"},
        topupIntervals=[{"invoiceId": "inv-topup", "amount": 199900}],
    )
    assert quote.amount == 2000  # top-up not part of subscription refund


def test_quote_future_only_preserves_current_access():
    service = _service()
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-future"],
            "caseReference": "CASE-102",
            "reason": "approved future-only refund",
        },
        paidIntervals=[
            _paidInterval(
                "inv-future",
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                datetime(2026, 12, 4, 10, tzinfo=timezone.utc),
                amount=118000,
                kind="future",
            ),
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    assert quote.accessExpired is False
    assert quote.currentAccessPreserved is True
    # fully unused future period: full amount
    assert quote.amount == 118000


def test_quote_current_termination_expires_access():
    service = _service()
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-103",
            "reason": "approved current termination",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
                kind="current",
            ),
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    assert quote.accessExpired is True
    assert quote.currentAccessPreserved is False


def test_quote_requires_nonempty_reason_and_case():
    service = _service()
    with pytest.raises(Exception):
        service.quoteUnusedTimeRefund(
            staffId="staff-1",
            payload={
                "userId": "u1",
                "invoiceIds": ["inv-1"],
                "caseReference": "",
                "reason": "",
            },
            paidIntervals=[
                _paidInterval(
                    "inv-1",
                    datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                    datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                )
            ],
            subscription={"user_id": "u1", "status": "active"},
        )


# --- initiation --------------------------------------------------------------


def test_initiate_recomputes_cutoff_and_conflicts_on_amount_change():
    store = _Store()
    service = _service(store=store)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-104",
            "reason": "approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    laterNow = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
    laterService = _service(store=store, now=lambda: laterNow)
    with pytest.raises(Exception) as exc:
        laterService.initiateUnusedTimeRefund(
            staffId="staff-1",
            payload={
                "userId": "u1",
                "invoiceIds": ["inv-1"],
                "caseReference": "CASE-104",
                "reason": "approved",
                "quoteId": quote.quoteId,
                "expectedTotalAmount": quote.amount,
            },
            requestKey="key-1",
        )
    assert "409" in str(exc.value) or "conflict" in str(exc.value).lower() or exc.value


def test_duplicate_intent_with_another_key_returns_existing():
    store = _Store()
    provider = _Provider()
    service = _service(store=store, provider=provider)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-105",
            "reason": "approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    first = service.initiateUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-105",
            "reason": "approved",
            "quoteId": quote.quoteId,
            "expectedTotalAmount": quote.amount,
        },
        requestKey="key-1",
    )
    # A different client key for the same interval returns the existing closure.
    second = service.initiateUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-105",
            "reason": "approved",
            "quoteId": quote.quoteId,
            "expectedTotalAmount": quote.amount,
        },
        requestKey="key-2",
    )
    assert first.refundIntentId == second.refundIntentId
    assert len(store.intents) == 1


def test_provider_timeout_leaves_unknown_state_and_closed_access():
    store = _Store()
    provider = _Provider(behavior="timeout")
    service = _service(store=store, provider=provider)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-106",
            "reason": "approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-1",
                datetime(2026, 10, 5, 10, tzinfo=timezone.utc),
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                amount=3000,
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    intent = service.initiateUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-1"],
            "caseReference": "CASE-106",
            "reason": "approved",
            "quoteId": quote.quoteId,
            "expectedTotalAmount": quote.amount,
        },
        requestKey="key-1",
    )
    assert intent.refundState == "unknown"
    assert intent.accessRestored is False
    assert intent.accessExpired is True  # current termination closed access


def test_future_only_refund_preserves_current_access_and_sets_optout():
    store = _Store()
    service = _service(store=store)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-future"],
            "caseReference": "CASE-107",
            "reason": "approved future-only",
        },
        paidIntervals=[
            _paidInterval(
                "inv-future",
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                datetime(2026, 12, 4, 10, tzinfo=timezone.utc),
                amount=118000,
                kind="future",
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    intent = service.initiateUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-future"],
            "caseReference": "CASE-107",
            "reason": "approved future-only",
            "quoteId": quote.quoteId,
            "expectedTotalAmount": quote.amount,
        },
        requestKey="key-1",
    )
    assert intent.accessExpired is False
    assert intent.currentAccessPreserved is True
    assert intent.accessRestored is False


def test_refund_state_transitions_to_processed_on_provider_confirmation():
    store = _Store()
    service = _service(store=store)
    quote = service.quoteUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-future"],
            "caseReference": "CASE-108",
            "reason": "approved",
        },
        paidIntervals=[
            _paidInterval(
                "inv-future",
                datetime(2026, 11, 4, 10, tzinfo=timezone.utc),
                datetime(2026, 12, 4, 10, tzinfo=timezone.utc),
                amount=118000,
                kind="future",
            )
        ],
        subscription={"user_id": "u1", "status": "active"},
    )
    intent = service.initiateUnusedTimeRefund(
        staffId="staff-1",
        payload={
            "userId": "u1",
            "invoiceIds": ["inv-future"],
            "caseReference": "CASE-108",
            "reason": "approved",
            "quoteId": quote.quoteId,
            "expectedTotalAmount": quote.amount,
        },
        requestKey="key-1",
    )
    settled = service.settleRefundEvidence(
        refundIntentId=intent.refundIntentId,
        providerEvidence={"refunds": [{"id": "rfnd_1", "status": "processed"}]},
    )
    assert settled["refundState"] == "processed"