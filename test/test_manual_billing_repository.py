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


def test_historical_row_requires_reviewed_backfill():
    connection = _ScriptedConnection()
    connection.on("is_canonical = true", results=[])
    connection.on("select id from public.subscriptions", results=[{"id":"history"}])
    with pytest.raises(ValueError, match="CANONICAL_BACKFILL_REQUIRED"):
        _repository(connection).ensureCanonicalSubscription("u-hist")
    assert connection.rolledBack == 1


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


from test.test_manual_billing_runtime import database, seed_payment, read_row, sqlTransaction, USER


def test_same_key_payload_conflict_is_rejected(database):
    repository,path=database
    evidence=seed_payment(database)
    with pytest.raises(ValueError,match="IDEMPOTENCY_CONFLICT"):
        repository.reserveCheckoutIntent(USER,"initial_purchase","invoice-one","different-payload",{
            "subscriptionId":read_row(path,"subscriptions")["id"],"invoiceId":"invoice-one","amount":3000})
    assert read_row(path,"Invoices")["status"]=="PAYMENT_PENDING"


def test_bound_order_cannot_be_replaced(database):
    repository,path=database
    evidence=seed_payment(database)
    with pytest.raises(ValueError,match="PROVIDER_ORDER_ALREADY_BOUND"):
        repository.bindProviderOrder(evidence.attemptId,{"id":"different-order"})
    assert read_row(path,"Invoices")["razorpay_order_id"]=="order-one"


def test_unknown_provider_ack_is_not_submitted_again(database):
    repository,path=database
    evidence=seed_payment(database)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE billing_events SET payment_status='created' WHERE id=?",(evidence.attemptId,))
    assert repository.claimProviderOrderCreation(evidence.attemptId)
    assert not repository.claimProviderOrderCreation(evidence.attemptId)


def test_cancellation_is_durable_and_idempotent_without_access_loss(database):
    repository,path=database
    repository.finalizeCapturedPayment(seed_payment(database))
    end=read_row(path,"subscriptions")["current_period_end"]
    repository.setRenewalOptOut(USER,True,"No longer needed","request-one")
    repository.setRenewalOptOut(USER,True,"Repeated","request-one")
    subscription=read_row(path,"subscriptions")
    assert subscription["renewal_opt_out"] and subscription["status"]=="active"
    assert subscription["current_period_end"]==end
    assert subscription["cancellation_reason"]=="No longer needed"
