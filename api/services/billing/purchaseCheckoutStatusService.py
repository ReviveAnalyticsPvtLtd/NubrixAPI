"""Observe an owned payment without creating or submitting a new purchase."""
from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
from api.services.billing.purchaseCheckoutContracts import PurchaseCheckoutCapabilities, PurchaseCheckoutStatus


class PurchaseCheckoutStatusService:
    def __init__(self, repository, provider):
        self.repository = repository
        self.recovery = ManualBillingRecoveryService(repository, provider)

    def refresh(self, userId: str, attemptId: str) -> PurchaseCheckoutStatus:
        from api.services.billing.manualBillingRepository import _now

        attempt = self.repository.attemptById(userId, attemptId)
        results = ()
        providerState = 'unknown'
        if attempt.get('provider_order_id'):
            observation = self.recovery.inspectAttempt(attempt)
            try:
                results = self.recovery.applyObservation(attempt, observation)
            except RuntimeError as exc:
                if str(exc) != 'PROVIDER_STATUS_UNKNOWN':
                    raise
            if observation.complete:
                providerState = observation.outcome

        # Re-read after financial finalization or a concurrent callback. Ownership
        # must still exist; erasure must not turn a stale read into public facts.
        attempt = self.repository.attemptById(userId, attemptId)
        metadata = self.repository._json(attempt.get('metadata_json'))
        frozen = metadata.get('manualBilling', {})
        purpose = frozen.get('purpose')
        if purpose not in ('initial_purchase', 'renewal', 'expert_addition', 'topup'):
            raise ValueError('PURCHASE_CHECKOUT_METADATA_INVALID')
        intent = self.repository._intentFromAttemptRow(attempt, metadata, userId, purpose)
        recorded = frozen.get('providerObservation', {})
        payable = False
        if any(result.state == 'requires_reconciliation' for result in results):
            state, reason = 'needs_attention', 'PAYMENT_REQUIRES_RECONCILIATION'
        elif attempt['payment_status'] == 'captured':
            state, reason = 'completed', None
        elif intent.state == 'requires_reconciliation':
            state, reason = 'needs_attention', 'PAYMENT_REQUIRES_RECONCILIATION'
        elif frozen.get('closedAt') or intent.state in ('expired', 'cancelled', 'superseded'):
            state, reason = 'cancelled', 'ORIGINAL_CHECKOUT_CLOSED'
        elif providerState == 'unknown':
            state, reason = 'status_unknown', 'PROVIDER_STATUS_UNKNOWN'
        elif attempt['payment_status'] == 'authorized' or recorded.get('outcome') == 'authorized':
            state, reason = 'awaiting_capture', 'PAYMENT_AWAITING_CAPTURE'
        elif recorded.get('outcome') == 'processing':
            state, reason = 'processing', 'PAYMENT_PROCESSING'
        elif recorded.get('outcome') == 'unpaid' and recorded.get('complete'):
            state, reason = 'unpaid', None
            payable = bool(intent.razorpayOrderId and intent.expiresAt > _now()
                and intent.state in ('created', 'pending_provider_ack'))
        else:
            state, reason = 'status_unknown', 'PROVIDER_STATUS_UNKNOWN'

        # Other purposes retain their existing resumption APIs. Initial-purchase
        # capability must also respect a fresh owner/coverage restriction check.
        payable = payable and purpose == 'initial_purchase' and self.repository.mayResumeInitialCheckout(userId, attemptId)
        return PurchaseCheckoutStatus(userId, intent.invoiceId, intent.attemptId,
            purpose, state, providerState, reason,
            capabilities=PurchaseCheckoutCapabilities(mayResumeOriginal=payable))
