"""Explicit purchase actions; no cancellation is enabled before compensation readiness."""
import os
from dataclasses import asdict
from uuid import UUID

from api.services.billing.purchaseDraftService import editableCheckoutEnabled


def purchaseActionsReady():
    if not editableCheckoutEnabled() or os.environ.get('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', 'false').lower() != 'true':
        return False
    try:
        from api.services.billing.purchaseCompensationService import compensationReady
    except ImportError:
        return False
    return bool(compensationReady())


def requirePurchaseActions():
    if not purchaseActionsReady():
        raise ValueError('PURCHASE_ACTIONS_UNAVAILABLE')


def requireManualCaptureCurrency(currency):
    eligible = set(os.environ.get('SUBSCRIPTION_MANUAL_CAPTURE_CURRENCIES', '').split(','))
    if currency not in {'INR', 'USD'} or currency not in eligible:
        raise ValueError('PURCHASE_CAPTURE_NOT_ELIGIBLE')


def validatePurchaseKey(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise ValueError('INVALID_REQUEST_KEY')


class PurchaseCheckoutService:
    def __init__(self, repository, provider):
        self.repository, self.provider = repository, provider

    def cancel(self, userId, attemptId, expectedOperationRevision, requestKey):
        validatePurchaseKey(requestKey)
        UUID(attemptId)
        if not purchaseActionsReady():
            # Gate-off replay is observational. New cancellations remain denied.
            import psycopg2
            try:
                replay = self.repository.cancellationReplay(userId, attemptId, expectedOperationRevision, requestKey)
            except (psycopg2.Error, RuntimeError, OSError):
                replay = False
            if not replay:
                raise ValueError('PURCHASE_ACTIONS_UNAVAILABLE')
            return self.repository.readEditor(userId)
        if self.repository.cancellationReplay(userId, attemptId, expectedOperationRevision, requestKey):
            return self.repository.readEditor(userId)
        from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
        attempt = self.repository.attemptById(userId, attemptId)
        recovery = ManualBillingRecoveryService(self.repository, self.provider)
        observation = recovery.inspectAttempt(attempt) if attempt.get('provider_order_id') else None
        if observation is not None:
            try:
                recovery.applyObservation(attempt, observation)
            except RuntimeError as exc:
                if str(exc) != 'PROVIDER_STATUS_UNKNOWN':
                    raise
        return self.repository.acceptCancellation(userId, attemptId, expectedOperationRevision, requestKey, observation)

    def submit(self, userId, draftRevision, contact, pricingContext, expectedCatalogRevision, requestKey):
        requirePurchaseActions()
        validatePurchaseKey(requestKey)
        from api.services.billing.pricingContext import verifyPricingContext
        claims = verifyPricingContext(pricingContext, userId)
        if claims.get('catalogRevision') != expectedCatalogRevision:
            raise ValueError('PRICING_CHANGED')
        requireManualCaptureCurrency(claims['currency'])
        editor = self.repository.readEditor(userId)
        if editor.draft.revision != draftRevision:
            raise ValueError('PURCHASE_DRAFT_STALE')
        pricing = {'userId': userId, 'currency': claims['currency'], 'catalogRevision': expectedCatalogRevision,
            'domains': list(editor.draft.domains),
            'billingMode': 'annual_prepaid' if editor.draft.billingInterval == 'year' else 'monthly_prepaid'}
        intent = self.repository.reserveDraftCheckout(userId, draftRevision, pricing, requestKey)
        from api.services.billing.manualPaymentService import ManualPaymentService
        intent = ManualPaymentService.forProduction(self.provider, self.repository).openReservedCheckout(intent)
        from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
        return {'editor': {'schemaVersion': 1, **asdict(self.repository.readEditor(userId))},
            'checkout': serializeCheckoutIntent(intent, os.environ.get('RAZORPAY_KEY_ID'), {'userContact': contact})}
