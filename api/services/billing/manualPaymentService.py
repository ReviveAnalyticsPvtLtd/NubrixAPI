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
import json
import os
from datetime import datetime, timedelta, timezone

from api.services.billing.manualBillingContracts import (
    CheckoutIntent,
    CheckoutRequest,
    FinalizationResult,
    VerifiedPaymentEvidence,
)
from utils.logger import logger


MANUAL_CHECKOUT_TTL_SECONDS = int(os.environ.get("MANUAL_CHECKOUT_TTL_SECONDS", "1800"))

_PURPOSE_ORDER_NOTE_TYPES = {
    "initial_purchase": "initial_subscription",
    "renewal": "manual_renewal",
    "expert_addition": "domain_upgrade_proration",
    "topup": "credit_topup",
}


class _OperationStore:
    """In-memory exactly-once guard doubles for unit tests.

    Production uses ManualBillingRepository: the same identities are enforced
    through owner locks, immutable provider-payment identity, invoice state
    and coverage/allocation identity inside one PostgreSQL transaction.
    """

    def __init__(self):
        self.finalizations = {}
        self.anomalies = []
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


class ManualPaymentService:
    def __init__(
        self,
        razorpayClient=None,
        supabaseClient=None,
        repository=None,
        store=None,
        now=None,
    ):
        self.razorpayClient = razorpayClient
        self.supabaseClient = supabaseClient
        if repository is None and store is None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repository = getManualBillingRepository()
        self.repository = repository
        self.store = store
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._intents = {}

    @classmethod
    def forProduction(cls, provider=None, repository=None):
        return cls(razorpayClient=provider, repository=repository)

    def _createDurableCheckout(self, request):
        self.repository.ensureCanonicalSubscription(request.userId)
        from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
        recovery = ManualBillingRecoveryService(self.repository, self.razorpayClient)
        for pending in self.repository.orderlessRecoveryRows(request.userId, request.purpose, request.billingMode):
            try:
                recovery.recoverAttempt(pending)
            except Exception as exc:
                raise RuntimeError('ORDER_ACK_UNKNOWN') from exc
        intent = self.repository.reserveCheckout(request)
        if intent.razorpayOrderId:
            return intent
        if self.razorpayClient is None:
            raise RuntimeError('PAYMENT_PROVIDER_NOT_CONFIGURED')
        if not self.repository.claimProviderOrderCreation(intent.attemptId):
            from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
            attempt = self.repository.attemptById(request.userId, intent.attemptId)
            ManualBillingRecoveryService(self.repository, self.razorpayClient).recoverAttempt(attempt)
            attempt = self.repository.attemptById(request.userId, intent.attemptId)
            intent = self.repository._intentFromAttemptRow(attempt,
                self.repository._json(attempt.get('metadata_json')), request.userId, request.purpose)
            if not intent.razorpayOrderId:
                raise RuntimeError('ORDER_ACK_UNKNOWN')
            return intent
        noteType = _PURPOSE_ORDER_NOTE_TYPES[request.purpose]
        if request.purpose == 'renewal' and request.billingMode == 'annual_prepaid':
            noteType = 'annual_renewal'
        notes = {'userId': request.userId, 'invoiceId': intent.invoiceId,
            'attemptId': intent.attemptId, 'type': noteType, 'purpose': request.purpose,
            'billingMode': request.billingMode, 'domains': ', '.join(intent.snapshot.get('domains') or [])}
        if request.purpose == 'topup':
            notes.update(packId=intent.snapshot['packId'], tokens=str(intent.snapshot['tokens']))
        try:
            order = self.razorpayClient.order.create({'amount': intent.amount, 'currency': intent.currency,
                'receipt': intent.attemptId, 'notes': notes})
            if (not order.get('id') or int(order.get('amount', -1)) != intent.amount
                    or order.get('currency') != intent.currency):
                raise ValueError('PROVIDER_ORDER_EVIDENCE_MISMATCH')
            return self.repository.bindProviderOrder(intent.attemptId, order)
        except Exception as exc:
            raise RuntimeError('ORDER_ACK_UNKNOWN') from exc

    # -- idempotency helpers --------------------------------------------------

    @staticmethod
    def _payloadHash(payload: dict) -> str:
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _requestKeyNamespace(self, userId: str, purpose: str, requestKey: str) -> str:
        return f"{purpose}:{userId}:{requestKey}"

    # -- checkout --------------------------------------------------------------

    def createCheckout(
        self,
        userId: str | CheckoutRequest,
        purpose: str = None,
        payload: dict = None,
        requestKey: str = None,
    ) -> CheckoutIntent:
        """Create (or idempotently return) a customer-free checkout intent.

        Reserves the durable intent BEFORE the provider call; if the order
        acknowledgement is unknown the attempt stays pending_provider_ack for
        reconciliation instead of creating a second charge opportunity.
        """
        if self.repository is not None:
            if not isinstance(userId, CheckoutRequest):
                raise TypeError('Production checkout requires CheckoutRequest')
            return self._createDurableCheckout(userId)
        if not requestKey:
            raise ValueError("requestKey is required")
        payloadHash = self._payloadHash(payload)
        namespace = self._requestKeyNamespace(userId, purpose, requestKey)

        existing = self.store.findIntent(namespace)
        if existing is not None:
            if existing.payloadHash != payloadHash:
                raise ValueError(
                    "IDEMPOTENCY_CONFLICT: same request key with a different payload"
                )
            return existing

        amount = int(payload.get("amount") or 0)
        currency = str(payload.get("currency") or "INR")
        expiresAt = self._resolveAttemptExpiry(userId, purpose, payload)

        attempt = CheckoutIntent(
            attemptId=namespace,
            invoiceId=str(payload.get("invoiceId") or "") or "",
            userId=userId,
            lifecycleId=str(payload.get("lifecycleId") or "") or "",
            purpose=purpose,
            billingMode=str(payload.get("billingMode") or "monthly_prepaid"),
            payloadHash=payloadHash,
            currency=currency,
            state="created",
            revision=int(payload.get("revision") or 1),
            amount=amount,
            expiresAt=expiresAt,
            razorpayOrderId=None,
            snapshot=dict(payload),
        )
        self._intents = None  # intents live in the shared store
        self.store.saveIntent(namespace, attempt)

        if self.razorpayClient is None:
            return attempt

        orderPayload = {
            "amount": amount,
            "currency": currency,
            "notes": {
                "userId": userId,
                "type": _PURPOSE_ORDER_NOTE_TYPES.get(purpose, purpose),
                "purpose": purpose,
                "invoiceId": attempt.invoiceId,
                "billingMode": attempt.billingMode,
                "domains": ", ".join(payload.get("domains") or []),
            },
        }
        receipt = payload.get("receipt")
        if receipt:
            orderPayload["receipt"] = receipt
        # No customer_id, no token enrollment, no recurring fields. Ever.

        try:
            order = self.razorpayClient.order.create(orderPayload)
        except Exception:
            # The reserved intent persists (pending_provider_ack concept);
            # creation failure must not create a second order opportunity on
            # retry with the same key.
            object.__setattr__(attempt, "state", "pending_provider_ack")
            raise

        object.__setattr__(attempt, "razorpayOrderId", order.get("id"))
        object.__setattr__(attempt, "state", "pending_provider_ack")
        return attempt

    def recoverUnresolvedCheckout(
        self,
        userId: str,
        purpose: str,
        requestKey: str,
    ) -> CheckoutIntent:
        """Return the stored pending intent for an unresolved order creation.

        Recovery resolves the original correlation through supported
        provider evidence (reconciliation); it never blindly issues another
        charge opportunity.
        """
        namespace = self._requestKeyNamespace(userId, purpose, requestKey)
        existing = self.store.findIntent(namespace)
        if existing is None:
            raise ValueError(
                f"No reserved intent for {namespace}: unknown order creation "
                "is investigated money, not a new order"
            )
        return existing

    def _resolveAttemptExpiry(
        self, userId: str, purpose: str, payload: dict
    ) -> datetime:
        """Local attempt lifetime.

        initial/topup: created + TTL. renewal/expert_addition: the earlier of
        created + TTL and the current period end (monthly deadline).
        """
        now = self.now()
        expiry = now + timedelta(seconds=MANUAL_CHECKOUT_TTL_SECONDS)
        periodEnd = payload.get("currentPeriodEnd")
        if purpose in ("renewal", "expert_addition") and periodEnd:
            from api.services.subscriptions.paymentValidationService import parseUtc

            parsedEnd = parseUtc(periodEnd)
            if parsedEnd is not None and parsedEnd < expiry:
                expiry = parsedEnd
        return expiry

    # -- shared finalization ----------------------------------------------------

    def finalizeCapturedPayment(
        self,
        evidence: VerifiedPaymentEvidence,
        attemptClosed: bool = False,
    ) -> FinalizationResult:
        """One authorized finalization per invoice.

        Rules (audited design 8.1-8.3):
        - authorized-only evidence never grants;
        - missing capture timing is reconciliation, never created_at fallback;
        - two distinct captures on one invoice grant once, excess money is a
          tracked anomaly;
        - failure after capture cannot downgrade the committed outcome;
        - a capture against a closed attempt is a financial anomaly, never a
          grant.
        """
        if self.repository is not None:
            return self.repository.finalizeCapturedPayment(evidence)
        invoiceId = evidence.invoiceId
        operationKey = f"finalize:{invoiceId}"

        financialStatus = (evidence.financialStatus or "").lower()

        # Capture evidence gate: never infer capture time from created_at.
        if financialStatus == "captured" and not evidence.timingVerified:
            if evidence.provenCaptureAt is not None and evidence.timingKind != "payment_created_at_only":
                pass  # verified by another admissible evidence kind
            else:
                return FinalizationResult(
                    invoiceId=invoiceId,
                    attemptId=evidence.attemptId,
                    state="requires_reconciliation",
                    creditState="pending",
                    finalized=False,
                    creditsRefilled=False,
                    renewalOptOut=False,
                    currentPeriod=None,
                    nextPeriod=None,
                    anomalyId=None,
                )

        if financialStatus != "captured":
            if financialStatus in ("authorized", "created"):
                return FinalizationResult(
                    invoiceId=invoiceId,
                    attemptId=evidence.attemptId,
                    state="awaiting_capture",
                    creditState="pending",
                    finalized=False,
                    creditsRefilled=False,
                    renewalOptOut=False,
                    currentPeriod=None,
                    nextPeriod=None,
                    anomalyId=None,
                )
            if financialStatus == "failed":
                # failed is not a terminal money state: the same payment may
                # capture later. A committed grant is never downgraded.
                claimed, committed = self.store.claimFinalization(
                    invoiceId, operationKey, {"paymentId": evidence.providerPaymentId, "state": "captured"}
                )
                if claimed is None and committed.get("state") == "captured":
                    return self._resultFromCommitted(invoiceId, evidence, "already_finalized")
                return FinalizationResult(
                    invoiceId=invoiceId,
                    attemptId=evidence.attemptId,
                    state="payment_failed",
                    creditState="pending",
                    finalized=False,
                    creditsRefilled=False,
                    renewalOptOut=False,
                    currentPeriod=None,
                    nextPeriod=None,
                    anomalyId=None,
                )

        # Captured evidence.
        if attemptClosed:
            self.store.recordAnomaly({
                "kind": "capture_against_closed_attempt",
                "invoiceId": invoiceId,
                "paymentId": evidence.providerPaymentId,
                "observedAt": evidence.observedAt.isoformat(),
            })
            return FinalizationResult(
                invoiceId=invoiceId,
                attemptId=evidence.attemptId,
                state="requires_reconciliation",
                creditState="pending",
                finalized=False,
                creditsRefilled=False,
                renewalOptOut=False,
                currentPeriod=None,
                nextPeriod=None,
                anomalyId=None,
            )

        claimed, committed = self.store.claimFinalization(
            invoiceId,
            operationKey,
            {
                "paymentId": evidence.providerPaymentId,
                "state": "captured",
                "amount": evidence.amount,
                "currency": evidence.currency,
            },
        )
        if claimed is None:
            committedPayment = committed.get("paymentId")
            if committedPayment != evidence.providerPaymentId:
                # A second distinct capture for one invoice: grant once,
                # track the excess funds.
                self.store.recordAnomaly({
                    "kind": "duplicate_capture_for_invoice",
                    "invoiceId": invoiceId,
                    "paymentId": evidence.providerPaymentId,
                    "committedPaymentId": committedPayment,
                    "amount": evidence.amount,
                    "currency": evidence.currency,
                })
            return self._resultFromCommitted(invoiceId, evidence, "already_finalized")

        return self._resultFromCommitted(invoiceId, evidence, "finalized")

    def _resultFromCommitted(
        self,
        invoiceId: str,
        evidence: VerifiedPaymentEvidence,
        state: str,
    ) -> FinalizationResult:
        finalized = state in ("finalized", "already_finalized")
        return FinalizationResult(
            invoiceId=invoiceId,
            attemptId=evidence.attemptId,
            state=state,
            creditState="pending_materialization",
            finalized=True if finalized else False,
            creditsRefilled=False,
            renewalOptOut=False,
            currentPeriod=None,
            nextPeriod=None,
            anomalyId=None,
        )

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
        )


manualPaymentService = ManualPaymentService()
