"""Explicit retained-original policy, separate from active account authority."""
from psycopg2.extras import Json

PROTECTED_EVENTS = ('subscription.legacy_snapshot','subscription.model_backfill',
    'subscription.next_selection','subscription.expert_grant',
    'subscription.model_initialized','subscription.trial_started')

NOTIFICATION_ERASURE_SQL = """
    UPDATE public.notification_deliveries SET
        status = CASE
            WHEN status IN ('SENDING','RETRY_PENDING') AND
                (last_error_code='AMBIGUOUS_SEND' OR submission_started_at IS NOT NULL) THEN 'RETRY_PENDING'
            WHEN status IN ('PENDING', 'RETRY_PENDING', 'SENDING') THEN 'CANCELLED'
            ELSE status END,
        terminal_at=CASE
            WHEN status IN ('SENDING','RETRY_PENDING') AND
                (last_error_code='AMBIGUOUS_SEND' OR submission_started_at IS NOT NULL) THEN terminal_at
            WHEN status IN ('PENDING', 'RETRY_PENDING', 'SENDING') THEN now()
            ELSE terminal_at END,
        last_error_code=CASE WHEN status IN ('SENDING','RETRY_PENDING') AND
            (last_error_code='AMBIGUOUS_SEND' OR submission_started_at IS NOT NULL)
            THEN 'AMBIGUOUS_SEND' ELSE last_error_code END,
        next_reconcile_at=CASE WHEN status IN ('SENDING','RETRY_PENDING') AND
            (last_error_code='AMBIGUOUS_SEND' OR submission_started_at IS NOT NULL)
            THEN coalesce(next_reconcile_at,now()) ELSE next_reconcile_at END,
        user_id = null, subscription_id = null, metadata_json='{}'::jsonb,
        dedupe_key='erased:'||id::text,
        lease_owner = null, lease_expires_at = null, updated_at=now()
    WHERE user_id = %s
"""


class ErasureLeaseLost(RuntimeError):
    pass


def scalar(row):
    return next(iter(row.values())) if isinstance(row,dict) else row[0] if row else None


def retentionAvailable(cursor):
    for table in ('private.retained_user_subjects','private.retained_user_records'):
        cursor.execute('SELECT to_regclass(%s)',(table,))
        if not scalar(cursor.fetchone()):
            return False
    return True


def assertErasureLease(cursor,requestId,workerId,userId=None):
    if not workerId:
        raise ErasureLeaseLost('ERASURE_LEASE_REQUIRED')
    cursor.execute('''SELECT target_user_id FROM public.user_erasure_requests
        WHERE id=%s AND worker_id=%s AND status='IN_PROGRESS'
        AND lease_expires_at>clock_timestamp() FOR UPDATE''',(requestId,workerId))
    row = cursor.fetchone()
    if row is None or (userId is not None and scalar(row)!=userId):
        raise ErasureLeaseLost('ERASURE_LEASE_LOST')


def prepareRetention(cursor,requestId,userId):
    if not retentionAvailable(cursor):
        raise RuntimeError('ERASURE_RETENTION_SCHEMA_UNAVAILABLE')
    cursor.execute('''INSERT INTO private.retained_user_subjects(request_id,original_user_id,policy_code)
        VALUES(%s,%s,'ORIGINAL_AUDITS_RETAINED') ON CONFLICT(request_id) DO NOTHING''',(requestId,userId))
    cursor.execute('SELECT original_user_id FROM private.retained_user_subjects WHERE request_id=%s',(requestId,))
    if scalar(cursor.fetchone())!=userId:
        raise ValueError('ERASURE_RETENTION_OWNER_MISMATCH')
    cursor.execute('SELECT source_table,count(*) FROM private.retained_user_records WHERE request_id=%s GROUP BY source_table',(requestId,))
    manifest = retentionManifest(dict(cursor.fetchall()))
    cursor.execute('UPDATE public.user_erasure_requests SET retention_manifest=%s WHERE id=%s',(Json(manifest),requestId))


def retentionManifest(counts, obligations=None):
    return {'schemaVersion':1,'policyCode':'ORIGINAL_AUDITS_RETAINED',
        'deletionScope':'product_account_and_content','originalIdentifiersRetained':True,
        'originalMetadataRetained':True,'retainedRecordCounts':counts,
        'financialObligationsAtDeletion':obligations or {},
        'retentionExpiresAt':None,'purgeScheduled':False}


