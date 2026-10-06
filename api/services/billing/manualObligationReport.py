"""Read-only operator projection; shared predicates drive report and metrics."""
import base64
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
import re
from psycopg2.extras import RealDictCursor
from api.services.subscriptions.paymentValidationService import parseUtc


def eventCategory(row, now):
    status = str(row.get('event_status') or '').upper()
    kind = row.get('event_type')
    metadata = row.get('metadata_json') or {}
    if row.get('event_category') == 'payment_attempt':
        payment = row.get('payment_status')
        if payment in ('created','pending_provider_ack','authorized'):
            return 'order_unknown_ack' if not row.get('provider_order_id') else 'capture_pending'
        frozen = metadata.get('manualBilling',{})
        if frozen.get('closedAt') and not frozen.get('closureReconciledAt'):
            return 'closed_attempt_unreconciled'
    if kind in ('payment.capture','payment.unmapped') and status == 'REQUIRES_RECONCILIATION':
        return 'unmapped_capture' if kind == 'payment.unmapped' else 'capture_anomaly'
    if kind == 'refund.intent' and status != 'PROCESSED': return 'refund_obligation'
    if kind == 'credit.usage_reported' and status == 'PENDING': return 'usage_pending'
    if kind == 'credit.operation_settled' and metadata.get('unfundedTokens',0) > 0: return 'usage_unfunded'
    if kind == 'credit.operation_admitted':
        occurred = parseUtc(row.get('occurred_at'))
        if occurred and now-occurred > timedelta(hours=24): return 'admission_without_measurement'
    if kind == 'email.billing_intent.committed' and status == 'COMMITTED':
        return 'notification_held' if metadata.get('metadata',{}).get('holdForRenewalEvidence') else 'notification_unbridged'
    return None


def listObligations(repository, limit=100, cursor=None):
    if isinstance(limit,bool) or not 1 <= limit <= 500: raise ValueError('INVALID_OBLIGATION_LIMIT')
    after = None
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if not isinstance(decoded,list) or len(decoded)!=2 or not all(isinstance(x,str) for x in decoded): raise ValueError()
            if parseUtc(decoded[0]) is None: raise ValueError()
            after = tuple(decoded)
        except Exception as error: raise ValueError('INVALID_OBLIGATION_CURSOR') from error
    now = datetime.now(timezone.utc)
    errors = []
    def load(connection):
        with connection.cursor(cursor_factory=RealDictCursor) as sql:
            sql.execute('''select a.* from public.billing_events a where
                (a.event_category='payment_attempt' and a.payment_status <> 'captured')
                or (a.event_type in ('payment.capture','payment.unmapped') and a.event_status='REQUIRES_RECONCILIATION')
                or (a.event_type='refund.intent' and a.event_status <> 'processed')
                or (a.event_type='credit.usage_reported' and a.event_status='PENDING')
                or (a.event_type='email.billing_intent.committed' and a.event_status='COMMITTED')
                or (a.event_type='credit.operation_settled' and cast(coalesce(a.metadata_json->>'unfundedTokens','0') as bigint)>0)
                or (a.event_type='credit.operation_admitted' and not exists (
                    select 1 from public.billing_events b where b.user_id=a.user_id
                    and b.event_type='credit.usage_reported'
                    and b.metadata_json->>'operationId'=a.metadata_json->>'operationId'))''')
            events = sql.fetchall()
            sql.execute("""select * from public.notification_deliveries where
                status in ('PENDING','RETRY_PENDING','SENDING','ACCEPTED')
                or last_error_code in ('AMBIGUOUS_SEND','AMBIGUOUS_SEND_UNRESOLVED')""")
            return events,sql.fetchall()
    try:
        events,deliveries = repository._run(load)
    except Exception:
        return {'generatedAt':now.isoformat(),'available':False,'items':[],
                'total':None,'totals':None,'nextCursor':None,'errors':['billing_database_unavailable']}
    rows = []
    def project(source,row,category,action):
        occurred = parseUtc(row.get('occurred_at') or row.get('created_at'))
        occurred = occurred or datetime(1970,1,1,tzinfo=timezone.utc)
        code = row.get('failure_reason') or row.get('last_error_code')
        code = code if isinstance(code,str) and re.fullmatch('[A-Z0-9_]{1,80}',code) else None
        metadata = row.get('metadata_json') or {}
        if isinstance(metadata,str): metadata=json.loads(metadata)
        rows.append({'id':str(row['id']),'source':source,'category':category,
            'userId':row.get('user_id'),'invoiceId':str(row['invoice_id']) if row.get('invoice_id') else None,
            'amount':row.get('amount') or metadata.get('amount'), 'currency':row.get('currency'),
            'providerOrderId':row.get('provider_order_id'),'providerPaymentId':row.get('provider_payment_id'),
            'state':row.get('event_status') or row.get('status'),'reasonCode':code,'safeAction':action,
            'occurredAt':occurred.isoformat(),'ageSeconds':max(0,int((now-occurred).total_seconds()))})
    for row in events:
        row['metadata_json'] = repository._json(row.get('metadata_json'))
        category = eventCategory(row,now)
        if category:
            action = ('reconcile_original_provider_identity' if category.startswith(('capture','order','closed'))
                else 'support_review_original_refund' if category=='refund_obligation'
                else 'recover_original_usage' if category=='usage_pending'
                else 'bridge_original_intent' if category.startswith('notification') else 'support_review')
            project('billing_event',row,category,action)
    for row in deliveries:
        category = 'delivery_ambiguous' if row.get('last_error_code') in ('AMBIGUOUS_SEND','AMBIGUOUS_SEND_UNRESOLVED') or (row['status']=='SENDING' and row.get('submission_started_at')) else 'delivery_unresolved'
        project('delivery',row,category,'reconcile_original_tracking_tag' if category=='delivery_ambiguous' else 'dispatch_or_reconcile_original_delivery')
    from api.services.credits.creditUsageSpool import spoolDirectory
    for path in sorted(spoolDirectory().glob('*.json')):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            project('usage_spool',{'id':path.stem,'user_id':None,'metadata_json':{},
                'occurred_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                'event_status':'PENDING'},'usage_spooled','recover_original_usage')
            rows[-1]['tokensUsed']=data.get('tokensUsed')
        except Exception: errors.append('usage_spool_unreadable')
    rows.sort(key=lambda row:(row['occurredAt'],row['source']+':'+row['id']))
    totals=dict(Counter(row['category'] for row in rows))
    remaining=[row for row in rows if after is None or (row['occurredAt'],row['source']+':'+row['id']) > after]
    items=remaining[:limit]
    nextCursor=None
    if len(remaining)>limit:
        last=items[-1]
        nextCursor=base64.urlsafe_b64encode(json.dumps([last['occurredAt'],last['source']+':'+last['id']]).encode()).decode()
    return {'generatedAt':now.isoformat(),'available':not errors,'items':items,'total':len(rows),
            'totals':totals,'nextCursor':nextCursor,'errors':errors}
