"""Recover only durable original purchase identities; no recurring payments."""


class PurchaseOperationTask:
    def execute(self):
        from api.services.billing.purchaseCheckoutRepository import getPurchaseCheckoutRepository
        from api.services.billing.purchaseOperationWorker import PurchaseOperationWorker
        from api.services.subscriptions.subscriptionService import subscriptionService
        return PurchaseOperationWorker(getPurchaseCheckoutRepository(),subscriptionService.razorpayClient).execute()
