"""Every minute: execute reserved full refunds of captured payments that were never granted."""


class PaymentOperationTask:
    def execute(self):
        from api.services.billing.purchaseCheckoutRepository import getPurchaseCheckoutRepository
        from api.services.billing.paymentOperationWorker import PaymentOperationWorker
        from api.services.subscriptions.subscriptionService import subscriptionService
        return PaymentOperationWorker(getPurchaseCheckoutRepository(),subscriptionService.razorpayClient).execute()
