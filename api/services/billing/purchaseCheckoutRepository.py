"""Owned draft transactions and durable purchase-operation inventory; no provider I/O."""
from dataclasses import replace
from uuid import UUID

from psycopg2.extras import Json, RealDictCursor

from api.models import SavePurchaseDraftRequest
from api.services.billing.manualBillingRepository import ManualBillingRepository, _now, _utc
from api.services.billing.purchaseCheckoutContracts import PurchaseDraft, PurchaseEditorState, PurchaseCheckoutCapabilities
from api.services.billing.purchaseDraftService import editableCheckoutEnabled

TABLES = ('subscription_purchase_drafts', 'subscription_purchase_checkout_controls', 'subscription_purchase_operations')


class PurchaseCheckoutRepository(ManualBillingRepository):
    @staticmethod
    def _requireStorage(cursor):
        for table in TABLES:
            cursor.execute('SELECT to_regclass(%s) AS relation', ('public.' + table,))
            if not cursor.fetchone()['relation']:
                raise RuntimeError('PURCHASE_STORAGE_UNAVAILABLE')

    def _editorContext(self, cursor, userId):
        self._requireStorage(cursor)
        self._lockUser(cursor, userId)
        subscription = self._canonical(cursor, userId)
        coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
        allowed = coverage.currentPeriod is None and coverage.denialReason in (
            None, 'no_paid_coverage', 'no_subscription', 'expired')
        return subscription, allowed

    @staticmethod
    def _draft(userId, row):
        return PurchaseDraft(userId, int(row['revision']), tuple(row['domains']), row['billing_interval'],
                             row['updated_at'].isoformat())

    def readEditor(self, userId):
        def operation(connection):
            from api.services.billing.purchaseCheckoutStatusService import reducePurchaseCheckoutStatus
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                subscription, allowed = self._editorContext(cursor, userId)
                cursor.execute('SELECT * FROM public.subscription_purchase_drafts WHERE user_id=%s', (userId,))
                stored = cursor.fetchone()
                cursor.execute('''SELECT * FROM public.billing_events WHERE user_id=%s AND subscription_id=%s
                    AND event_category='payment_attempt' AND metadata_json->'manualBilling'->>'purpose'='initial_purchase'
                    ORDER BY created_at DESC,id DESC LIMIT 1''', (userId, subscription['id']))
                attempt = cursor.fetchone()
                draft, checkout = PurchaseDraft(userId), None
                if attempt:
                    metadata = self._json(attempt.get('metadata_json'))
                    frozen = metadata['manualBilling']
                    intent = self._intentFromAttemptRow(attempt, metadata, userId, 'initial_purchase')
                    observation = frozen.get('providerObservation', {})
                    checked = _utc(observation.get('checkedAt'))
                    fresh = checked and 0 <= (_now() - checked).total_seconds() <= 60
                    providerState = observation.get('outcome', 'unknown') if fresh and observation.get('complete') else 'unknown'
                    checkout = reducePurchaseCheckoutStatus(attempt, intent, providerState)
                    cursor.execute('SELECT * FROM public.subscription_purchase_checkout_controls WHERE attempt_id=%s AND user_id=%s',
                                   (attempt['id'], userId))
                    control = cursor.fetchone()
                    if control:
                        checkout = replace(checkout, operationRevision=int(control['operation_revision']),
                            cancellationAcceptedAt=control['cancellation_accepted_at'].isoformat() if control['cancellation_accepted_at'] else None)
                    if not frozen.get('closedAt') and intent.state not in ('cancelled', 'expired', 'superseded', 'captured'):
                        draft = PurchaseDraft(userId, domains=tuple(frozen.get('domains', [])),
                            billingInterval='year' if frozen.get('billingMode') == 'annual_prepaid' else 'month')
                capabilities = PurchaseCheckoutCapabilities(mayEditDraft=bool(allowed and editableCheckoutEnabled()))
                if checkout:
                    checkout = replace(checkout, capabilities=capabilities)
                return PurchaseEditorState(userId, self._draft(userId, stored) if stored else draft,
                    enabled=editableCheckoutEnabled(), checkout=checkout, capabilities=capabilities)
        return self._run(operation)

    def saveDraft(self, userId, expectedRevision, domains, billingInterval):
        if not editableCheckoutEnabled():
            raise ValueError('EDITABLE_CHECKOUT_DISABLED')
        request = SavePurchaseDraftRequest(expectedRevision=expectedRevision, domains=domains, billingInterval=billingInterval)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                _, allowed = self._editorContext(cursor, userId)
                if not allowed:
                    raise ValueError('PURCHASE_DRAFT_NOT_ALLOWED')
                cursor.execute('SELECT revision FROM public.subscription_purchase_drafts WHERE user_id=%s', (userId,))
                existing = cursor.fetchone()
                if (int(existing['revision']) if existing else 0) != request.expectedRevision:
                    raise ValueError('PURCHASE_DRAFT_STALE')
                cursor.execute('''INSERT INTO public.subscription_purchase_drafts(user_id,revision,domains,billing_interval)
                    VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET revision=EXCLUDED.revision,
                    domains=EXCLUDED.domains,billing_interval=EXCLUDED.billing_interval,updated_at=clock_timestamp()
                    WHERE subscription_purchase_drafts.revision=%s RETURNING *''',
                    (userId, request.expectedRevision + 1, Json(request.domains), request.billingInterval, request.expectedRevision))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('PURCHASE_DRAFT_STALE')
                return self._draft(userId, row)
        return self._run(operation)

    def getControl(self, userId, attemptId):
        UUID(attemptId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._requireStorage(cursor)
                self._lockUser(cursor, userId)
                cursor.execute('''SELECT c.* FROM public.subscription_purchase_checkout_controls c
                    JOIN public.billing_events b ON b.id=c.attempt_id AND b.invoice_id=c.invoice_id AND b.user_id=c.user_id
                    WHERE c.user_id=%s AND c.attempt_id=%s''', (userId, attemptId))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
                return dict(row)
        return self._run(operation)

    def listDueOperations(self, kind, afterId=None, limit=100):
        if kind not in ('capture', 'compensation') or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('INVALID_PURCHASE_OPERATION_SCAN')
        if afterId is not None:
            UUID(str(afterId))
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._requireStorage(cursor)
                cursor.execute('''SELECT * FROM public.subscription_purchase_operations WHERE kind=%s
                    AND state NOT IN ('succeeded','cancelled') AND next_retry_at<=clock_timestamp()
                    AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s''', (kind, afterId, afterId, limit))
                return list(cursor.fetchall())
        return self._run(operation)

    def hasUnresolvedPurchaseOperations(self, userId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._requireStorage(cursor)
                self._lockUser(cursor, userId)
                cursor.execute('''SELECT 1 FROM public.subscription_purchase_operations
                    WHERE user_id=%s AND state NOT IN ('succeeded','cancelled') LIMIT 1''', (userId,))
                return cursor.fetchone() is not None
        return self._run(operation)


def getPurchaseCheckoutRepository():
    return PurchaseCheckoutRepository()