PURCHASE_TABLES = ('subscription_purchase_drafts', 'subscription_purchase_checkout_controls', 'subscription_purchase_operations')


def purchaseErasureInventory(cursor, userId):
    present = []
    for table in PURCHASE_TABLES:
        cursor.execute('SELECT to_regclass(%s)', ('public.' + table,))
        present.append(bool(scalar(cursor.fetchone())))
    if not any(present):
        return {}
    if not all(present):
        raise RuntimeError('PURCHASE_STORAGE_UNAVAILABLE')
    counts = {}
    for table in PURCHASE_TABLES:
        cursor.execute(f'SELECT count(*) FROM public.{table} WHERE user_id=%s', (userId,))
        counts[table] = int(scalar(cursor.fetchone()))
    cursor.execute('''SELECT count(*) FROM public.subscription_purchase_operations
        WHERE user_id=%s AND state NOT IN ('succeeded','cancelled')''', (userId,))
    counts['unresolvedPurchaseOperations'] = int(scalar(cursor.fetchone()))
    cursor.execute('''SELECT count(*) FROM public.subscription_purchase_checkout_controls c
        JOIN public.billing_events b ON b.id=c.attempt_id WHERE c.user_id=%s
        AND c.cancellation_requested_at IS NOT NULL AND b.payment_status<>'captured'
        AND b.metadata_json->'manualBilling'->>'closureReconciledAt' IS NULL''',(userId,))
    counts['unresolvedPurchaseCancellations'] = int(scalar(cursor.fetchone()))
    return counts


def archivePurchaseOriginals(cursor, requestId, userId):
    counts = {}
    inventory = purchaseErasureInventory(cursor, userId)
    if inventory.get('unresolvedPurchaseOperations') or inventory.get('unresolvedPurchaseCancellations'):
        raise RuntimeError('PURCHASE_OPERATIONS_UNRESOLVED')
    for table, identity in (('subscription_purchase_checkout_controls', 'attempt_id'), ('subscription_purchase_operations', 'id')):
        if table not in inventory:
            continue
        cursor.execute(f'''INSERT INTO private.retained_user_records
            (request_id,source_table,source_id,original_record,record_hash)
            SELECT %s,%s,e.{identity},to_jsonb(e),encode(sha256(convert_to(to_jsonb(e)::text,'UTF8')),'hex')
            FROM public.{table} e WHERE e.user_id=%s ON CONFLICT(source_table,source_id) DO NOTHING''',
            (requestId, table, userId))
        cursor.execute(f'''SELECT count(*) FROM public.{table} e WHERE e.user_id=%s
            AND NOT EXISTS(SELECT 1 FROM private.retained_user_records a WHERE a.request_id=%s
                AND a.source_table=%s AND a.source_id=e.{identity} AND a.original_record=to_jsonb(e))''',
            (userId, requestId, table))
        if scalar(cursor.fetchone()):
            raise ValueError('ERASURE_RETAINED_COPY_CONFLICT')
        cursor.execute('SELECT count(*) FROM private.retained_user_records WHERE request_id=%s AND source_table=%s',
                       (requestId, table))
        counts[table] = int(scalar(cursor.fetchone()))
    return counts


