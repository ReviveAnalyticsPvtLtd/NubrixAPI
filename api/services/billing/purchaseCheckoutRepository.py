"""Owned draft transactions, financial bindings and durable recovery inventory."""
from uuid import UUID

from psycopg2.extras import Json, RealDictCursor

from api.models import SavePurchaseDraftRequest
from api.services.billing.manualBillingRepository import ManualBillingRepository, _now, _utc
from api.services.billing.purchaseCheckoutContracts import PurchaseDraft, PurchaseEditorState
from api.services.billing.purchaseDraftService import editableCheckoutEnabled
from api.services.billing.paymentOperationRepository import PaymentOperationRepositoryMixin

TABLES = ('subscription_purchase_drafts', 'payment_slots', 'payment_slot_controls', 'payment_operations')


class PurchaseCheckoutRepository(PaymentOperationRepositoryMixin, ManualBillingRepository):
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
                    if not frozen.get('closedAt') and intent.state not in ('cancelled', 'expired', 'superseded', 'captured'):
                        draft = PurchaseDraft(userId, domains=tuple(frozen.get('domains', [])),
                            billingInterval='year' if frozen.get('billingMode') == 'annual_prepaid' else 'month')
                draft = self._draft(userId, stored) if stored else draft
                checkout,capabilities = self._purchasePresentation(cursor,checkout,draft,allowed)
                return PurchaseEditorState(userId, draft,
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
                cursor.execute('''SELECT c.* FROM public.payment_slot_controls c
                    JOIN public.billing_events b ON b.id=c.attempt_id AND b.invoice_id=c.invoice_id AND b.user_id=c.user_id
                    WHERE c.user_id=%s AND c.attempt_id=%s''', (userId, attemptId))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
                return dict(row)
        return self._run(operation)

    def listDueOperations(self, kind, afterId=None, limit=100):
        if kind != 'refund' or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('INVALID_PAYMENT_OPERATION_SCAN')
        if afterId is not None:
            UUID(str(afterId))
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._requireStorage(cursor)
                cursor.execute('''SELECT * FROM public.payment_operations WHERE kind=%s
                    AND state NOT IN ('succeeded','cancelled') AND next_retry_at<=clock_timestamp()
                    AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s''', (kind, afterId, afterId, limit))
                return list(cursor.fetchall())
        return self._run(operation)

    def hasUnresolvedPurchaseOperations(self, userId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._requireStorage(cursor)
                self._lockUser(cursor, userId)
                cursor.execute('''SELECT 1 FROM public.payment_operations
                    WHERE user_id=%s AND state NOT IN ('succeeded','cancelled') LIMIT 1''', (userId,))
                return cursor.fetchone() is not None
        return self._run(operation)

    def draftCheckoutRequest(self, userId, draftRevision, pricing, requestKey):
        """The saved-draft Pay request; the caller reserves it through the shared reservation path."""
        from api.services.billing.purchaseCheckoutService import requirePurchaseActions, validatePurchaseKey
        from api.services.billing.manualBillingContracts import CheckoutRequest
        requirePurchaseActions()
        validatePurchaseKey(requestKey)
        if pricing.get('userId') != userId or set(pricing) != {'userId', 'currency', 'catalogRevision', 'domains', 'billingMode'}:
            raise ValueError('PURCHASE_PRICING_INVALID')
        return CheckoutRequest(userId, 'initial_purchase', pricing['billingMode'],
            {'domains': pricing['domains']}, requestKey, currency=pricing['currency'],
            expectedCatalogRevision=pricing['catalogRevision'], purchaseDraftRevision=draftRevision)

def guardInitialPurchaseLocked(billing, cursor, request, subscription, now):
    """Runs inside the original invoice reservation transaction, including old clients."""
    from api.services.billing.purchaseCheckoutService import purchaseActionsReady, requirePurchaseActions
    cursor.execute("SELECT to_regclass('public.payment_slot_controls') AS relation")
    present = cursor.fetchone()['relation']
    if not present:
        if request.purchaseDraftRevision is not None:
            raise RuntimeError('PURCHASE_STORAGE_UNAVAILABLE')
        return
    PurchaseCheckoutRepository._requireStorage(cursor)
    if request.purchaseDraftRevision is None and request.requestKey:
        cursor.execute('SELECT metadata_json FROM public.billing_events WHERE user_id=%s AND idempotency_key=%s',
            (request.userId, f'initial_purchase:{request.userId}:{request.requestKey}'))
        replay = cursor.fetchone()
        if replay and billing._json(replay['metadata_json']).get('manualBilling', {}).get('closedReason') != 'ORDER_NOT_CREATED':
            return  # Historical-key identity is observable, never a new purchase.
    if request.purchaseDraftRevision is None:
        if purchaseActionsReady():
            raise ValueError('PURCHASE_DRAFT_REQUIRED')
        return
    requirePurchaseActions()
    cursor.execute('SELECT * FROM public.subscription_purchase_drafts WHERE user_id=%s', (request.userId,))
    draft = cursor.fetchone()
    if not draft or type(request.purchaseDraftRevision) is not int or draft['revision'] != request.purchaseDraftRevision:
        raise ValueError('PURCHASE_DRAFT_STALE')
    if (sorted(draft['domains']) != sorted(request.payload['domains'])
            or draft['billing_interval'] != ('year' if request.billingMode == 'annual_prepaid' else 'month')):
        raise ValueError('PURCHASE_DRAFT_STALE')
    cursor.execute('SELECT DISTINCT currency FROM public."Invoices" WHERE "userId"=%s AND status=\'PAID\'', (request.userId,))
    currencies = {row['currency'] for row in cursor.fetchall()}
    if currencies and currencies != {request.currency}:
        raise ValueError('BILLING_CURRENCY_CONFLICT')


def requirePaymentWorkerLocked(cursor):
    """No new Order opens while nobody can recover or refund it."""
    if _storagePresent(cursor) and not PurchaseCheckoutRepository._paymentWorkerFresh(cursor):
        raise RuntimeError('PAYMENT_ACTIONS_UNAVAILABLE')


def openSlotLocked(cursor, userId: str, kind: str, slotKey: str) -> str:
    """Reuse the owner's unsettled slot for this kind and key, or open one. Runs under the owner lock."""
    cursor.execute('''INSERT INTO public.payment_slots(user_id,kind,slot_key) VALUES(%s,%s,%s)
        ON CONFLICT(user_id,kind,slot_key) WHERE settled_payment_id IS NULL DO NOTHING RETURNING slot_id''',
        (userId, kind, slotKey))
    row = cursor.fetchone()
    if not row:
        cursor.execute('''SELECT slot_id FROM public.payment_slots WHERE user_id=%s AND kind=%s AND slot_key=%s
            AND settled_payment_id IS NULL''', (userId, kind, slotKey))
        row = cursor.fetchone()
    return str(row['slot_id'] if isinstance(row, dict) else row[0])


def _storagePresent(cursor):
    cursor.execute("SELECT to_regclass('public.payment_slot_controls') AS relation")
    row = cursor.fetchone()
    return bool(row['relation'] if isinstance(row, dict) else row[0])


def bindPurchaseControlLocked(cursor, request, intent, slotKey):
    """Bind a new attempt of any purpose to its slot; an existing control is never rebound."""
    from api.services.billing.paymentSlots import slotKind
    if not _storagePresent(cursor):
        return
    cursor.execute('SELECT 1 FROM public.payment_slot_controls WHERE attempt_id=%s', (intent.attemptId,))
    if cursor.fetchone():
        return  # Idempotent replay: discover the control before opening any slot.
    slotId = openSlotLocked(cursor, request.userId, slotKind(request.purpose), slotKey)
    cursor.execute('''INSERT INTO public.payment_slot_controls
        (attempt_id,slot_id,invoice_id,user_id,purpose,draft_revision,capture_policy)
        VALUES(%s,%s,%s,%s,%s,%s,'automatic')''',
        (intent.attemptId, slotId, intent.invoiceId, request.userId, request.purpose,
         request.purchaseDraftRevision if request.purpose == 'initial_purchase' else None))


def adoptStartedTermControlsLocked(cursor, attemptId, slotAttemptIds):
    """Move the controls of a started, unpaid next term into the open Pro term slot of ``attemptId``.

    A renewal reserved before its term end E was bound to slot ISO(E). Once E passed with no
    current term it buys the open term (spec 4.1, 5 case 4b), so its controls join the open slot:
    supersession stays within one slot and a later grant settles one slot instance. The storage
    guard permits exactly this move (ISO(E) to open, same owner, both unsettled, E <= now).
    """
    if not _storagePresent(cursor) or not slotAttemptIds:
        return
    cursor.execute('''SELECT s.slot_id FROM public.payment_slot_controls c
        JOIN public.payment_slots s ON s.slot_id=c.slot_id
        WHERE c.attempt_id=%s AND s.kind='pro_term' AND s.slot_key='open' AND s.settled_payment_id IS NULL''', (attemptId,))
    target = cursor.fetchone()
    if not target:
        return
    cursor.execute('''SELECT c.attempt_id FROM public.payment_slot_controls c
        JOIN public.payment_slots s ON s.slot_id=c.slot_id
        WHERE c.attempt_id = ANY(%s::uuid[]) AND c.slot_id<>%s AND s.kind='pro_term' AND s.slot_key<>'open'
          AND s.settled_payment_id IS NULL ORDER BY c.attempt_id FOR UPDATE OF c''',
        (list(slotAttemptIds), target['slot_id']))
    for row in cursor.fetchall():
        cursor.execute('''UPDATE public.payment_slot_controls SET slot_id=%s,
            operation_revision=operation_revision+1,updated_at=clock_timestamp() WHERE attempt_id=%s''',
            (target['slot_id'], row['attempt_id']))


_LIVE = ('created', 'pending_provider_ack', 'authorized')


def retireSupersededAttemptLocked(cursor, attempt, now, replacementInvoiceId=None):
    """Close the attempt (and its non-renewal invoice) so a replacement may become the live attempt."""
    frozen = ManualBillingRepository._json(attempt.get('metadata_json')).get('manualBilling', {})
    if frozen.get('closedAt'):
        if frozen.get('closedReason') != 'SUPERSEDED':
            raise ValueError('PAYMENT_SUPERSESSION_INVALID')
        return
    closure = {'closedAt': now.isoformat(), 'closedReason': 'SUPERSEDED'}
    if frozen.get('purpose') != 'renewal':
        cursor.execute('SELECT * FROM public."Invoices" WHERE id=%s FOR UPDATE', (attempt['invoice_id'],))
        invoice = cursor.fetchone()
        if not invoice or str(invoice['status']).upper() not in ('UPCOMING', 'PAYMENT_PENDING'):
            raise ValueError('PAYMENT_SUPERSESSION_INVALID')
        metadata = ManualBillingRepository._json(invoice.get('metadata_json'))
        metadata.setdefault('manualBilling', {}).update(closure)
        if replacementInvoiceId is not None:
            metadata['manualBilling']['supersededByInvoiceId'] = str(replacementInvoiceId)
        cursor.execute('UPDATE public."Invoices" SET status=\'VOID\',metadata_json=%s WHERE id=%s',
                       (Json(metadata), invoice['id']))
        cursor.execute('''SELECT id,metadata_json FROM public.billing_events WHERE invoice_id=%s
            AND event_category='payment_attempt' AND payment_status IN ('created','pending_provider_ack','authorized')
            ORDER BY id FOR UPDATE''', (invoice['id'],))
        rows = cursor.fetchall()
    else:
        rows = [attempt]
    for row in rows:
        metadata = ManualBillingRepository._json(row.get('metadata_json'))
        metadata.setdefault('manualBilling', {}).update(closure)
        # Not reconciled: late provider money on this Order stays in the recovery scan.
        metadata['manualBilling'].pop('closureReconciledAt', None)
        cursor.execute('''UPDATE public.billing_events SET payment_status='cancelled',event_status='cancelled',
            metadata_json=%s WHERE id=%s''', (Json(metadata), row['id']))


def supersedeAttemptLocked(cursor, attemptId: str, supersededBy: str, now) -> None:
    """Close one attempt in favour of a newer open attempt in the same slot. Runs under the owner lock."""
    from api.services.billing.paymentPolicy import captureWindowMinutes
    from api.services.billing.paymentSlots import checkoutTtlSeconds, slotKind, supersededRecoverUntil
    if str(attemptId) == str(supersededBy):
        raise ValueError('PAYMENT_SUPERSESSION_INVALID')
    cursor.execute('''SELECT * FROM public.billing_events WHERE id IN (%s,%s)
        AND event_category='payment_attempt' FOR UPDATE''', (attemptId, supersededBy))
    rows = {str(row['id']): row for row in cursor.fetchall()}
    source, target = rows.get(str(attemptId)), rows.get(str(supersededBy))
    if not source or not target:
        raise ValueError('PAYMENT_SUPERSESSION_INVALID')
    sourceFrozen = ManualBillingRepository._json(source.get('metadata_json')).get('manualBilling', {})
    targetFrozen = ManualBillingRepository._json(target.get('metadata_json')).get('manualBilling', {})
    sourceAt, targetAt = _utc(source.get('attempted_at')), _utc(target.get('attempted_at'))
    if (source['user_id'] is None or source['user_id'] != target['user_id']
            or slotKind(sourceFrozen.get('purpose')) != slotKind(targetFrozen.get('purpose'))
            or source.get('payment_status') == 'captured'
            or target.get('payment_status') not in _LIVE or targetFrozen.get('closedAt')
            or not sourceAt or not targetAt or targetAt < sourceAt):
        raise ValueError('PAYMENT_SUPERSESSION_INVALID')
    storage = _storagePresent(cursor)
    sourceControl = targetControl = None
    if storage:
        cursor.execute('SELECT * FROM public.payment_slot_controls WHERE attempt_id IN (%s,%s) FOR UPDATE',
                       (attemptId, supersededBy))
        controls = {str(row['attempt_id']): row for row in cursor.fetchall()}
        targetControl, sourceControl = controls.get(str(supersededBy)), controls.get(str(attemptId))
        if not targetControl or targetControl['closed_at'] is not None or (sourceControl and (
                sourceControl['closed_at'] is not None or sourceControl['slot_id'] != targetControl['slot_id'])):
            raise ValueError('PAYMENT_SUPERSESSION_INVALID')
    retireSupersededAttemptLocked(cursor, source, now, target.get('invoice_id'))
    if not storage:
        return
    if not sourceControl:
        # A pre-rollout Order joins the replacement's slot when it is superseded.
        policy = 'automatic' if sourceFrozen.get('capturePolicy') == 'automatic' else 'account_default'
        cursor.execute('''INSERT INTO public.payment_slot_controls
            (attempt_id,slot_id,invoice_id,user_id,purpose,draft_revision,provider_order_id,capture_policy)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (attemptId, targetControl['slot_id'], source['invoice_id'],
            source['user_id'], sourceFrozen.get('purpose'),
            sourceFrozen.get('purchaseDraftRevision') if sourceFrozen.get('purpose') == 'initial_purchase' else None,
            source.get('provider_order_id'), policy))
    recoverUntil = supersededRecoverUntil(now, _utc(sourceFrozen.get('expiresAt')),
        _utc(sourceFrozen.get('providerObservation', {}).get('latestPaymentCreatedAt')),
        int(sourceFrozen.get('captureWindowMinutes') or captureWindowMinutes()), checkoutTtlSeconds())
    cursor.execute('''UPDATE public.payment_slot_controls SET operation_revision=operation_revision+1,
        closed_at=%s,closed_reason='SUPERSEDED',superseded_by=%s,recover_until=%s,updated_at=clock_timestamp()
        WHERE attempt_id=%s''', (now, supersededBy, recoverUntil, attemptId))


def bindControlledOrderLocked(cursor, attemptId, orderId):
    cursor.execute("SELECT to_regclass('public.payment_slot_controls') AS relation")
    if not cursor.fetchone()['relation']:
        return
    cursor.execute('''UPDATE public.payment_slot_controls SET provider_order_id=%s,
        operation_revision=operation_revision+1,updated_at=clock_timestamp()
        WHERE attempt_id=%s AND provider_order_id IS NULL''', (orderId, attemptId))


def getPurchaseCheckoutRepository():
    return PurchaseCheckoutRepository()
