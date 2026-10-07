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


# SQL classifies and pages obligations before their metadata enters Python.
# Counts use the same expression, keeping operator pages and metrics aligned.
_EVENT_CLASSIFICATION = """case
 when a.event_category='payment_attempt' and a.payment_status in ('created','pending_provider_ack','authorized')
   then case when a.provider_order_id is null then 'order_unknown_ack' else 'capture_pending' end
 when a.event_category='payment_attempt' and a.payment_status<>'captured'
   and a.metadata_json->'manualBilling'->>'closedAt' is not null
   and a.metadata_json->'manualBilling'->>'closureReconciledAt' is null then 'closed_attempt_unreconciled'
 when a.event_type in ('payment.capture','payment.unmapped') and upper(a.event_status)='REQUIRES_RECONCILIATION'
   then case when a.event_type='payment.unmapped' then 'unmapped_capture' else 'capture_anomaly' end
 when a.event_type='refund.intent' and upper(a.event_status)<>'PROCESSED' then 'refund_obligation'
 when a.event_type='credit.usage_reported' and a.event_status='PENDING' then 'usage_pending'
 when a.event_type='credit.operation_settled' and cast(coalesce(a.metadata_json->>'unfundedTokens','0') as bigint)>0 then 'usage_unfunded'
 when a.event_type='credit.operation_admitted' and a.event_status<>'MEASURED' and coalesce(a.occurred_at,a.created_at)<%s
   and not exists (select 1 from public.billing_events b where b.user_id=a.user_id
     and b.event_type in ('credit.usage_reported','credit.operation_settled')
     and b.metadata_json->>'operationId'=a.metadata_json->>'operationId') then 'admission_without_measurement'
 when a.event_type='email.billing_intent.committed' and a.event_status='COMMITTED'
   then case when cast(a.metadata_json->'metadata'->>'holdForRenewalEvidence' as text) in ('true','1') then 'notification_held' else 'notification_unbridged' end
 end"""
_DELIVERY_FILTER = """status in ('PENDING','RETRY_PENDING','SENDING','ACCEPTED')
 or last_error_code in ('AMBIGUOUS_SEND','AMBIGUOUS_SEND_UNRESOLVED')"""
_DELIVERY_CLASSIFICATION = """case when last_error_code in ('AMBIGUOUS_SEND','AMBIGUOUS_SEND_UNRESOLVED')
 or (status='SENDING' and submission_started_at is not null) then 'delivery_ambiguous' else 'delivery_unresolved' end"""
_EVENT_FILTER = """(event_category='payment_attempt' and
    (payment_status in ('created','pending_provider_ack','authorized') or
      (payment_status<>'captured' and metadata_json->'manualBilling'->>'closedAt' is not null
        and metadata_json->'manualBilling'->>'closureReconciledAt' is null)))
 or (event_type in ('payment.capture','payment.unmapped') and event_status='REQUIRES_RECONCILIATION')
 or (event_type='refund.intent' and event_status<>'processed')
 or (event_type='credit.usage_reported' and event_status='PENDING')
 or (event_type='credit.operation_settled' and cast(coalesce(metadata_json->>'unfundedTokens','0') as bigint)>0)
 or (event_type='credit.operation_admitted' and event_status<>'MEASURED')
 or (event_type='email.billing_intent.committed' and event_status='COMMITTED')"""

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
        cutoff = now - timedelta(hours=24)
        classifications = [
            ('billing_events', 'a', _EVENT_CLASSIFICATION, 'where ' + _EVENT_FILTER, [cutoff], 'occurred_at'),
            ('notification_deliveries', 'a', _DELIVERY_CLASSIFICATION, 'where ' + _DELIVERY_FILTER, [], 'created_at'),
        ]
        pages = []
        totals = Counter()
        with connection.cursor(cursor_factory=RealDictCursor) as sql:
            for table, alias, classification, condition, parameters, timeField in classifications:
                classified = 'select a.*, ' + classification + ' as obligation_category from public.' + table + ' a ' + condition
                sql.execute('select obligation_category,count(*) as count from (' + classified + ') classified where obligation_category is not null group by obligation_category', parameters)
                totals.update({row['obligation_category']:int(row['count']) for row in sql.fetchall()})
                source = 'billing_event' if table == 'billing_events' else 'delivery'
                timestamp = 'coalesce(' + timeField + ",created_at,'1970-01-01T00:00:00+00:00')"
                key = "'" + source + ":'||id::text"
                afterFilter = ' and (' + timestamp + ',' + key + ')>(%s,%s)' if after else ''
                sql.execute('select * from (' + classified + ') classified where obligation_category is not null' + afterFilter + ' order by ' + timestamp + ',' + key + ' limit %s',
                    parameters + (list(after) if after else []) + [limit+1])
                pages.append(sql.fetchall())
        return pages[0], pages[1], totals
    try:
        events,deliveries,totals = repository._run(load)
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
        category = row['obligation_category']
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
    spoolPaths = sorted(spoolDirectory().glob('*.json'), key=lambda path:(path.stat().st_mtime,path.stem))
    totals['usage_spooled'] += len(spoolPaths)
    if not spoolPaths:
        totals.pop('usage_spooled',None)
    spoolSeen = 0
    for path in spoolPaths:
        stamp=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()
        if after and (stamp,'usage_spool:'+path.stem)<=after:
            continue
        if spoolSeen>=limit+1:
            break
        spoolSeen+=1
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            project('usage_spool',{'id':path.stem,'user_id':None,'metadata_json':{},
                'occurred_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                'event_status':'PENDING'},'usage_spooled','recover_original_usage')
            rows[-1]['tokensUsed']=data.get('tokensUsed')
        except Exception: errors.append('usage_spool_unreadable')
    rows.sort(key=lambda row:(row['occurredAt'],row['source']+':'+row['id']))
    totals=dict(totals)
    remaining=[row for row in rows if after is None or (row['occurredAt'],row['source']+':'+row['id']) > after]
    items=remaining[:limit]
    nextCursor=None
    if len(remaining)>limit:
        last=items[-1]
        nextCursor=base64.urlsafe_b64encode(json.dumps([last['occurredAt'],last['source']+':'+last['id']]).encode()).decode()
    return {'generatedAt':now.isoformat(),'available':not errors,'items':items,'total':sum(totals.values()),
            'totals':totals,'nextCursor':nextCursor,'errors':errors}
