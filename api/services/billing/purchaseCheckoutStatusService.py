"""Observe an owned payment without creating or submitting a new purchase."""
from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
from api.services.billing.purchaseCheckoutContracts import PurchaseCheckoutStatus


class PurchaseCheckoutStatusService:
    def __init__(self, repository, provider):
        self.repository = repository
        self.recovery = ManualBillingRecoveryService(repository, provider)

    def refresh(self, userId: str, attemptId: str) -> PurchaseCheckoutStatus:
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
        if purpose == 'initial_purchase':
            from api.services.billing.purchaseCheckoutRepository import PurchaseCheckoutRepository
            status = PurchaseCheckoutRepository(self.repository.connectionFactory).projectPurchaseStatus(status)
        return status


def reducePurchaseCheckoutStatus(attempt, intent, providerState, results=()):
    """One conservative reducer shared by status refresh and stored editor reads.

    Pay is the only customer action. The public state says whether Pay waits for an
    earlier payment; pendingReason keeps the evidence behind a ready state.
    """
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    frozen = ManualBillingRepository._json(attempt.get('metadata_json')).get('manualBilling', {})
    recorded = frozen.get('providerObservation', {})
    if any(result.state == 'requires_reconciliation' for result in results):
        state, reason = 'ready', 'PAYMENT_REQUIRES_RECONCILIATION'
    elif attempt['payment_status'] == 'captured':
        state, reason = 'completed', None
    elif intent.state == 'requires_reconciliation':
        state, reason = 'ready', 'PAYMENT_REQUIRES_RECONCILIATION'
    elif frozen.get('closedAt') or intent.state in ('expired', 'cancelled', 'superseded'):
        state, reason = 'ready', 'ORIGINAL_CHECKOUT_CLOSED'
    elif providerState == 'unknown':
        state, reason = 'ready', 'PROVIDER_STATUS_UNKNOWN'
    elif attempt['payment_status'] == 'authorized' or recorded.get('outcome') == 'authorized':
        state, reason = 'confirming_earlier_payment', 'PAYMENT_AWAITING_CAPTURE'
    elif recorded.get('outcome') == 'processing':
        state, reason = 'confirming_earlier_payment', 'PAYMENT_PROCESSING'
    elif recorded.get('outcome') == 'unpaid' and recorded.get('complete'):
        state, reason = 'ready', None
    else:
        state, reason = 'ready', 'PROVIDER_STATUS_UNKNOWN'
    return PurchaseCheckoutStatus(intent.userId, intent.invoiceId, intent.attemptId,
        intent.purpose, state, providerState, reason)
