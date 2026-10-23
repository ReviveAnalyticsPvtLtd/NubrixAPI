"""Saved-draft Pay; the server decides open, reuse, replace or wait. No user cancellation exists."""
import os
from dataclasses import asdict

from api.services.billing.purchaseDraftService import editableCheckoutEnabled


def purchaseActionsReady():
    """Saved-draft Pay is offered when the editable checkout is enabled. Money rules have no runtime
    gate (spec 6): the refund worker always runs once deployed, and Pay separately requires its fresh
    heartbeat, so no new Order opens while nobody can refund."""
    return editableCheckoutEnabled()


def requirePurchaseActions():
    if not purchaseActionsReady():
        raise ValueError('PURCHASE_ACTIONS_UNAVAILABLE')


def validatePurchaseKey(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise ValueError('INVALID_REQUEST_KEY')


class PurchaseCheckoutService:
    def __init__(self, repository, provider):
        self.repository, self.provider = repository, provider

    def submit(self, userId, draftRevision, contact, pricingContext, expectedCatalogRevision, requestKey):
        requirePurchaseActions()
        validatePurchaseKey(requestKey)
        from api.services.billing.pricingContext import verifyPricingContext
        claims = verifyPricingContext(pricingContext, userId)
        if claims.get('catalogRevision') != expectedCatalogRevision:
            raise ValueError('PRICING_CHANGED')
        editor = self.repository.readEditor(userId)
        if editor.draft.revision != draftRevision:
            raise ValueError('PURCHASE_DRAFT_STALE')
        pricing = {'userId': userId, 'currency': claims['currency'], 'catalogRevision': expectedCatalogRevision,
            'domains': list(editor.draft.domains),
            'billingMode': 'annual_prepaid' if editor.draft.billingInterval == 'year' else 'monthly_prepaid'}
        from api.services.billing.manualPaymentService import ManualPaymentService
        request = self.repository.draftCheckoutRequest(userId, draftRevision, pricing, requestKey)
        intent = ManualPaymentService.forProduction(self.provider, self.repository).createCheckout(request)
        from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
        return {'editor': {'schemaVersion': 1, **asdict(self.repository.readEditor(userId))},
            'checkout': serializeCheckoutIntent(intent, os.environ.get('RAZORPAY_KEY_ID'), {'userContact': contact})}