def archiveOriginals(cursor,requestId,userId):
    counts = archivePurchaseOriginals(cursor, requestId, userId)
    for table,column in (('billing_events','user_id'),('Invoices','"userId"')):
        cursor.execute(f'''SELECT count(*) FROM public."{table}" e WHERE e.{column} IS NOT NULL
            AND e.{column}<>%s AND e.subscription_id IN (SELECT id FROM public.subscriptions WHERE user_id=%s)''',
            (userId,userId))
        if scalar(cursor.fetchone()):
            raise ValueError('ERASURE_FINANCIAL_OWNER_CONFLICT')
        cursor.execute(f'''INSERT INTO private.retained_user_records
            (request_id,source_table,source_id,original_record,record_hash)
            SELECT %s,%s,e.id,to_jsonb(e),encode(sha256(convert_to(to_jsonb(e)::text,'UTF8')),'hex')
            FROM public."{table}" e WHERE e.{column}=%s OR e.subscription_id IN
                (SELECT id FROM public.subscriptions WHERE user_id=%s)
            ON CONFLICT(source_table,source_id) DO NOTHING''',(requestId,table,userId,userId))
        cursor.execute(f'''SELECT count(*) FROM public."{table}" e WHERE
            (e.{column}=%s OR e.subscription_id IN (SELECT id FROM public.subscriptions WHERE user_id=%s))
            AND NOT EXISTS(SELECT 1 FROM private.retained_user_records a WHERE a.request_id=%s
                AND a.source_table=%s AND a.source_id=e.id AND a.original_record=to_jsonb(e))''',
            (userId,userId,requestId,table))
        if scalar(cursor.fetchone()):
            raise ValueError('ERASURE_RETAINED_COPY_CONFLICT')
        cursor.execute('SELECT count(*) FROM private.retained_user_records WHERE request_id=%s AND source_table=%s',(requestId,table))
        counts[table] = int(scalar(cursor.fetchone()))
    cursor.execute('''SELECT
        count(*) FILTER(WHERE event_category='payment_attempt' AND payment_status<>'captured'
            AND metadata_json->'manualBilling'->>'closureReconciledAt' IS NULL),
        count(*) FILTER(WHERE event_type='payment.capture' AND event_status IN ('OBSERVED','REQUIRES_RECONCILIATION')),
        count(*) FILTER(WHERE event_type='refund.intent' AND event_status<>'processed')
        FROM public.billing_events WHERE user_id=%s OR subscription_id IN
            (SELECT id FROM public.subscriptions WHERE user_id=%s)''',(userId,userId))
    obligations = dict(zip(('paymentAttempts','captureReconciliation','approvedRefundIntents'),cursor.fetchone()))
    cursor.execute('UPDATE public.user_erasure_requests SET retention_manifest=%s WHERE id=%s',
        (Json(retentionManifest(counts,obligations)),requestId))


_MANUAL_FIELDS = ('purpose','billingMode','lifecycleId','servicePeriodId','selectionEventId','selectionVersion',
    'invoiceId','attemptId','revision','currency','frozenAmount','payloadHash','periodStart','periodEnd',
    'expiresAt','closedAt','closedReason','closureReconciledAt','orderCreationClaimedAt',
    'zeroOrderLookupAt','coverageState','creditPeriodId','providerPaymentId','revokedAt','refundIntentId',
    'purchaseDraftRevision','capturePolicy','captureWindowMinutes','purchaseCancellationAcceptedAt','authorizedPaymentId')


def _fields(value, keys):
    if not isinstance(value,dict):
        return {}
    return {k:value[k] for k in keys if k in value and isinstance(value[k],(str,int,float,bool,type(None)))}


def operationalMetadata(metadata,requestId):
    """Only fields consumed by financial recovery survive in the active ledger."""
    import json
    metadata = json.loads(metadata) if isinstance(metadata,str) else metadata or {}
    metadata = metadata if isinstance(metadata,dict) else {}
    result = {'erasureRequestId':str(requestId)}
    frozen = metadata.get('manualBilling')
    if isinstance(frozen,dict):
        result['manualBilling'] = _fields(frozen,_MANUAL_FIELDS)
        observation = frozen.get('providerObservation')
        if isinstance(observation,dict):
            result['manualBilling']['providerObservation'] = _fields(observation,
                ('orderId','outcome','checkedAt','complete','hasPayments'))
    if 'refundIntentId' in metadata and isinstance(metadata.get('items'),list):
        result.update(_fields(metadata,('refundIntentId','cutoff','amount','accessExpired','currentAccessPreserved','refundState')))
        # Absent/malformed send history is unknown, never proof of non-submission.
        # Even an invalid value for an existing key must keep its once-only fence.
        submissions = metadata.get('submissions')
        if isinstance(submissions,dict):
            result['submissions'] = {key:(_fields(value,('state','claimedAt')) if isinstance(value,dict)
                else value if isinstance(value,str) else 'unknown') for key,value in submissions.items()}
        result['userId'] = None
        result['items'] = [_fields(item,('paymentId','invoiceId','amount','currency','originalAmount',
            'intervalStart','intervalEnd','cutoff','creditPeriodId')) for item in metadata['items']]
        result['providerRefunds'] = {key:_fields(value,('id','payment_id','amount','currency','status'))
            for key,value in metadata.get('providerRefunds',{}).items()}
    return result
