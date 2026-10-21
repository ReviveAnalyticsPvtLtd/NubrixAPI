"""Owner-locked persistence for correction refunds and durable recovery scans."""
from datetime import timedelta
from dataclasses import replace
import os
from uuid import UUID
from psycopg2.extras import Json, RealDictCursor
from api.services.billing.manualBillingRepository import _now, _utc


class PurchaseOperationRepositoryMixin:
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
        if not PurchaseOperationRepositoryMixin._purchaseSchemaReady(cursor):
            return False
        cursor.execute('''SELECT 1 FROM private.payment_recovery_runtime
            WHERE worker_seen_at BETWEEN clock_timestamp()-interval '5 minutes' AND clock_timestamp()''')
        return cursor.fetchone() is not None

    @staticmethod
    def _purchaseRecoveryReady(cursor):
        from api.services.billing.purchaseCheckoutService import purchaseActionsReady
        return purchaseActionsReady() and PurchaseOperationRepositoryMixin._paymentWorkerFresh(cursor)

    def _purchasePresentation(self,cursor,checkout,draft,allowed):
        from api.services.billing.purchaseCheckoutContracts import PurchaseCheckoutCapabilities
        from api.services.billing.purchaseDraftService import editableCheckoutEnabled
        ready=self._purchaseRecoveryReady(cursor)
        if checkout:
            cursor.execute("SELECT state FROM public.payment_operations WHERE attempt_id=%s AND kind='refund'",(checkout.attemptId,))
            states={r['state'] for r in cursor.fetchall()}
            if states:
                refund='completed' if states=={'succeeded'} else 'needs_attention' if 'needs_attention' in states else 'submitted' if 'submitted' in states else 'pending'
                checkout=replace(checkout,refundState=refund,
                    pendingReason=None if refund=='completed' else 'PURCHASE_CORRECTION_PENDING')
        # Owed refunds and other money never block Pay; the server decides open, reuse or replace.
        capabilities=PurchaseCheckoutCapabilities(
            mayEditDraft=bool(allowed and editableCheckoutEnabled()),
            maySubmitLatestDraft=bool(ready and allowed and draft.revision>0))
        return replace(checkout,capabilities=capabilities) if checkout else None,capabilities

    def projectPurchaseStatus(self,status):
        from api.services.billing.purchaseCheckoutContracts import PurchaseDraft
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT to_regclass('public.payment_operations') AS relation")
                if not cursor.fetchone()['relation']:
                    return status
                subscription,allowed=self._editorContext(cursor,status.userId)
                cursor.execute('SELECT * FROM public.subscription_purchase_drafts WHERE user_id=%s',(status.userId,))
                stored=cursor.fetchone()
                draft=self._draft(status.userId,stored) if stored else PurchaseDraft(status.userId)
                return self._purchasePresentation(cursor,status,draft,allowed)[0]
        return self._run(operation)

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

    def reserveCompensation(self,evidence):
        if evidence.financialStatus != 'captured' or evidence.purpose != 'initial_purchase':
            raise ValueError('PURCHASE_COMPENSATION_EVIDENCE_INVALID')
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT to_regclass('public.payment_slot_controls') AS relation")
                if not cursor.fetchone()['relation']:
                    return None
                self._requireStorage(cursor)
                attempt,invoice,control = self._operationContext(cursor,evidence.attemptId)
                if not control or control['closed_reason']!='SUPERSEDED' or invoice['status']=='PAID':
                    return None
                if (str(attempt['invoice_id'])!=evidence.invoiceId or attempt['provider_order_id']!=evidence.providerOrderId
                        or attempt['amount']!=evidence.amount or attempt['currency']!=evidence.currency
                        or invoice['total_amount']!=evidence.amount or invoice['currency']!=evidence.currency
                        or self._json(attempt['metadata_json']).get('manualBilling',{}).get('purpose')!='initial_purchase'):
                    raise ValueError('PURCHASE_COMPENSATION_EVIDENCE_INVALID')
                if attempt['user_id'] != evidence.userId:
                    if attempt['user_id'] is not None or not control or not control['erasure_request_id']:
                        raise ValueError('PURCHASE_COMPENSATION_OWNER_MISMATCH')
                    cursor.execute('SELECT 1 FROM private.retained_user_subjects WHERE request_id=%s AND original_user_id=%s',
                        (control['erasure_request_id'],evidence.userId))
                    if evidence.userId is not None and not cursor.fetchone():
                        raise ValueError('PURCHASE_COMPENSATION_OWNER_MISMATCH')
                frozen=self._json(attempt['metadata_json']).get('manualBilling',{})
                if (invoice['status']!='VOID' or frozen.get('closedReason')!='SUPERSEDED' or not control['closed_at']
                        or _utc(frozen.get('closedAt'))!=control['closed_at']):
                    raise ValueError('PURCHASE_SUPERSESSION_PROOF_INVALID')
                cursor.execute('''SELECT * FROM public.billing_events WHERE provider_payment_id=%s
                    AND event_type='payment.capture' FOR UPDATE''',(evidence.providerPaymentId,))
                capture=cursor.fetchone()
                if (not capture or str(capture['invoice_id'])!=evidence.invoiceId
                        or capture['provider_order_id']!=evidence.providerOrderId or capture['amount']!=evidence.amount
                        or capture['currency']!=evidence.currency or capture['event_status']!='REQUIRES_RECONCILIATION'):
                    raise ValueError('PURCHASE_COMPENSATION_CAPTURE_NOT_VERIFIED')
                cursor.execute('SELECT * FROM public.payment_operations WHERE kind=\'refund\' AND provider_payment_id=%s',
                    (evidence.providerPaymentId,))
                prior=cursor.fetchone()
                if prior:
                    return dict(prior)
                # Never compete with a pre-existing ordinary refund obligation.
                cursor.execute("SELECT 1 FROM public.billing_events WHERE invoice_id=%s AND event_type='refund.intent' LIMIT 1",(evidence.invoiceId,))
                if cursor.fetchone():
                    raise ValueError('PURCHASE_EXISTING_REFUND_OBLIGATION')
                cursor.execute('''INSERT INTO public.payment_operations
                    (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key,erasure_request_id)
                    VALUES(%s,%s,%s,%s,'refund','paid_after_close',%s,%s,%s,%s) RETURNING *''',
                    (attempt['user_id'],evidence.attemptId,evidence.invoiceId,evidence.providerPaymentId,evidence.amount,
                     evidence.currency,'payment:refund:'+evidence.providerPaymentId,control['erasure_request_id']))
                result=dict(cursor.fetchone())
                metadata=self._json(attempt['metadata_json'])
                metadata['manualBilling'].pop('closureReconciledAt',None)
                cursor.execute('UPDATE public.billing_events SET metadata_json=%s WHERE id=%s',(Json(metadata),evidence.attemptId))
                return result
        return self._run(operation)

    def operationById(self,operationId):
        UUID(str(operationId))
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''SELECT o.*,c.operation_revision,c.provider_order_id FROM public.payment_operations o
                    JOIN public.payment_slot_controls c ON c.attempt_id=o.attempt_id WHERE o.id=%s''',(operationId,))
                row=cursor.fetchone()
                if not row:
                    raise ValueError('PURCHASE_OPERATION_MISSING')
                return dict(row)
        return self._run(operation)

    def beginCompensationSubmission(self,operationId,expectedRevision):
        observed=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                attempt,invoice,control=self._operationContext(cursor,str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                op=cursor.fetchone()
                facts=self._json(op['result'])
                if (op['kind']!='refund' or op['state']!='queued' or self._json(op['submission_evidence']).get('startedAt')
                        or control['operation_revision']!=expectedRevision or control['closed_reason']!='SUPERSEDED'
                        or invoice['status']=='PAID' or facts.get('pending')!=0 or not facts.get('checkedAt')
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
                    raise ValueError('PURCHASE_OPERATION_KIND_INVALID')
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
                elif state=='needs_attention' and op['error_code'] in ('PURCHASE_PROVIDER_RETRIES_ENABLED','PURCHASE_REFUND_HISTORY_INVALID'):
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
                    'PURCHASE_REFUND_FAILED' if state=='needs_attention' and own else None,operationId))
        self._run(operation)
        return self.operationById(operationId)

    def prepareFailedCompensationRetry(self,operationId):
        observed=self.operationById(operationId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._operationContext(cursor,str(observed['attempt_id']))
                cursor.execute('SELECT * FROM public.payment_operations WHERE id=%s FOR UPDATE',(operationId,))
                op=cursor.fetchone()
                evidence=self._json(op['submission_evidence'])
                facts=self._json(op['result'])
                own=[r for r in facts.get('refunds',[]) if r['ours'] and r.get('submissionKey')==evidence.get('key')]
                if (op['kind']!='refund' or op['state']!='needs_attention' or op['error_code']!='PURCHASE_REFUND_FAILED'
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

    def markCompensationUnknown(self,operationId):
        self._setPurchaseOperationState(operationId,'outcome_unknown','PURCHASE_REFUND_OUTCOME_UNKNOWN',
            ('submission_started','submitted','outcome_unknown'))

    def failPurchaseOperation(self,operationId,reason):
        self._setPurchaseOperationState(operationId,'needs_attention',reason,
            ('queued','submission_started','submitted','outcome_unknown','needs_attention','failed'))

    def _setPurchaseOperationState(self,operationId,state,reason,fromStates):
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
                    AND state NOT IN ('succeeded','cancelled') RETURNING *''',('purchase:'+operationId,operationId))
                op=cursor.fetchone()
                if not op:
                    return False
                cursor.execute('''INSERT INTO public.billing_events(user_id,invoice_id,event_category,event_type,event_status,
                    idempotency_key,metadata_json) VALUES(%s,%s,'system','purchase.operation_escalated','OPEN',%s,%s)
                    ON CONFLICT(idempotency_key) DO NOTHING''', (row['user_id'],row['invoice_id'],
                    'purchase:escalation:'+operationId,Json({'operationId':operationId,'caseReference':'purchase:'+operationId,'reasonCode':reason})))
                return True
        created=self._run(operation)
        if created:
            from utils.logger import logger
            logger.bind(operationId=operationId,caseReference='purchase:'+operationId,reasonCode=reason).error('Purchase financial resolution requires attention')
        return bool(created)

    def schedulePurchaseRetry(self,operationId,now):
        row=self.operationById(operationId)
        threshold=max(0,int(os.environ.get('PURCHASE_RESOLUTION_ESCALATION_SECONDS','1800')))
        escalated=False
        if row['state'] not in ('succeeded','cancelled') and (now-row['created_at']).total_seconds()>=threshold:
            escalated=self.escalateOperationOnce(operationId,'PURCHASE_RESOLUTION_OVERDUE')
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

    def recordPurchaseWorkerHeartbeat(self):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                if not self._purchaseSchemaReady(cursor):
                    return False
                cursor.execute('''INSERT INTO private.payment_recovery_runtime(singleton,worker_seen_at)
                    VALUES(true,clock_timestamp()) ON CONFLICT(singleton) DO UPDATE SET worker_seen_at=EXCLUDED.worker_seen_at''')
                return True
        return self._run(operation)

    def purchaseScanHighWater(self,source):
        table,key=self._purchaseScanSource(source)
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT {key} FROM public.{table} ORDER BY {key} DESC LIMIT 1')
                row=cursor.fetchone()
                return row[0] if row else None
        return self._run(operation)

    @staticmethod
    def _purchaseScanSource(source):
        if source=='refund':
            return 'payment_operations','id'
        raise ValueError('INVALID_PURCHASE_OPERATION_SCAN')

    def purchaseScanPage(self,source,highWater,afterId,now,limit=100):
        table,key=self._purchaseScanSource(source)
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError('INVALID_PURCHASE_OPERATION_SCAN')
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

    def recoverPurchaseItem(self,source,row,provider,now):
        identity=str(row['id'])
        self._purchaseScanSource(source)
        from api.services.billing.purchaseCompensationService import PurchaseCompensationService
        result=PurchaseCompensationService(self,provider).execute(identity)
        if result['state']=='needs_attention' and result.get('error_code')=='PURCHASE_REFUND_FAILED':
            self.prepareFailedCompensationRetry(identity)
        resolved=result['state'] in ('succeeded','cancelled')
        escalated=False if resolved else self.schedulePurchaseRetry(identity,now)
        return {'resolved':resolved,'escalated':escalated}
