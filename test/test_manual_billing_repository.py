"""Behavioural tests for manualBillingContracts and the transaction repository.

The repository uses psycopg2 against a real database transaction. These tests
exercise the SQL-building and transactional semantics against a fake
connection/cursor so they run without a live database. Real mutation coverage
is the opt-in integration suite.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.manualBillingContracts import (  # noqa: E402
    CheckoutIntent,
    CoveragePeriod,
    CreditOperationContext,
    FinalizationResult,
    RefundIntent,
    RefundQuote,
    VerifiedPaymentEvidence,
)
from api.services.billing.manualBillingRepository import (  # noqa: E402
    ManualBillingRepository,
)


_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _ScriptedCursor:
    """Executes scripted queries; fetch results are queued per query."""

    def __init__(self, connection):
        self.connection = connection
        self.executed = []

    def execute(self, query, parameters=None):
        self.executed.append((query, parameters))
        for fragment, results, error in self.connection.script:
            normalized = " ".join(query.lower().split())
            if fragment in normalized:
                if error is not None:
                    raise error()
                self.connection.fetchoneResults = list(results)
                return
        self.connection.fetchoneResults = []

    def fetchone(self):
        if self.connection.fetchoneResults:
            return self.connection.fetchoneResults.pop(0)
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ScriptedConnection:
    def __init__(self):
        self.committed = 0
        self.rolledBack = 0
        self.closed = 0
        self.cursors = []
        self.script = []
        self.fetchoneResults = []

    def on(self, fragment, results=(), error=None):
        self.script.append((fragment, list(results), error))
        return self

    def cursor(self, cursor_factory=None):
        cursor = _ScriptedCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolledBack += 1

    def close(self):
        self.closed += 1

    @property
    def sql(self):
        return "\n".join(query for query, _ in self.cursors[-1].executed) if self.cursors else ""


def _repository(connection):
    return ManualBillingRepository(connectionFactory=lambda: connection)


# --- contracts --------------------------------------------------------------


def test_coverage_period_is_frozen_dataclass():
    period = CoveragePeriod(
        userId="u1",
        subscriptionId="s1",
        lifecycleId="lc1",
        creditPeriodId="cp1",
        invoiceId="inv1",
        start=_NOW,
        end=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
        domains=("banking",),
        billingMode="monthly_prepaid",
        revokedAt=None,
    )
    with pytest.raises(Exception):
        period.userId = "u2"  # type: ignore[misc]


def test_verified_payment_evidence_defaults():
    evidence = VerifiedPaymentEvidence(
        attemptId="a1",
        invoiceId="inv1",
        userId="u1",
        providerOrderId="order_1",
        providerPaymentId="pay_1",
        purpose="renewal",
        currency="INR",
        financialStatus="captured",
        timingKind="server_observation",
        amount=100000,
        observedAt=_NOW,
        provenCaptureAt=None,
        sourceEventId=None,
        timingVerified=False,
    )
    assert evidence.timingVerified is False


def test_finalization_result_allows_missing_attempt():
    result = FinalizationResult(
        invoiceId="inv1",
        attemptId=None,
        state="expired",
        creditState="closed",
        finalized=False,
        creditsRefilled=False,
        renewalOptOut=False,
        currentPeriod=None,
        nextPeriod=None,
        anomalyId=None,
    )
    assert result.attemptId is None
    assert result.currentPeriod is None


# --- repository -------------------------------------------------------------


def test_missing_owner_gets_one_non_entitled_canonical_shell():
    connection = _ScriptedConnection()
    connection.on("is_canonical = true", results=[])  # no canonical row
    connection.on("order by updated_at desc", results=[])  # no history
    connection.on(
        "insert into public.subscriptions",
        results=[
            {
                "id": "shell-1",
                "user_id": "u-new",
                "billing_mode": "none",
                "status": "none",
                "plan_type": "none",
                "is_canonical": True,
            }
        ],
    )
    row = _repository(connection).ensureCanonicalSubscription("u-new")
    assert row["is_canonical"] is True
    assert row["status"] == "none"
    assert row["billing_mode"] == "none"
    assert connection.committed == 1
    assert connection.rolledBack == 0


def test_ensure_canonical_uses_per_user_advisory_lock():
    connection = _ScriptedConnection()
    connection.on("is_canonical = true", results=[{"id": "c1", "is_canonical": True}])
    _repository(connection).ensureCanonicalSubscription("u-lock")
    assert "pg_advisory_xact_lock" in connection.sql


def test_historical_row_can_be_promoted_when_no_canonical_exists():
    connection = _ScriptedConnection()
    historical = {
        "id": "hist-1",
        "user_id": "u-hist",
        "billing_mode": "monthly_recurring",
        "status": "expired",
        "is_canonical": False,
    }
    connection.on("is_canonical = true", results=[])
    connection.on("order by updated_at desc", results=[historical])
    connection.on("update public.subscriptions", results=[])
    row = _repository(connection).ensureCanonicalSubscription("u-hist")
    assert row["id"] == "hist-1"
    assert row["is_canonical"] is True
    assert connection.committed == 1


def test_existing_canonical_row_is_returned_without_rewrites():
    connection = _ScriptedConnection()
    canonical = {
        "id": "c1",
        "user_id": "u1",
        "billing_mode": "monthly_prepaid",
        "status": "active",
        "is_canonical": True,
    }
    connection.on("is_canonical = true", results=[canonical])
    row = _repository(connection).ensureCanonicalSubscription("u1")
    assert row["id"] == "c1"
    # Only the lock and the select ran — no insert/update.
    assert "insert" not in connection.sql
    assert "update" not in connection.sql


def test_reserve_checkout_intent_persists_attempt_row():
    connection = _ScriptedConnection()
    connection.on("idempotency_key = %s", results=[])  # no existing intent
    connection.on(
        "where id = %s",
        results=[
            {
                "id": "attempt-1",
                "user_id": "u1",
                "invoice_id": "inv-1",
                "payment_status": "created",
                "provider_order_id": None,
                "metadata_json": {
                    "manualBilling": {
                        "lifecycleId": "lc1",
                        "payloadHash": "hash-1",
                        "frozenAmount": 100000,
                        "currency": "INR",
                        "expiresAt": "2026-10-05T12:30:00+00:00",
                        "revision": 1,
                        "billingMode": "monthly_prepaid",
                        "purpose": "initial_purchase",
                    }
                },
            }
        ],
    )
    intent = _repository(connection).reserveCheckoutIntent(
        userId="u1",
        purpose="initial_purchase",
        requestKey="req-1",
        payloadHash="hash-1",
        snapshot={
            "lifecycleId": "lc1",
            "invoiceId": "inv-1",
            "subscriptionId": "sub-1",
            "billingMode": "monthly_prepaid",
            "domains": ["banking"],
            "amount": 100000,
            "currency": "INR",
            "expiresAt": "2026-10-05T12:30:00+00:00",
            "periodStart": "2026-10-05T12:00:00+00:00",
            "periodEnd": "2026-11-05T12:00:00+00:00",
        },
    )
    assert isinstance(intent, CheckoutIntent)
    assert intent.payloadHash == "hash-1"
    assert intent.amount == 100000
    assert intent.razorpayOrderId is None
    assert "billing_events" in connection.sql
    assert "authenticated_checkout" in connection.sql
    assert "pg_advisory_xact_lock" in connection.sql
    assert connection.committed == 1
    assert connection.rolledBack == 0


def test_same_key_changed_payload_conflicts():
    connection = _ScriptedConnection()
    connection.on(
        "idempotency_key = %s",
        results=[
            {
                "id": "attempt-1",
                "user_id": "u1",
                "invoice_id": "inv-1",
                "payment_status": "created",
                "provider_order_id": None,
                "metadata_json": {
                    "manualBilling": {
                        "payloadHash": "different-hash",
                        "purpose": "initial_purchase",
                    }
                },
            }
        ],
    )
    with pytest.raises(ValueError):
        _repository(connection).reserveCheckoutIntent(
            userId="u1",
            purpose="initial_purchase",
            requestKey="req-1",
            payloadHash="hash-1",
            snapshot={"lifecycleId": "lc1", "amount": 100000, "currency": "INR"},
        )
    assert connection.rolledBack == 1
    assert connection.committed == 0


def test_same_key_same_payload_returns_original_intent():
    connection = _ScriptedConnection()
    connection.on(
        "idempotency_key = %s",
        results=[
            {
                "id": "attempt-1",
                "user_id": "u1",
                "invoice_id": "inv-1",
                "payment_status": "created",
                "provider_order_id": None,
                "metadata_json": {
                    "manualBilling": {
                        "payloadHash": "hash-1",
                        "purpose": "initial_purchase",
                        "frozenAmount": 100000,
                        "currency": "INR",
                        "expiresAt": "2026-10-05T12:30:00+00:00",
                    }
                },
            }
        ],
    )
    intent = _repository(connection).reserveCheckoutIntent(
        userId="u1",
        purpose="initial_purchase",
        requestKey="req-1",
        payloadHash="hash-1",
        snapshot={"lifecycleId": "lc1", "amount": 100000, "currency": "INR"},
    )
    assert intent.attemptId == "attempt-1"
    # no second insert for an idempotent replay
    assert "insert into public.billing_events" not in connection.sql


def test_bind_provider_order_updates_attempt_mapping():
    connection = _ScriptedConnection()
    connection.on(
        "update public.billing_events",
        results=[
            {
                "id": "attempt-1",
                "user_id": "u1",
                "invoice_id": "inv-1",
                "payment_status": "pending_provider_ack",
                "provider_order_id": "order_new",
                "metadata_json": {
                    "manualBilling": {
                        "purpose": "initial_purchase",
                        "frozenAmount": 100000,
                        "currency": "INR",
                        "expiresAt": "2026-10-05T12:30:00+00:00",
                    }
                },
            }
        ],
    )
    intent = _repository(connection).bindProviderOrder(
        "attempt-1",
        {"id": "order_new", "status": "created", "amount": 100000},
    )
    assert intent.razorpayOrderId == "order_new"
    assert intent.state == "pending_provider_ack"
    assert "provider_order_id" in connection.sql
    assert connection.committed == 1


def test_transaction_failure_rolls_back_all_writes():
    connection = _ScriptedConnection()
    connection.on("idempotency_key = %s", results=[])
    connection.on("insert into public.billing_events", error=RuntimeError)
    with pytest.raises(RuntimeError):
        _repository(connection).reserveCheckoutIntent(
            userId="u1",
            purpose="initial_purchase",
            requestKey="req-2",
            payloadHash="hash-2",
            snapshot={"lifecycleId": "lc1", "amount": 100000, "currency": "INR"},
        )
    assert connection.rolledBack == 1
    assert connection.committed == 0
    assert connection.closed >= 1


def test_set_renewal_opt_out_writes_flag_and_reason():
    connection = _ScriptedConnection()
    connection.on(
        "renewal_opt_out = %s",
        results=[
            {
                "id": "sub-1",
                "user_id": "u1",
                "status": "active",
                "renewal_opt_out": True,
                "cancellation_reason": "not needed",
                "current_period_end": "2026-10-20T10:00:00+00:00",
            }
        ],
    )
    row = _repository(connection).setRenewalOptOut(
        "u1", True, "not needed", requestKey="cancel-1"
    )
    assert row["renewal_opt_out"] is True
    assert "renewal_opt_out" in connection.sql
    assert "auto_renew_enabled = false" in connection.sql
    assert connection.committed == 1