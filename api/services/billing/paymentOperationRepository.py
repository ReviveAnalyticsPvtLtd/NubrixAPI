"""Owner-locked persistence for full refunds of captured payments that were never granted, any purpose,
and the refund worker's durable scans. No provider I/O ever happens here, so no lock is held across it."""
from datetime import timedelta
from dataclasses import replace
import os
from uuid import UUID
from psycopg2.extras import Json, RealDictCursor
from api.services.billing.captureRules import REFUND_REASONS
from api.services.billing.manualBillingRepository import _commitNotificationIntentLocked, _now, _utc
from utils.logger import logger


def _refundableCaptureLocked(cursor, operation):
    """The operation's payment was recorded and never granted: only such money is ever returned."""
    cursor.execute('''SELECT 1 FROM public.billing_events WHERE provider_payment_id=%s AND event_type='payment.capture'
        AND event_status='REFUND_RESERVED' AND invoice_id=%s AND currency=%s AND amount>=%s''',
        (operation['provider_payment_id'], operation['invoice_id'], operation['currency'], operation['amount']))
    return cursor.fetchone() is not None


REFUND_NOTIFICATIONS = {'started': 'payment_refund_started', 'completed': 'payment_refund_completed'}


def _recordRefundNotificationLocked(cursor, operationId, milestone):
    """Commit one customer email intent for a refund milestone (spec 4.7) in the caller's transaction.

    The durable notification bridge delivers it. Its identity is the operation and milestone, so each
    email is committed exactly once. Content is what the customer needs (amount, currency, reason, start
    date), never an invoice, attempt, Order, payment or operation identity. A detached (erased) owner is
    never emailed; an owner without a canonical subscription cannot be addressed, so the skipped email
    is logged for repair and the refund itself goes ahead.
    """
    cursor.execute('''SELECT o.*,c.purpose FROM public.payment_operations o
        JOIN public.payment_slot_controls c ON c.attempt_id=o.attempt_id WHERE o.id=%s''', (operationId,))
    operation = cursor.fetchone()
    if not operation or not operation['user_id']:
        return
    cursor.execute('SELECT id FROM public.subscriptions WHERE user_id=%s AND is_canonical', (operation['user_id'],))
    subscription = cursor.fetchone()
    if not subscription:
        logger.bind(operationId=str(operation['id']), milestone=milestone).error(
            'Refund email skipped: owner has no canonical subscription')
        return
    startedAt = operation['created_at'].isoformat()
    metadata = {'amount': operation['amount'], 'currency': operation['currency'], 'reason': operation['reason'],
                'purpose': operation['purpose'], 'startedAt': startedAt}
    if milestone == 'completed':
        metadata['completedAt'] = _now().isoformat()
    dedupeKey = f"payment_refund:{operation['id']}:{milestone}"
    intent = {'userId': operation['user_id'], 'subscriptionId': str(subscription['id']),
              'notificationType': REFUND_NOTIFICATIONS[milestone], 'dedupeKey': dedupeKey,
              'periodEnd': startedAt, 'metadata': metadata}
    _commitNotificationIntentLocked(cursor, intent)


