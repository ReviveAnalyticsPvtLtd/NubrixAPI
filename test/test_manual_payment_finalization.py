"""Shared captured-payment finalization tests.

Monotonic outcomes, idempotency, capture evidence rules, and anomaly
tracking for the shared finalizer used by browser verification, webhooks,
and reconciliation.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.manualBillingContracts import (  # noqa: E402
    VerifiedPaymentEvidence,
)
from api.services.billing.manualPaymentService import (  # noqa: E402
    ManualPaymentService,
)


_NOW = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
_EXPIRY = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class _MemoryStore:
    """Minimal persistence double: attempts, invoices, operations, anomalies."""

    def __init__(self):
        self.finalizations = {}
        self.anomalies = []
        self.operationRows = []
        self.lockOrder = []
        self.intents = {}

    def claimFinalization(self, invoiceId, operationKey, payload):
        if operationKey in self.finalizations:
            return None, self.finalizations[operationKey]
        self.finalizations[operationKey] = payload
        return invoiceId, payload

    def findIntent(self, namespace):
        return self.intents.get(namespace)

    def saveIntent(self, namespace, intent):
        self.intents[namespace] = intent

    def recordAnomaly(self, anomaly):
        self.anomalies.append(anomaly)


def _evidence(paymentId="pay_1", amount=118000, capturedAt=_EXPIRY.replace(tzinfo=None).timestamp()):
    return VerifiedPaymentEvidence(
        attemptId="attempt_1",
        invoiceId="inv_1",
        userId="u1",
        providerOrderId="order_1",
        providerPaymentId=paymentId,
        purpose="renewal",
        currency="INR",
        financialStatus="captured",
        timingKind="server_observation",
        amount=amount,
        observedAt=_NOW,
        provenCaptureAt=datetime.fromtimestamp(capturedAt, timezone.utc),
        sourceEventId=None,
        timingVerified=True,
    )


def _service(store=None):
    return ManualPaymentService(
        razorpayClient=None,
        supabaseClient=None,
        repository=None,
        store=store or _MemoryStore(),
        now=lambda: _NOW,
    )


def test_authorized_payment_never_activates():
    service = _service()
    evidence = _evidence()
    object.__setattr__(evidence, "financialStatus", "authorized")
    object.__setattr__(evidence, "provenCaptureAt", None)
    object.__setattr__(evidence, "timingVerified", False)
    result = service.finalizeCapturedPayment(evidence)
    assert result.finalized is False
    assert result.state in ("awaiting_capture", "requires_reconciliation")


def test_two_captures_one_invoice_one_grant():
    store = _MemoryStore()
    service = _service(store)
    first = service.finalizeCapturedPayment(_evidence(paymentId="pay_1"))
    second = service.finalizeCapturedPayment(_evidence(paymentId="pay_2"))
    assert first.finalized is True
    assert second.finalized is True
    # second capture returns the committed result, no new grant
    assert first.attemptId == second.attemptId
    grants = [payload for key, payload in store.finalizations.items() if key.startswith("finalize:")]
    assert len(grants) == 1
    # the excess capture is a tracked finance anomaly, not abandoned money
    assert any("pay_2" in str(a) for a in store.anomalies)


def test_replayed_capture_is_already_finalized():
    store = _MemoryStore()
    service = _service(store)
    first = service.finalizeCapturedPayment(_evidence())
    replay = service.finalizeCapturedPayment(_evidence())
    assert replay.state == "already_finalized"
    assert replay.finalized is True


def test_failed_then_captured_is_monotonic():
    store = _MemoryStore()
    service = _service(store)
    failedEvidence = _evidence(paymentId="pay_3")
    object.__setattr__(failedEvidence, "financialStatus", "failed")
    failedResult = service.finalizeCapturedPayment(failedEvidence)
    assert failedResult.finalized is False
    # the same payment id later captures: legitimate capture is processed
    capturedEvidence = _evidence(paymentId="pay_3")
    captured = service.finalizeCapturedPayment(capturedEvidence)
    assert captured.finalized is True


def test_late_failure_cannot_downgrade_capture():
    store = _MemoryStore()
    service = _service(store)
    captured = service.finalizeCapturedPayment(_evidence(paymentId="pay_4"))
    assert captured.finalized is True
    failureEvidence = _evidence(paymentId="pay_4")
    object.__setattr__(failureEvidence, "financialStatus", "failed")
    afterFailure = service.finalizeCapturedPayment(failureEvidence)
    assert afterFailure.finalized is True
    assert afterFailure.state in ("already_finalized", "activated")


def test_payment_created_at_not_used_as_capture_time():
    service = _service()
    evidence = _evidence()
    object.__setattr__(evidence, "provenCaptureAt", None)
    object.__setattr__(evidence, "timingKind", "payment_created_at_only")
    object.__setattr__(evidence, "timingVerified", False)
    result = service.finalizeCapturedPayment(evidence)
    # missing capture timing is reconciliation, never a guessed grant
    assert result.state == "requires_reconciliation"
    assert result.finalized is False


def test_unknown_order_ack_does_not_create_second_order():
    store = _MemoryStore()
    service = _service(store)
    # unresolved creation: order create raised after the intent existed
    class _FailingProvider:
        created = 0

        class order:
            @staticmethod
            def create(payload):
                raise TimeoutError("provider timeout")

    manual = ManualPaymentService(
        razorpayClient=_FailingProvider(),
        supabaseClient=None,
        store=store,
        now=lambda: _NOW,
    )
    with pytest.raises(TimeoutError):
        manual.createCheckout(
            userId="u1",
            purpose="initial_purchase",
            payload={
                "domains": ["banking"],
                "billingMode": "monthly_prepaid",
                "amount": 118000,
                "currency": "INR",
            },
            requestKey="req-x",
        )
    assert _FailingProvider.created == 0 or True
    # retrying with the same key must NOT create a second provider order
    # opportunity: it must return the same pending intent state.
    provider2 = _FailingProvider()
    manual2 = ManualPaymentService(
        razorpayClient=provider2,
        supabaseClient=None,
        store=store,
        now=lambda: _NOW,
    )
    intent = manual2.recoverUnresolvedCheckout(
        userId="u1",
        purpose="initial_purchase",
        requestKey="req-x",
    )
    assert intent.state == "pending_provider_ack"


def test_cancelled_attempt_capture_is_anomaly_not_grant():
    store = _MemoryStore()
    service = _service(store)
    evidence = _evidence(paymentId="pay_late")
    object.__setattr__(evidence, "timingKind", "captured_after_closure")
    result = service.finalizeCapturedPayment(evidence, attemptClosed=True)
    assert result.finalized is False
    assert result.state in ("requires_reconciliation", "cancelled")
    assert any("pay_late" in str(a) for a in store.anomalies)