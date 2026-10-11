"""Independent top-up settlement: TTL, post-expiry grant, no access."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.credits.topupSettlement import (  # noqa: E402
    TopupSettlementService,
)


_NOW = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)


class _Store:
    def __init__(self):
        self.attempts = {}
        self.grants = []
        self.balances = {}

    def findAttempt(self, attemptKey):
        return self.attempts.get(attemptKey)

    def recordGrant(self, userId, tokens, operationId):
        self.grants.append({"userId": userId, "tokens": tokens, "operationId": operationId})

    def addTopupTokens(self, userId, tokens):
        row = self.balances.setdefault(userId, {"topup_tokens": 0})
        row["topup_tokens"] += tokens
        return dict(row)


def _attempt(created=_NOW, expiresAt=None):
    return {
        "state": "pending_capture",
        "createdAt": (created or _NOW).isoformat(),
        "expiresAt": (expiresAt or (created or _NOW) + timedelta(minutes=30)).isoformat(),
        "tokens": 500_000,
    }


def test_valid_topup_settles_after_subscription_expiry():
    # Attempt created while eligible; captured after expiry: grant once,
    # store the balance, grant NO paid time.
    store = _Store()
    store.attempts["topup:u1:pack-1"] = _attempt(
        created=datetime(2026, 10, 20, 9, tzinfo=timezone.utc)
    )
    service = TopupSettlementService(store=store, now=lambda: _NOW)
    result = service.settleCapturedTopup(
        userId="u1",
        attemptKey="topup:u1:pack-1",
        providerPaymentId="pay_1",
    )
    assert result["granted"] is True
    assert result["tokens"] == 500_000
    assert result["paidAccessGranted"] is False
    assert store.balances["u1"]["topup_tokens"] == 500_000


def test_settlement_is_idempotent_per_payment():
    store = _Store()
    store.attempts["topup:u1:pack-1"] = _attempt()
    service = TopupSettlementService(store=store, now=lambda: _NOW)
    first = service.settleCapturedTopup("u1", "topup:u1:pack-1", "pay_1")
    second = service.settleCapturedTopup("u1", "topup:u1:pack-1", "pay_1")
    assert first["granted"] is True
    assert second["granted"] is False
    assert store.balances["u1"]["topup_tokens"] == 500_000


def test_superseded_attempt_is_investigated_not_granted():
    store = _Store()
    store.attempts["topup:u1:pack-1"] = {
        **_attempt(),
        "state": "superseded",
    }
    service = TopupSettlementService(store=store, now=lambda: _NOW)
    result = service.settleCapturedTopup("u1", "topup:u1:pack-1", "pay_late")
    assert result["granted"] is False
    assert result["disposition"] == "reconciliation"
    assert "u1" not in store.balances or store.balances.get("u1", {}).get("topup_tokens") == 0


def test_cancelled_attempt_capture_is_investigated():
    store = _Store()
    store.attempts["topup:u1:pack-1"] = {
        **_attempt(),
        "state": "cancelled",
    }
    service = TopupSettlementService(store=store, now=lambda: _NOW)
    result = service.settleCapturedTopup("u1", "topup:u1:pack-1", "pay_late")
    assert result["granted"] is False
    assert result["disposition"] == "reconciliation"


def test_unknown_attempt_is_reconciliation():
    store = _Store()
    service = TopupSettlementService(store=store, now=lambda: _NOW)
    result = service.settleCapturedTopup("u1", "topup:u1:missing", "pay_x")
    assert result["granted"] is False
    assert result["disposition"] == "reconciliation"