def reserveRefund(cursor, *, attemptId: str, paymentId: str, amount: int, currency: str, reason: str) -> str:
    """Reserve the one full refund of a captured payment that was never granted (spec 4.5).

    Runs in the finalization transaction under the owner lock, after the capture is recorded as
    REFUND_RESERVED. Idempotent per provider payment ('payment:refund:<paymentId>'); the refund
    worker executes it later, never under this lock. A new reservation commits the customer's
    'payment_refund_started' email intent in the same transaction.
    """
    if (reason not in REFUND_REASONS or not isinstance(paymentId, str) or not paymentId
            or type(amount) is not int or amount <= 0 or currency not in ('INR', 'USD')):
        raise ValueError('PAYMENT_REFUND_INVALID')
    cursor.execute("SELECT * FROM public.billing_events WHERE id=%s AND event_category='payment_attempt' FOR UPDATE",
                   (attemptId,))
    attempt = cursor.fetchone()
    cursor.execute('SELECT * FROM public.payment_slot_controls WHERE attempt_id=%s FOR UPDATE', (attemptId,))
    control = cursor.fetchone()
    if (not attempt or not control or not attempt.get('provider_order_id')
            or control['provider_order_id'] != attempt['provider_order_id']
            or str(control['invoice_id']) != str(attempt['invoice_id'])
            or control['user_id'] != attempt['user_id']):
        raise ValueError('PAYMENT_REFUND_BINDING_INVALID')
    if amount > int(attempt['amount'] or 0) or currency != attempt['currency']:
        raise ValueError('PAYMENT_REFUND_AMOUNT_INVALID')
    cursor.execute('''SELECT * FROM public.billing_events WHERE provider_payment_id=%s
        AND event_type='payment.capture' FOR UPDATE''', (paymentId,))
    capture = cursor.fetchone()
    if (not capture or capture['event_status'] != 'REFUND_RESERVED'
            or capture['provider_order_id'] != attempt['provider_order_id']
            or str(capture['invoice_id']) != str(attempt['invoice_id'])
            or capture['amount'] != amount or capture['currency'] != currency):
        raise ValueError('PAYMENT_REFUND_CAPTURE_NOT_VERIFIED')
    cursor.execute("SELECT * FROM public.payment_operations WHERE dedupe_key=%s", ('payment:refund:' + paymentId,))
    prior = cursor.fetchone()
    if prior:
        if str(prior['attempt_id']) != str(attemptId) or prior['amount'] != amount or prior['currency'] != currency:
            raise ValueError('PAYMENT_REFUND_BINDING_INVALID')
        return str(prior['id'])
    cursor.execute('''INSERT INTO public.payment_operations
        (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key,erasure_request_id)
        VALUES(%s,%s,%s,%s,'refund',%s,%s,%s,%s,%s) RETURNING id''',
        (attempt['user_id'], attemptId, str(attempt['invoice_id']), paymentId, reason, amount, currency,
         'payment:refund:' + paymentId, control['erasure_request_id']))
    operationId = str(cursor.fetchone()['id'])
    _recordRefundNotificationLocked(cursor, operationId, 'started')
    metadata = attempt['metadata_json'] if isinstance(attempt['metadata_json'], dict) else {}
    if metadata.get('manualBilling', {}).pop('closureReconciledAt', None) is not None:
        cursor.execute('UPDATE public.billing_events SET metadata_json=%s WHERE id=%s', (Json(metadata), attemptId))
    return operationId


