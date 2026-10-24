"""The owner's payment status: refresh the one Order whose money can change it, then present it.

A status read is one of the finalization paths (spec 4.4): captured money it observes is finalized
idempotently. The provider is read outside any lock or transaction, and nothing is ever opened.
"""
from api.services.billing import paymentStatusPresentation as presentation
from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
from api.services.billing.purchaseCheckoutContracts import PurchaseCheckoutStatus


class PurchaseCheckoutStatusService:
    def __init__(self, repository, provider):
        self.repository = repository
        self.recovery = ManualBillingRecoveryService(repository, provider)

    def refresh(self, userId: str) -> dict:
        from api.services.billing.manualBillingRepository import _now
        shown = self._read(lambda cursor: presentation.checkoutAttempt(cursor, userId, _now()))
        if shown is not None and presentation.needsProviderRefresh(shown[0], _now()):
            self._observe(shown[0])
        now = _now()
        return self._read(lambda cursor: presentation.presentPaymentStatus(cursor, userId, now))

    def _read(self, read):
        from psycopg2.extras import RealDictCursor
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                return read(cursor)
        return self.repository._run(operation)

    def _observe(self, attempt):
        observation = self.recovery.inspectAttempt(attempt)
        try:
            self.recovery.applyObservation(attempt, observation)
        except RuntimeError as exc:
            if str(exc) != 'PROVIDER_STATUS_UNKNOWN':
                raise


def reducePurchaseCheckoutStatus(attempt, intent, now) -> PurchaseCheckoutStatus:
    """The editor's checkout binding, with the state from the one payment status reducer."""
    return PurchaseCheckoutStatus(intent.userId, intent.invoiceId, intent.attemptId, intent.purpose,
                                  presentation.checkoutState(attempt, now))
