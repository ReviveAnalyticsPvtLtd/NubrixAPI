"""Draft edits cannot create, cancel, capture or refund a purchase."""
import os

from api.services.billing.purchaseCheckoutContracts import PurchaseDraft, PurchaseEditorState


def editableCheckoutEnabled():
    return os.environ.get('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'false').lower() == 'true'


class PurchaseDraftService:
    def __init__(self, repository):
        self.repository = repository

    def get(self, userId):
        if not editableCheckoutEnabled():
            return PurchaseEditorState(userId, PurchaseDraft(userId))
        return self.repository.readEditor(userId)

    def save(self, userId, request):
        if not editableCheckoutEnabled():
            raise ValueError('EDITABLE_CHECKOUT_DISABLED')
        self.repository.saveDraft(userId, request.expectedRevision, request.domains, request.billingInterval)
        return self.repository.readEditor(userId)
