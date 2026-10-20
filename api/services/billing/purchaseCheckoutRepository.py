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
            from api.services.billing.purchaseCheckoutStatusService import reducePurchaseCheckoutStatus, applyPurchaseControl
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
                    checkout = applyPurchaseControl(checkout, control)
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

    def reserveDraftCheckout(self, userId, draftRevision, pricing, requestKey):
        from api.services.billing.purchaseCheckoutService import requirePurchaseActions, validatePurchaseKey
        from api.services.billing.manualBillingContracts import CheckoutRequest
        requirePurchaseActions()
        validatePurchaseKey(requestKey)
        if pricing.get('userId') != userId or set(pricing) != {'userId', 'currency', 'catalogRevision', 'domains', 'billingMode'}:
            raise ValueError('PURCHASE_PRICING_INVALID')
        return self.reserveCheckout(CheckoutRequest(userId, 'initial_purchase', pricing['billingMode'],
            {'domains': pricing['domains']}, requestKey, currency=pricing['currency'],
            expectedCatalogRevision=pricing['catalogRevision'], purchaseDraftRevision=draftRevision))

    def _ownedPurchaseLocked(self, cursor, userId, attemptId):
        self._requireStorage(cursor)
        self._lockUser(cursor, userId)
        subscription = self._canonical(cursor, userId)
        cursor.execute('SELECT invoice_id FROM public.billing_events WHERE id=%s AND user_id=%s', (attemptId, userId))
        row = cursor.fetchone()
        if not row:
            raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
        cursor.execute('SELECT * FROM public."Invoices" WHERE id=%s AND "userId"=%s FOR UPDATE', (row['invoice_id'], userId))
        invoice = cursor.fetchone()
        cursor.execute('SELECT * FROM public.billing_events WHERE id=%s AND user_id=%s FOR UPDATE', (attemptId, userId))
        attempt = cursor.fetchone()
        frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
        if not invoice or str(attempt['subscription_id']) != str(subscription['id']) or frozen.get('purpose') != 'initial_purchase':
            raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
        cursor.execute('SELECT * FROM public.subscription_purchase_checkout_controls WHERE attempt_id=%s AND user_id=%s FOR UPDATE', (attemptId, userId))
        return subscription, invoice, attempt, cursor.fetchone()

    def _cancelReplayLocked(self, cursor, userId, attemptId, revision, key):
        cursor.execute('SELECT metadata_json FROM public.billing_events WHERE idempotency_key=%s AND user_id=%s',
                       ('purchase-cancel:' + userId + ':' + key, userId))
        existing = cursor.fetchone()
        if not existing:
            return False
        if self._json(existing['metadata_json']) != {'attemptId': attemptId, 'expectedOperationRevision': revision}:
            raise ValueError('IDEMPOTENCY_CONFLICT')
        return True

    def cancellationReplay(self, userId, attemptId, revision, key):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                return self._cancelReplayLocked(cursor, userId, attemptId, revision, key)
        return self._run(operation)

    def acceptCancellation(self, userId, attemptId, expectedOperationRevision, requestKey, observation):
        from api.services.billing.purchaseCheckoutService import requirePurchaseActions, validatePurchaseKey
        requirePurchaseActions()
        validatePurchaseKey(requestKey)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                subscription, invoice, attempt, control = self._ownedPurchaseLocked(cursor, userId, attemptId)
                if self._cancelReplayLocked(cursor, userId, attemptId, expectedOperationRevision, requestKey):
                    return
                if invoice['status'] == 'PAID' or attempt['payment_status'] == 'captured':
                    raise ValueError('PURCHASE_ALREADY_COMPLETED')
                if not control:
                    if expectedOperationRevision != 0:
                        raise ValueError('PURCHASE_CHECKOUT_STALE')
                    cursor.execute('''INSERT INTO public.subscription_purchase_checkout_controls
                        (attempt_id,invoice_id,user_id,draft_revision,provider_order_id)
                        VALUES(%s,%s,%s,1,%s) RETURNING *''', (attemptId, invoice['id'], userId, attempt['provider_order_id']))
                    control = cursor.fetchone()
                if int(control['operation_revision']) != expectedOperationRevision:
                    raise ValueError('PURCHASE_CHECKOUT_STALE')
                coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                if coverage.currentPeriod or coverage.denialReason not in (None, 'no_paid_coverage', 'no_subscription', 'expired'):
                    raise ValueError('PURCHASE_DRAFT_NOT_ALLOWED')
                cursor.execute('''SELECT * FROM public.subscription_purchase_operations WHERE attempt_id=%s
                    AND kind='capture' ORDER BY id FOR UPDATE''', (attemptId,))
                captures = list(cursor.fetchall())
                inFlight = any(row['state'] in ('submission_started', 'outcome_unknown', 'submitted')
                    or row['state'] in ('needs_attention', 'failed') and self._json(row['submission_evidence']).get('startedAt') for row in captures)
                for row in captures:
                    if row['state'] == 'queued' or row['state'] in ('needs_attention','failed') and not self._json(row['submission_evidence']).get('startedAt'):
                        cursor.execute("UPDATE public.subscription_purchase_operations SET state='cancelled',updated_at=clock_timestamp() WHERE id=%s", (row['id'],))
                now = _now()
                accepted = control['cancellation_accepted_at'] or (None if inFlight else now)
                cursor.execute('''UPDATE public.subscription_purchase_checkout_controls SET
                    operation_revision=operation_revision+1,cancellation_requested_at=coalesce(cancellation_requested_at,%s),
                    cancellation_accepted_at=%s,updated_at=%s WHERE attempt_id=%s''', (now, accepted, now, attemptId))
                if accepted:
                    self._closeInvoice(cursor, invoice, 'PURCHASE_CANCELLED', now=accepted, preserveAuthorization=True)
                    for table, identity in (('billing_events', 'id'), ('Invoices', 'id')):
                        rowId = attemptId if table == 'billing_events' else invoice['id']
                        cursor.execute(f'SELECT metadata_json FROM public."{table}" WHERE {identity}=%s', (rowId,))
                        metadata = self._json(cursor.fetchone()['metadata_json'])
                        frozen = metadata.setdefault('manualBilling', {})
                        frozen['purchaseCancellationAcceptedAt'] = accepted.isoformat()
                        stored = self._json(attempt.get('metadata_json')).get('manualBilling', {}).get('providerObservation', {})
                        if (observation and observation.complete and observation.outcome == 'unpaid' and not observation.payments
                                and attempt['payment_status'] != 'authorized'
                                and stored.get('complete') and stored.get('outcome') == 'unpaid' and not stored.get('hasPayments')
                                and _utc(stored.get('checkedAt')) == observation.observedAt
                                and observation.attemptId == attemptId and observation.providerOrderId == attempt['provider_order_id']
                                and observation.amount == int(attempt['amount']) and observation.currency == attempt['currency']
                                and 0 <= (now-observation.observedAt).total_seconds() <= 60):
                            frozen['closureReconciledAt'] = now.isoformat()
                        cursor.execute(f'UPDATE public."{table}" SET metadata_json=%s WHERE {identity}=%s', (Json(metadata), rowId))
                cursor.execute('''INSERT INTO public.billing_events(user_id,subscription_id,invoice_id,event_category,
                    event_type,event_status,idempotency_key,metadata_json) VALUES(%s,%s,%s,'system',
                    'purchase.cancellation.requested','COMMITTED',%s,%s)''',
                    (userId, subscription['id'], invoice['id'], 'purchase-cancel:' + userId + ':' + requestKey,
                     Json({'attemptId': attemptId, 'expectedOperationRevision': expectedOperationRevision})))
        self._run(operation)
        return self.readEditor(userId)

    def reserveCapture(self, userId, attemptId, paymentId):
        from api.services.billing.purchaseCheckoutService import requirePurchaseActions
        requirePurchaseActions()
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                subscription, invoice, attempt, control = self._ownedPurchaseLocked(cursor, userId, attemptId)
                if not control or control['capture_policy'] != 'manual':
                    return None
                coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                frozen = self._json(attempt['metadata_json'])['manualBilling']
                if (control['cancellation_requested_at'] or frozen.get('closedAt') or invoice['status'] != 'PAYMENT_PENDING'
                        or frozen.get('providerObservation', {}).get('outcome') == 'captured'
                        or attempt['payment_status'] != 'authorized' or not _utc(frozen.get('expiresAt'))
                        or _utc(frozen['expiresAt']) <= _now() or coverage.currentPeriod
                        or coverage.denialReason not in (None, 'no_paid_coverage', 'no_subscription', 'expired')):
                    return None
                if frozen.get('authorizedPaymentId') != paymentId:
                    return None
                cursor.execute("SELECT * FROM public.subscription_purchase_operations WHERE attempt_id=%s AND kind='capture' ORDER BY id FOR UPDATE", (attemptId,))
                existing = list(cursor.fetchall())
                if existing:
                    if any(row['provider_payment_id'] != paymentId for row in existing):
                        raise ValueError('CAPTURE_PAYMENT_IDENTITY_MISMATCH')
                    return dict(existing[0])
                cursor.execute('''INSERT INTO public.subscription_purchase_operations
                    (user_id,attempt_id,invoice_id,provider_payment_id,kind,amount,currency,dedupe_key)
                    VALUES(%s,%s,%s,%s,'capture',%s,%s,%s) RETURNING *''',
                    (userId, attemptId, invoice['id'], paymentId, attempt['amount'], attempt['currency'], 'purchase:capture:' + paymentId))
                return dict(cursor.fetchone())
        return self._run(operation)

    def captureOperation(self, operationId):
        UUID(str(operationId))
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''SELECT o.*,c.operation_revision,c.capture_policy,c.cancellation_requested_at,
                    c.cancellation_accepted_at,c.provider_order_id FROM public.subscription_purchase_operations o
                    JOIN public.subscription_purchase_checkout_controls c ON c.attempt_id=o.attempt_id
                    WHERE o.id=%s AND o.kind='capture' ''', (operationId,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('PURCHASE_OPERATION_MISSING')
                return dict(row)
        return self._run(operation)

    def beginCaptureSubmission(self, operationId, expectedRevision):
        from api.services.billing.purchaseCheckoutService import requirePurchaseActions
        requirePurchaseActions()
        observed = self.captureOperation(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                subscription, invoice, attempt, control = self._ownedPurchaseLocked(cursor, observed['user_id'], str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.subscription_purchase_operations WHERE id=%s FOR UPDATE', (operationId,))
                op = cursor.fetchone()
                frozen = self._json(attempt['metadata_json'])['manualBilling']
                observation = frozen.get('providerObservation', {})
                checkedAt = _utc(observation.get('checkedAt'))
                if (op['state'] != 'queued' or control['operation_revision'] != expectedRevision or control['capture_policy'] != 'manual'
                        or control['cancellation_requested_at'] or frozen.get('closedAt') or invoice['status'] != 'PAYMENT_PENDING'
                        or attempt['payment_status'] != 'authorized' or _utc(frozen.get('expiresAt')) is None
                        or _utc(frozen['expiresAt']) <= _now() or observation.get('outcome') != 'authorized'
                        or checkedAt is None or not 0 <= (_now()-checkedAt).total_seconds() <= 60):
                    return None
                coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                if coverage.currentPeriod or coverage.denialReason not in (None, 'no_paid_coverage', 'no_subscription', 'expired'):
                    return None
                cursor.execute('''UPDATE public.subscription_purchase_checkout_controls SET operation_revision=operation_revision+1,
                    updated_at=clock_timestamp() WHERE attempt_id=%s''', (attempt['id'],))
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state='submission_started',
                    submission_evidence=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *''',
                    (Json({'controlRevision': expectedRevision+1, 'startedAt': _now().isoformat()}), operationId))
                return dict(cursor.fetchone())
        return self._run(operation)

    def markCaptureUnknown(self, operationId):
        observed = self.captureOperation(operationId)
        def operation(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor, observed['user_id'])
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state='outcome_unknown',
                    error_code='CAPTURE_OUTCOME_UNKNOWN',updated_at=clock_timestamp()
                    WHERE id=%s AND state IN ('submission_started','submitted','outcome_unknown')''', (operationId,))
        self._run(operation)
        return self.captureOperation(operationId)

    def recordReleasedCapture(self, operationId):
        observed = self.captureOperation(operationId)
        def operation(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor, observed['user_id'])
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state='failed',
                    error_code='PAYMENT_RELEASED',updated_at=clock_timestamp() WHERE id=%s
                    AND state IN ('submission_started','submitted','outcome_unknown','needs_attention')''', (operationId,))
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state='cancelled',
                    error_code='PAYMENT_RELEASED',updated_at=clock_timestamp() WHERE id=%s AND state IN ('queued','failed')''', (operationId,))
        self._run(operation)
        return self.captureOperation(operationId)

    def captureNeedsAttention(self, operationId):
        observed = self.captureOperation(operationId)
        def operation(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor, observed['user_id'])
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state='needs_attention',
                    error_code='CONFLICTING_PROVIDER_PAYMENTS',updated_at=clock_timestamp() WHERE id=%s AND state='queued' ''', (operationId,))
        self._run(operation)
        return self.captureOperation(operationId)

    def stopCaptureForSettledOrder(self, operationId):
        observed = self.captureOperation(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                _, invoice, _, _ = self._ownedPurchaseLocked(cursor, observed['user_id'], str(observed['attempt_id']))
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state=%s,
                    error_code='OTHER_PROVIDER_CAPTURE',updated_at=clock_timestamp() WHERE id=%s AND state='queued' ''',
                    ('cancelled' if invoice['status'] == 'PAID' else 'needs_attention', operationId))
        self._run(operation)
        return self.captureOperation(operationId)

    def recordCaptureOutcome(self, operationId, evidence):
        observed = self.captureOperation(operationId)
        if (str(observed['attempt_id']) != evidence.attemptId or str(observed['invoice_id']) != evidence.invoiceId
                or observed['user_id'] != evidence.userId or observed['provider_order_id'] != evidence.providerOrderId
                or observed['provider_payment_id'] != evidence.providerPaymentId or observed['amount'] != evidence.amount
                or observed['currency'] != evidence.currency or evidence.financialStatus != 'captured'):
            raise ValueError('CAPTURE_EVIDENCE_MISMATCH')
        def startObservation(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor, observed['user_id'])
                cursor.execute("UPDATE public.subscription_purchase_operations SET state='submission_started' WHERE id=%s AND state='queued'", (operationId,))
        self._run(startObservation)
        # The durable in-flight fence survives a failure before or after the
        # existing idempotent financial finalizer commits. Never post again.
        result = self.finalizeCapturedPayment(evidence)
        def complete(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor, observed['user_id'])
                cursor.execute('''UPDATE public.subscription_purchase_operations SET state=%s,result=%s,
                    updated_at=clock_timestamp() WHERE id=%s AND state IN
                    ('submission_started','outcome_unknown','submitted','needs_attention')''',
                    ('succeeded' if result.finalized or result.state == 'already_finalized' else 'needs_attention',
                     Json({'finalizationState': result.state}), operationId))
        self._run(complete)
        return {**self.captureOperation(operationId), 'finalizationResult': result}


def storedPurchaseControl(repository, userId, attemptId):
    def operation(connection):
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            repository._lockUser(cursor, userId)
            cursor.execute("SELECT to_regclass('public.subscription_purchase_checkout_controls') AS relation")
            if not cursor.fetchone()['relation']:
                return None
            cursor.execute('''SELECT c.* FROM public.subscription_purchase_checkout_controls c
                JOIN public.billing_events b ON b.id=c.attempt_id AND b.user_id=c.user_id
                WHERE c.user_id=%s AND c.attempt_id=%s''', (userId, attemptId))
            return cursor.fetchone()
    return repository._run(operation)


def guardInitialPurchaseLocked(billing, cursor, request, subscription, now):
    """Runs inside the original invoice reservation transaction, including old clients."""
    from api.services.billing.purchaseCheckoutService import purchaseActionsReady, requirePurchaseActions, requireManualCaptureCurrency
    cursor.execute("SELECT to_regclass('public.subscription_purchase_checkout_controls') AS relation")
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
    cursor.execute('''SELECT 1 FROM public.subscription_purchase_operations WHERE user_id=%s
        AND state NOT IN ('succeeded','cancelled') LIMIT 1''', (request.userId,))
    if cursor.fetchone():
        raise ValueError('PURCHASE_FUNDS_PENDING')
    cursor.execute('''SELECT b.*,c.cancellation_accepted_at FROM public.subscription_purchase_checkout_controls c
        JOIN public.billing_events b ON b.id=c.attempt_id WHERE c.user_id=%s ORDER BY b.id''', (request.userId,))
    for row in cursor.fetchall():
        frozen = billing._json(row['metadata_json']).get('manualBilling', {})
        exact = row.get('idempotency_key') == f'initial_purchase:{request.userId}:{request.requestKey}'
        if row.get('payment_status') == 'captured':
            continue
        if row['cancellation_accepted_at']:
            if not frozen.get('closureReconciledAt'):
                raise ValueError('PURCHASE_FUNDS_PENDING')
        elif not exact:
            raise ValueError('PURCHASE_REPLACEMENT_CONFIRMATION_REQUIRED')
    if request.purchaseDraftRevision is None:
        if purchaseActionsReady():
            raise ValueError('PURCHASE_DRAFT_REQUIRED')
        return
    requirePurchaseActions()
    requireManualCaptureCurrency(request.currency)
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
    # Uncontrolled original Orders also require explicit cancellation. Do not
    # let the legacy implicit-replacement policy bypass the editor confirmation.
    cursor.execute('''SELECT * FROM public.billing_events WHERE user_id=%s AND event_category='payment_attempt'
        AND metadata_json->'manualBilling'->>'purpose'='initial_purchase' ORDER BY id''', (request.userId,))
    for row in cursor.fetchall():
        frozen = billing._json(row['metadata_json'])['manualBilling']
        exact = row.get('idempotency_key') == f'initial_purchase:{request.userId}:{request.requestKey}'
        if not exact and row['payment_status'] != 'captured' and not frozen.get('closedAt'):
            raise ValueError('PURCHASE_REPLACEMENT_CONFIRMATION_REQUIRED')
        if not exact and frozen.get('closedAt') and not frozen.get('closureReconciledAt') and row['payment_status'] != 'captured':
            raise ValueError('PURCHASE_FUNDS_PENDING')


def bindPurchaseControlLocked(cursor, request, intent):
    cursor.execute('''SELECT c.attempt_id FROM public.subscription_purchase_checkout_controls c
        JOIN public.billing_events b ON b.id=c.attempt_id WHERE c.user_id=%s AND c.attempt_id<>%s
        AND c.cancellation_accepted_at IS NOT NULL ORDER BY c.created_at DESC,c.attempt_id DESC LIMIT 1''',
        (request.userId, intent.attemptId))
    predecessor = cursor.fetchone()
    cursor.execute('''INSERT INTO public.subscription_purchase_checkout_controls
        (attempt_id,invoice_id,user_id,draft_revision,capture_policy,predecessor_attempt_id)
        VALUES(%s,%s,%s,%s,'manual',%s) ON CONFLICT(attempt_id) DO NOTHING''',
        (intent.attemptId, intent.invoiceId, request.userId, request.purchaseDraftRevision,
         predecessor['attempt_id'] if predecessor else None))


def bindControlledOrderLocked(cursor, attemptId, orderId):
    cursor.execute("SELECT to_regclass('public.subscription_purchase_checkout_controls') AS relation")
    if not cursor.fetchone()['relation']:
        return
    cursor.execute('''UPDATE public.subscription_purchase_checkout_controls SET provider_order_id=%s,
        operation_revision=operation_revision+1,updated_at=clock_timestamp()
        WHERE attempt_id=%s AND provider_order_id IS NULL''', (orderId, attemptId))


def getPurchaseCheckoutRepository():
    return PurchaseCheckoutRepository()