class PaymentOperationRepositoryMixin:
    @staticmethod
    def _purchaseSchemaReady(cursor):
        cursor.execute("SELECT to_regclass('private.payment_recovery_runtime') AS relation")
        if not cursor.fetchone()['relation']:
            return False
        cursor.execute('''SELECT count(*) AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname IN
              ('subscription_purchase_drafts','payment_slots','payment_slot_controls','payment_operations')
            AND c.relrowsecurity AND NOT has_table_privilege('anon',c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            AND NOT has_table_privilege('authenticated',c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')''')
        if cursor.fetchone()['count']!=4:
            return False
        cursor.execute('''SELECT count(*) AS count FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
            AND c.relname IN ('payment_slots','payment_slot_controls','payment_operations')
            AND t.tgname IN ('payment_storage_guard','payment_no_truncate') AND NOT t.tgisinternal
            AND t.tgenabled IN ('O','A')''')
        return cursor.fetchone()['count']==6

    @staticmethod
    def _paymentWorkerFresh(cursor):
        """Guarded storage plus a recent recovery/refund worker heartbeat."""
        if not PaymentOperationRepositoryMixin._purchaseSchemaReady(cursor):
            return False
        cursor.execute('''SELECT 1 FROM private.payment_recovery_runtime
            WHERE worker_seen_at BETWEEN clock_timestamp()-interval '5 minutes' AND clock_timestamp()''')
        return cursor.fetchone() is not None

    @staticmethod
    def _payReady(cursor):
        """Pay needs the editable checkout and a refund worker that is alive (spec 6)."""
        from api.services.billing.purchaseCheckoutService import purchaseActionsReady
        return purchaseActionsReady() and PaymentOperationRepositoryMixin._paymentWorkerFresh(cursor)

    def _purchasePresentation(self,cursor,checkout,draft,allowed):
        from api.services.billing.purchaseCheckoutContracts import PurchaseCheckoutCapabilities
        from api.services.billing.purchaseDraftService import editableCheckoutEnabled
        ready=self._payReady(cursor)
        # Owed refunds and other money never block Pay; the server decides open, reuse or replace.
        capabilities=PurchaseCheckoutCapabilities(
            mayEditDraft=bool(allowed and editableCheckoutEnabled()),
            maySubmitLatestDraft=bool(ready and allowed and draft.revision>0))
        return replace(checkout,capabilities=capabilities) if checkout else None,capabilities

    def _operationContext(self,cursor,attemptId):
        cursor.execute('SELECT user_id FROM public.billing_events WHERE id=%s',(attemptId,))
        owner = cursor.fetchone()
        if not owner:
            raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
        self._lockUser(cursor,owner['user_id'])
        self._refreshFinancialOwnerLocked(cursor,attemptId,owner)
        cursor.execute('SELECT * FROM public.billing_events WHERE id=%s FOR UPDATE',(attemptId,))
        attempt = cursor.fetchone()
        cursor.execute('SELECT * FROM public."Invoices" WHERE id=%s FOR UPDATE',(attempt['invoice_id'],))
        invoice = cursor.fetchone()
        cursor.execute('SELECT * FROM public.payment_slot_controls WHERE attempt_id=%s FOR UPDATE',(attemptId,))
        return attempt,invoice,cursor.fetchone()

    reserveRefund = staticmethod(reserveRefund)

    def operationById(self,operationId):
        UUID(str(operationId))
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''SELECT o.*,c.operation_revision,c.provider_order_id FROM public.payment_operations o
                    JOIN public.payment_slot_controls c ON c.attempt_id=o.attempt_id WHERE o.id=%s''',(operationId,))
                row=cursor.fetchone()
                if not row:
                    raise ValueError('PAYMENT_OPERATION_MISSING')
                return dict(row)
        return self._run(operation)

    def beginRefundSubmission(self,operationId,expectedRevision):
        observed=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                attempt,invoice,control=self._operationContext(cursor,str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                op=cursor.fetchone()
                facts=self._json(op['result'])
                if (op['kind']!='refund' or op['state']!='queued' or self._json(op['submission_evidence']).get('startedAt')
                        or control['operation_revision']!=expectedRevision or not _refundableCaptureLocked(cursor,op)
                        or facts.get('pending')!=0 or not facts.get('checkedAt')
                        or not 0<=(_now()-_utc(facts['checkedAt'])).total_seconds()<=60):
                    return None
                amount=op['amount']-facts['processed']
                if amount<=0:
                    return None
                cursor.execute('''UPDATE public.payment_slot_controls SET operation_revision=operation_revision+1,
                    updated_at=clock_timestamp() WHERE attempt_id=%s''',(op['attempt_id'],))
                previous=self._json(op['submission_evidence']).get('priorSubmissions',[])
                submissionKey=operationId+('-'+str(len(previous)) if previous else '')
                cursor.execute('''UPDATE public.payment_operations SET state='submission_started',submission_evidence=%s,
                    updated_at=clock_timestamp() WHERE id=%s RETURNING *''',
                    (Json({'startedAt':_now().isoformat(),'amount':amount,'controlRevision':expectedRevision+1,
                           'key':submissionKey,'priorSubmissions':previous}),operationId))
                return dict(cursor.fetchone())
        return self._run(operation)

    def recordRefundEvidence(self,operationId,facts):
        observed=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._operationContext(cursor,str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                op=cursor.fetchone()
                if op['kind']!='refund':
                    raise ValueError('PAYMENT_OPERATION_KIND_INVALID')
                if op['state'] in ('succeeded','cancelled'):
                    return
                submitted=bool(self._json(op['submission_evidence']).get('startedAt'))
                submission=self._json(op['submission_evidence'])
                own=[r for r in facts['refunds'] if r['ours'] and r.get('submissionKey')==submission.get('key')]
                previousFacts=self._json(op['result'])
                externalOnly=bool(previousFacts.get('externalOnly') or (
                    op['state']=='queued' and not submitted and facts['pending'] and not any(r['ours'] for r in facts['refunds'])))
                state=op['state']
                if facts['processed']==op['amount']:
                    state='succeeded'
                elif facts['pending']:
                    state='submitted'
                elif submitted:
                    state='needs_attention' if own and all(r['status']=='failed' for r in own) else 'outcome_unknown'
                elif state=='needs_attention' and op['error_code'] in ('PAYMENT_PROVIDER_RETRIES_ENABLED','PAYMENT_REFUND_HISTORY_INVALID'):
                    state='queued'  # Fresh valid history; this operation never submitted.
                elif state=='submitted' and externalOnly:
                    # Only explicit external-only history proves no local send.
                    # An older submitted row without that proof stays unresolved.
                    cursor.execute("UPDATE public.payment_operations SET state='failed' WHERE id=%s",(operationId,))
                    state='queued'
                if state in ('succeeded','submitted') and op['state']=='queued':
                    cursor.execute("UPDATE public.payment_operations SET state='submission_started' WHERE id=%s",(operationId,))
                cursor.execute('''UPDATE public.payment_operations SET state=%s,result=%s,error_code=%s,updated_at=clock_timestamp()
                    WHERE id=%s''',(state,Json({**facts,'checkedAt':_now().isoformat(),'externalOnly':externalOnly}),
                    'PAYMENT_REFUND_FAILED' if state=='needs_attention' and own else None,operationId))
                if state=='succeeded':
                    # The provider reports the full amount processed: tell the customer once.
                    _recordRefundNotificationLocked(cursor,operationId,'completed')
        self._run(operation)
        return self.operationById(operationId)

    def prepareFailedRefundRetry(self,operationId):
        observed=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._operationContext(cursor,str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                op=cursor.fetchone()
                evidence=self._json(op['submission_evidence'])
                facts=self._json(op['result'])
                own=[r for r in facts.get('refunds',[]) if r['ours'] and r.get('submissionKey')==evidence.get('key')]
                if (op['kind']!='refund' or op['state']!='needs_attention' or op['error_code']!='PAYMENT_REFUND_FAILED'
                        or not evidence.get('startedAt') or facts.get('pending')!=0 or len(own)!=1
                        or own[0]['status']!='failed' or own[0]['amount']!=evidence['amount']
                        or not facts.get('checkedAt') or not 0<=(_now()-_utc(facts['checkedAt'])).total_seconds()<=60):
                    return False
                previous=evidence.pop('priorSubmissions',[])
                previous.append({**evidence,'failedRefundId':own[0]['id']})
                cursor.execute('''UPDATE public.payment_operations SET state='queued',error_code=NULL,
                    submission_evidence=%s,updated_at=clock_timestamp() WHERE id=%s''',
                    (Json({'priorSubmissions':previous}),operationId))
                return True
        return self._run(operation)

    def markRefundOutcomeUnknown(self,operationId):
        self._setPaymentOperationState(operationId,'outcome_unknown','PAYMENT_REFUND_OUTCOME_UNKNOWN',
            ('submission_started','submitted','outcome_unknown'))

    def failPaymentOperation(self,operationId,reason):
        self._setPaymentOperationState(operationId,'needs_attention',reason,
            ('queued','submission_started','submitted','outcome_unknown','needs_attention','failed'))

    def _setPaymentOperationState(self,operationId,state,reason,fromStates):
        row=self.operationById(operationId)
        def operation(connection):
            with connection.cursor() as cursor:
                self._lockUser(cursor,row['user_id'])
                cursor.execute('''UPDATE public.payment_operations SET state=%s,error_code=%s,
                    updated_at=clock_timestamp() WHERE id=%s AND state=ANY(%s)''',(state,reason,operationId,list(fromStates)))
        self._run(operation)

    def escalateOperationOnce(self,operationId,reason):
        row=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,row['user_id'])
                cursor.execute('''UPDATE public.payment_operations SET escalation_reference=%s,
                    updated_at=clock_timestamp() WHERE id=%s AND escalation_reference IS NULL
                    AND state NOT IN ('succeeded','cancelled') RETURNING *''',('refund:'+operationId,operationId))
                op=cursor.fetchone()
                if not op:
                    return False
                cursor.execute('''INSERT INTO public.billing_events(user_id,invoice_id,event_category,event_type,event_status,
                    idempotency_key,metadata_json) VALUES(%s,%s,'system','payment.refund_escalated','OPEN',%s,%s)
                    ON CONFLICT(idempotency_key) DO NOTHING''', (row['user_id'],row['invoice_id'],
                    'refund:escalation:'+operationId,Json({'operationId':operationId,'caseReference':'refund:'+operationId,'reasonCode':reason})))
                return True
        created=self._run(operation)
        if created:
            from utils.logger import logger
            logger.bind(operationId=operationId,caseReference='refund:'+operationId,reasonCode=reason).error('Purchase financial resolution requires attention')
        return bool(created)

    def scheduleRefundRetry(self,operationId,now):
        row=self.operationById(operationId)
        threshold=max(0,int(os.environ.get('PAYMENT_REFUND_ESCALATION_SECONDS','1800')))
        escalated=False
        if row['state'] not in ('succeeded','cancelled') and (now-row['created_at']).total_seconds()>=threshold:
            escalated=self.escalateOperationOnce(operationId,'PAYMENT_REFUND_OVERDUE')
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,row['user_id'])
                cursor.execute('SELECT retry_count FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                retryCount=cursor.fetchone()['retry_count']
                cursor.execute('''UPDATE public.payment_operations SET next_retry_at=%s,
                    retry_count=retry_count+1,updated_at=clock_timestamp() WHERE id=%s AND state NOT IN ('succeeded','cancelled')''',
                    (now+timedelta(seconds=min(30*2**min(retryCount,7),3600)),operationId))
        self._run(operation)
        return escalated

    def recordPaymentWorkerHeartbeat(self):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                if not self._purchaseSchemaReady(cursor):
                    return False
                cursor.execute('''INSERT INTO private.payment_recovery_runtime(singleton,worker_seen_at)
                    VALUES(true,clock_timestamp()) ON CONFLICT(singleton) DO UPDATE SET worker_seen_at=EXCLUDED.worker_seen_at''')
                return True
        return self._run(operation)

    def paymentScanHighWater(self,source):
        table,key=self._paymentScanSource(source)
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT {key} FROM public.{table} ORDER BY {key} DESC LIMIT 1')
                row=cursor.fetchone()
                return row[0] if row else None
        return self._run(operation)

    @staticmethod
    def _paymentScanSource(source):
        if source=='refund':
            return 'payment_operations','id'
        raise ValueError('INVALID_PAYMENT_OPERATION_SCAN')

    def paymentScanPage(self,source,highWater,afterId,now,limit=100):
        table,key=self._paymentScanSource(source)
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('INVALID_PAYMENT_OPERATION_SCAN')
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(f'''SELECT *,{key} AS id FROM public.{table} WHERE kind=%s
                    AND state NOT IN ('succeeded','cancelled') AND next_retry_at<=%s
                    AND {key}<=%s AND (%s::uuid IS NULL OR {key}>%s::uuid) ORDER BY {key} LIMIT %s''',
                    [source,now,highWater,afterId,afterId,limit])
                return list(cursor.fetchall())
        return self._run(operation)

    def purchaseAttempt(self,attemptId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT * FROM public.billing_events WHERE id=%s',(attemptId,))
                row=cursor.fetchone()
                if not row:
                    raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
                return dict(row)
        return self._run(operation)

    def purchaseControl(self,attemptId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('SELECT * FROM public.payment_slot_controls WHERE attempt_id=%s',(attemptId,))
                row=cursor.fetchone()
                return dict(row) if row else None
        return self._run(operation)

    def recoverPaymentOperation(self,source,row,provider,now):
        identity=str(row['id'])
        self._paymentScanSource(source)
        from api.services.billing.paymentRefundService import PaymentRefundService
        result=PaymentRefundService(self,provider).execute(identity)
        if result['state']=='needs_attention' and result.get('error_code')=='PAYMENT_REFUND_FAILED':
            self.prepareFailedRefundRetry(identity)
        resolved=result['state'] in ('succeeded','cancelled')
        escalated=False if resolved else self.scheduleRefundRetry(identity,now)
        return {'resolved':resolved,'escalated':escalated}
