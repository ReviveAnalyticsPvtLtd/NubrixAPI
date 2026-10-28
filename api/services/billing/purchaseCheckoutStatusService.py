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
        if shown is not None and self._claimRefresh(userId, str(shown[0]['id'])):
            self._observe(shown[0])
        now = _now()
        return self._read(lambda cursor: presentation.presentPaymentStatus(cursor, userId, now))

    def _read(self, read):
        from psycopg2.extras import RealDictCursor
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                return read(cursor)
        return self.repository._run(operation)

    def _claimRefresh(self, userId, attemptId):
        """Commit a short per-attempt claim before any network work; never fake an observation."""
        from datetime import timedelta
        from psycopg2.extras import Json
        from api.services.billing.manualBillingRepository import _now, _utc
        def claim(cursor):
            cursor.execute('''SELECT * FROM public.billing_events
                WHERE id=%s AND user_id=%s AND event_category='payment_attempt' FOR UPDATE''', (attemptId, userId))
            attempt = cursor.fetchone()
            now = _now()
            if not attempt or not presentation.needsProviderRefresh(attempt, now):
                return False
            metadata = self.repository._json(attempt.get('metadata_json'))
            frozen = metadata.setdefault('manualBilling', {})
            claimed = _utc(frozen.get('providerRefreshClaimedAt'))
            if claimed and timedelta(0) <= now - claimed < presentation.PROVIDER_REFRESH_INTERVAL:
                return False
            frozen['providerRefreshClaimedAt'] = now.isoformat()
            cursor.execute('UPDATE public.billing_events SET metadata_json=%s WHERE id=%s AND user_id=%s',
                           (Json(metadata), attemptId, userId))
            return True
        return self._read(claim)

    def _observe(self, attempt):
        """Best effort: a failed refresh never hides the committed checkout, refunds or attention."""
        import re
        try:
            observation = self.recovery.inspectAttempt(attempt)
            self.recovery.applyObservation(attempt, observation)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) == 'PROVIDER_STATUS_UNKNOWN':
                return
            from utils.logger import logger
            code = str(exc) if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', str(exc)) else type(exc).__name__
            logger.error('Payment status refresh failed: attempt={} reason={}', str(attempt['id']), code)


def reducePurchaseCheckoutStatus(attempt, intent, now) -> PurchaseCheckoutStatus:
    """The editor's checkout binding, with the state from the one payment status reducer."""
    return PurchaseCheckoutStatus(intent.userId, intent.invoiceId, intent.attemptId, intent.purpose,
                                  presentation.checkoutState(attempt, now))
