"""Customer-free manual payment orchestration.

Coordinates checkout intents, Razorpay orders, provider evidence and the
shared captured-payment finalizer. No Razorpay Customer create/fetch/edit,
token enrollment, or recurring parameters anywhere in this module. Same
contact across accounts is checkout prefill, never identity.

Ownership chain: authenticated application user -> owned local invoice and
subscription lifecycle -> persisted payment attempt / Razorpay order ->
verified captured payment for that order -> one authorized finalization.
"""

__all__ = ["ManualPaymentService", "manualPaymentService"]


import hashlib
import os
from datetime import datetime, timezone

from api.services.billing.manualBillingContracts import (
    CheckoutIntent,
    CheckoutRequest,
    FinalizationResult,
    VerifiedPaymentEvidence,
)
from razorpay.errors import BadRequestError

from utils.logger import logger


_PURPOSE_ORDER_NOTE_TYPES = {
    "initial_purchase": "initial_subscription",
    "renewal": "manual_renewal",
    "expert_addition": "domain_upgrade_proration",
    "topup": "credit_topup",
}


class ManualPaymentService:
    def __init__(
        self,
        razorpayClient=None,
        repository=None,
    ):
        self.razorpayClient = razorpayClient
        if repository is None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repository = getManualBillingRepository()
        self.repository = repository

    @classmethod
    def forProduction(cls, provider=None, repository=None):
        return cls(razorpayClient=provider, repository=repository)

    def _createDurableCheckout(self, request):
        self.repository.ensureCanonicalSubscription(request.userId)
        replay = self.repository.reserveCheckout(request, replayOnly=True)
        if replay is not None and (replay.state in ('captured', 'expired', 'cancelled', 'superseded', 'failed')
                or request.purpose != 'renewal' and replay.razorpayOrderId):
            # A renewal replay with an Order is refreshed by inspecting the slot's live attempt below.
            return replay
        from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
        recovery = ManualBillingRecoveryService(self.repository, self.razorpayClient)
        # The initial purchase slot spans billing modes.
        pendingRows = self.repository.orderlessRecoveryRows(request.userId, request.purpose,
            None if request.purpose == 'initial_purchase' else request.billingMode)
        for pending in pendingRows:
            try:
                recovery.recoverAttempt(pending)
            except Exception as exc:
                raise RuntimeError('PROVIDER_STATUS_UNKNOWN' if pending.get('provider_order_id') else 'ORDER_ACK_UNKNOWN') from exc
        # Provider reads happen before the owner lock (spec 4.3); the reservation re-reads the
        # slot's live attempt under the lock and is busy if it changed since this snapshot.
        live = self.repository.liveSlotAttempt(request)
        inspection = inspected = None
        if live is not None and live.get('provider_order_id'):
            inspection = recovery.inspectAttempt(live)
            if not inspection.complete:
                raise RuntimeError('PAYMENT_SERVICE_BUSY')  # Nothing closed, nothing opened.
            try:
                # Finalize captured money first; a granted payment settles the slot.
                recovery.applyObservation(live, inspection)
            except RuntimeError as exc:
                if str(exc) != 'PROVIDER_STATUS_UNKNOWN':
                    raise
                # Recorded; the slot decision treats an unclassified Order as busy,
                # or refunded money on it as finalize-first.
            inspected = self.repository.attemptById(request.userId, str(live['id']))
            if not self._recordedOnlyThisSnapshot(live, inspected, inspection):
                raise RuntimeError('PAYMENT_SERVICE_BUSY')  # Other evidence arrived meanwhile: never act on it.
        evidence = {} if inspection is None else {'inspection': inspection, 'inspectedAttempt': inspected}
        intent = self.repository.reserveCheckout(request, **evidence)
        return self.openReservedCheckout(intent)

    def _recordedOnlyThisSnapshot(self, before, after, snapshot):
        """The attempt changed only as recording this snapshot explains (or its capture settled it,
        or finalizing its capture closed it for a full refund)."""
        from api.services.billing.manualBillingRepository import _utc
        if after.get('payment_status') == 'captured':
            return True
        billing = self.repository._json(after.get('metadata_json')).get('manualBilling', {})
        observed = billing.get('providerObservation', {})
        if billing.get('closedReason') == 'PAYMENT_REFUNDED' and before.get('payment_status') != after.get('payment_status'):
            return _utc(observed.get('checkedAt')) == snapshot.observedAt
        expected = before.get('payment_status')
        if snapshot.outcome == 'authorized' and expected in ('created', 'pending_provider_ack'):
            expected = 'authorized'
        return after.get('payment_status') == expected and _utc(observed.get('checkedAt')) == snapshot.observedAt

    def openReservedCheckout(self, intent):
        """Submit only an already committed original reservation."""
        if intent.razorpayOrderId or intent.state not in ('created', 'pending_provider_ack', 'authorized'):
            # Historical-key replays retain closed intent identity. They
            # must never submit or recover an unsubmitted cancelled order.
            return intent
        if self.razorpayClient is None:
            raise RuntimeError('PAYMENT_PROVIDER_NOT_CONFIGURED')
        from api.services.billing.paymentPolicy import orderPaymentOptions, paymentRulesEffectiveAt
        from api.services.billing.purchaseProvider import purchaseProviderOptions
        paymentRulesEffectiveAt()  # unset or invalid cutoff: no new Order may open
        # The Order is told exactly the window recorded on the attempt at reservation.
        options = orderPaymentOptions(intent.snapshot.get('captureWindowMinutes'))
        providerOptions = purchaseProviderOptions()
        if not self.repository.claimProviderOrderCreation(intent.attemptId):
            from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
            attempt = self.repository.attemptById(intent.userId, intent.attemptId)
            if not attempt.get('provider_order_id') and attempt.get('payment_status') not in ('created', 'pending_provider_ack', 'authorized'):
                # Replaced by a concurrent Pay before this Order was requested: nothing was sent.
                raise RuntimeError('PAYMENT_SERVICE_BUSY')
            ManualBillingRecoveryService(self.repository, self.razorpayClient).recoverAttempt(attempt)
            attempt = self.repository.attemptById(intent.userId, intent.attemptId)
            intent = self.repository._intentFromAttemptRow(attempt,
                self.repository._json(attempt.get('metadata_json')), intent.userId, intent.purpose)
            if not intent.razorpayOrderId:
                raise RuntimeError('ORDER_ACK_UNKNOWN')
            return intent
        noteType = _PURPOSE_ORDER_NOTE_TYPES[intent.purpose]
        if intent.purpose == 'renewal' and intent.billingMode == 'annual_prepaid':
            noteType = 'annual_renewal'
        notes = {'userId': intent.userId, 'invoiceId': intent.invoiceId,
            'attemptId': intent.attemptId, 'type': noteType, 'purpose': intent.purpose,
            'billingMode': intent.billingMode, 'domains': ', '.join(intent.snapshot.get('domains') or [])}
        if intent.purpose == 'topup':
            notes.update(packId=intent.snapshot['packId'], tokens=str(intent.snapshot['tokens']))
        payload = {'amount': intent.amount, 'currency': intent.currency, 'receipt': intent.attemptId, 'notes': notes}
        payload['payment'] = options
        try:
            order = self.razorpayClient.order.create(payload, **providerOptions)
            if (not order.get('id') or int(order.get('amount', -1)) != intent.amount
                    or order.get('currency') != intent.currency):
                raise ValueError('PROVIDER_ORDER_EVIDENCE_MISMATCH')
            return self.repository.bindProviderOrder(intent.attemptId, order)
        except BadRequestError as exc:
            # A provider 4xx proves no Order exists: close now, no recovery wait.
            if not self.repository.closeUncreatedOrder(intent.attemptId, providerRejected=True):
                # Another transaction may already have bound the Order. Never
                # claim absence or overwrite that committed financial identity.
                raise RuntimeError('ORDER_ACK_UNKNOWN') from exc
            raise RuntimeError('PAYMENT_SERVICE_BUSY') from exc
        except Exception as exc:
            raise RuntimeError('ORDER_ACK_UNKNOWN') from exc

    def createCheckout(self, request: CheckoutRequest) -> CheckoutIntent:
        """Reserve durably before opening or reusing the provider Order."""
        if not isinstance(request, CheckoutRequest):
            raise TypeError('Production checkout requires CheckoutRequest')
        return self._createDurableCheckout(request)

    def finalizeCapturedPayment(self, evidence: VerifiedPaymentEvidence) -> FinalizationResult:
        """Grant or reserve a full refund under the repository's owner transaction."""
        return self.repository.finalizeCapturedPayment(evidence)

    # -- provider evidence -------------------------------------------------------

    @staticmethod
    def verifyCheckoutSignature(orderId: str, paymentId: str, signature: str) -> bool:
        """HMAC-SHA256 checkout signature verification against order|payment."""
        import hmac as hmacModule

        secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        message = f"{orderId}|{paymentId}".encode()
        expected = hmacModule.new(secret.encode(), message, hashlib.sha256).hexdigest()
        return hmacModule.compare_digest(expected, signature)

    @staticmethod
    def buildPaymentEvidence(
        payment: dict,
        *,
        attemptId: str,
        invoiceId: str,
        userId: str,
        purpose: str,
        observedAt: datetime,
        sourceEventId: str | None = None,
        serverObservedCapture: bool = True,
    ) -> VerifiedPaymentEvidence:
        """Build trusted evidence from a validated provider payment entity.

        payment.created_at is NOT capture time. `provenCaptureAt` is set only
        from admissible evidence (server observation of captured state); a
        payment entity carrying captured_at is treated as provider-attested
        evidence when the caller explicitly validated the fetch.
        """
        from api.services.billing.captureRules import providerCreatedAt
        status = str(payment.get("status") or "").lower()
        capturedAt = payment.get("captured_at")
        provenCaptureAt = None
        timingKind = "server_observation" if serverObservedCapture else "unverified"
        timingVerified = False
        if status == "captured":
            if capturedAt is not None:
                provenCaptureAt = datetime.fromtimestamp(int(capturedAt), timezone.utc)
                timingKind = "provider_captured_at"
                timingVerified = True
            elif serverObservedCapture:
                # First server observation of already-captured state proves
                # timely capture for deadline purposes (design 8.3).
                provenCaptureAt = observedAt
                timingKind = "server_observation"
                timingVerified = True
        return VerifiedPaymentEvidence(
            attemptId=attemptId,
            invoiceId=invoiceId,
            userId=userId,
            providerOrderId=str(payment.get("order_id") or ""),
            providerPaymentId=str(payment.get("id") or ""),
            purpose=purpose,
            currency=str(payment.get("currency") or "INR"),
            financialStatus=status,
            timingKind=timingKind,
            amount=int(payment.get("amount") or 0),
            observedAt=observedAt,
            provenCaptureAt=provenCaptureAt,
            sourceEventId=sourceEventId,
            timingVerified=timingVerified,
            paymentCreatedAt=providerCreatedAt(payment),
        )


manualPaymentService = ManualPaymentService()
