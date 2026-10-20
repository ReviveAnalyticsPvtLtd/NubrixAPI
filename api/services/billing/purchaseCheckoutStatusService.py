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
        status = reducePurchaseCheckoutStatus(attempt, intent, providerState, results)
        payable = (status.state == 'unpaid' and purpose == 'initial_purchase'
            and intent.razorpayOrderId and intent.expiresAt > _now()
            and intent.state in ('created', 'pending_provider_ack')
            and self.repository.mayResumeInitialCheckout(userId, attemptId))
        from dataclasses import replace
        status = replace(status, capabilities=PurchaseCheckoutCapabilities(mayResumeOriginal=bool(payable)))
        from api.services.billing.purchaseCheckoutRepository import storedPurchaseControl
        status = applyPurchaseControl(status, storedPurchaseControl(self.repository, userId, attemptId))
        if purpose == 'initial_purchase':
            from api.services.billing.purchaseCheckoutRepository import PurchaseCheckoutRepository
            status = PurchaseCheckoutRepository(self.repository.connectionFactory).projectPurchaseStatus(status)
        return status


def applyPurchaseControl(status, control):
    from dataclasses import replace
    if not control:
        return status
    accepted = control.get('cancellation_accepted_at')
    status = replace(status, operationRevision=int(control['operation_revision']),
        cancellationAcceptedAt=accepted.isoformat() if accepted else None)
    if status.state in ('completed', 'needs_attention'):
        return status
    if accepted:
        return replace(status, state='cancelled', pendingReason='ORIGINAL_CHECKOUT_CLOSED',
                       capabilities=PurchaseCheckoutCapabilities())
    if control.get('cancellation_requested_at'):
        return replace(status, state='cancellation_pending', pendingReason='CAPTURE_OUTCOME_PENDING',
                       capabilities=PurchaseCheckoutCapabilities())
    return status


def reducePurchaseCheckoutStatus(attempt, intent, providerState, results=(), mayResume=False):
    """One conservative reducer shared by status refresh and stored editor reads."""
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    frozen = ManualBillingRepository._json(attempt.get('metadata_json')).get('manualBilling', {})
    recorded = frozen.get('providerObservation', {})
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
    else:
        state, reason = 'status_unknown', 'PROVIDER_STATUS_UNKNOWN'

    return PurchaseCheckoutStatus(intent.userId, intent.invoiceId, intent.attemptId,
        intent.purpose, state, providerState, reason,
        capabilities=PurchaseCheckoutCapabilities(mayResumeOriginal=bool(mayResume and state == 'unpaid')))
