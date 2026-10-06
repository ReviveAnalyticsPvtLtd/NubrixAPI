"""Durable manual-period usage; Redis cannot create paid coverage or a quota."""
import uuid
from datetime import datetime, timezone
from psycopg2.extras import Json, RealDictCursor
from api.services.billing.manualBillingContracts import CreditOperationContext
from api.services.billing.manualBillingRepository import getManualBillingRepository, _utc


class ManualCreditRepository:
    def __init__(self, repository=None):
        self.repository = repository or getManualBillingRepository()

    def _monthlySettled(self, cursor, userId, periodId):
        cursor.execute("select metadata_json from public.billing_events where user_id=%s and event_type='credit.operation_settled'", (userId,))
        records = [self.repository._json(row['metadata_json']) for row in cursor.fetchall()]
        return sum(int(row.get('monthlyCharged', 0)) for row in records if row.get('creditPeriodId') == periodId)

    def admit(self, userId, operationType, operationId):
        now = datetime.now(timezone.utc)
        self.repository.activateDueCoverage(userId, now)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor, userId)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',
                    ('credit-admit:'+userId+':'+operationId,))
                existing = cursor.fetchone()
                if existing:
                    stored = self.repository._json(existing['metadata_json'])
                    if stored['operationType'] != operationType:
                        raise ValueError('CREDIT_OPERATION_IDENTITY_CONFLICT')
                    return CreditOperationContext(userId,stored['lifecycleId'],stored['creditPeriodId'],
                        operationId,operationType,operationId,_utc(stored['admittedAt']))
                subscription = self.repository._canonical(cursor, userId)
                if subscription.get('billing_mode') != 'monthly_prepaid' or subscription.get('erasure_pending') or not _utc(subscription.get('current_period_start')) <= now < _utc(subscription.get('current_period_end')):
                    raise ValueError('CREDIT_ADMISSION_REQUIRES_PAID_COVERAGE')
                cursor.execute('select * from public.credit_balances where user_id=%s for update', (userId,))
                balance = cursor.fetchone()
                if not balance or not balance.get('credit_period_id'):
                    raise ValueError('CREDIT_PERIOD_MISSING')
                context = CreditOperationContext(userId, str(balance['lifecycle_id']), str(balance['credit_period_id']), operationId, operationType, operationId, now)
                snapshot = {'lifecycleId': context.lifecycleId, 'creditPeriodId': context.creditPeriodId,
                    'operationId': operationId, 'operationType': operationType, 'admittedAt': now.isoformat(),
                    'monthlyRemaining': int(balance['remaining_tokens']),
                    'settledMonthly': self._monthlySettled(cursor, userId, context.creditPeriodId)}
                cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                    values(%s,%s,%s,'audit','credit.operation_admitted','ADMITTED',%s,%s,%s)
                    on conflict(idempotency_key) do nothing''', (str(uuid.uuid4()), userId, subscription['id'], 'credit-admit:'+userId+':'+operationId, Json(snapshot), now))
                return context
        return self.repository._run(operation)

    def settle(self, context, tokensUsed, runId):
        if tokensUsed <= 0: return {'settled': False}
        key = 'credit-settle:'+context.userId+':'+context.operationId+':'+str(runId)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor, context.userId)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s', (key,))
                existing = cursor.fetchone()
                if existing:
                    self._markUsageSettled(cursor,context,runId)
                    return self.repository._json(existing['metadata_json'])
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s', ('credit-admit:'+context.userId+':'+context.operationId,))
                admitted = cursor.fetchone()
                if not admitted: raise ValueError('CREDIT_ADMISSION_MISSING')
                snapshot = self.repository._json(admitted['metadata_json'])
                if snapshot['creditPeriodId'] != context.creditPeriodId or snapshot['lifecycleId'] != context.lifecycleId:
                    raise ValueError('CREDIT_CONTEXT_MISMATCH')
                cursor.execute('select * from public.credit_balances where user_id=%s for update', (context.userId,))
                balance = cursor.fetchone()
                if not balance: raise ValueError('CREDIT_BALANCE_MISSING')
                now = datetime.now(timezone.utc)
                current = str(balance.get('credit_period_id')) == context.creditPeriodId and _utc(balance['period_end']) > now and int(balance['monthly_token_quota']) > 0
                available = int(balance['remaining_tokens']) if current else max(0, snapshot['monthlyRemaining'] - (self._monthlySettled(cursor, context.userId, context.creditPeriodId) - snapshot['settledMonthly']))
                monthly = min(available, int(tokensUsed))
                topup = min(int(balance.get('topup_tokens') or 0), int(tokensUsed) - monthly)
                metadata = {'creditPeriodId': context.creditPeriodId, 'lifecycleId': context.lifecycleId,
                    'operationId': context.operationId, 'runId': str(runId), 'tokensUsed': int(tokensUsed),
                    'monthlyCharged': monthly, 'topupCharged': topup, 'unfundedTokens': int(tokensUsed)-monthly-topup,
                    'settled': True, 'historicalPeriod': not current}
                cursor.execute('''update public.credit_balances set remaining_tokens=remaining_tokens-%s,
                    used_tokens=used_tokens+%s,topup_tokens=topup_tokens-%s,balance_version=balance_version+1,updated_at=%s where user_id=%s''',
                    (monthly if current else 0, int(tokensUsed) if current else 0, topup, now, context.userId))
                cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                    values(%s,%s,%s,'audit','credit.operation_settled','SETTLED',%s,%s,%s)''',
                    (str(uuid.uuid4()), context.userId, balance['subscription_id'], key, Json(metadata), now))
                self._markUsageSettled(cursor,context,runId)
                return metadata
        return self.repository._run(operation)

    def _markUsageSettled(self,cursor,context,runId):
        cursor.execute("update public.billing_events set event_status='SETTLED' where idempotency_key=%s",
            ('credit-report:'+context.userId+':'+context.operationId+':'+str(runId),))

    def reportUsage(self,context,tokensUsed,runId):
        """Commit measured usage before attempting its balance settlement."""
        key='credit-report:'+context.userId+':'+context.operationId+':'+str(runId)
        metadata={'userId':context.userId,'lifecycleId':context.lifecycleId,'creditPeriodId':context.creditPeriodId,
            'operationId':context.operationId,'operationType':context.operationType,'admittedAt':context.admittedAt.isoformat(),
            'runId':str(runId),'tokensUsed':int(tokensUsed)}
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor,context.userId)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',(key,))
                previous=cursor.fetchone()
                if previous:
                    if self.repository._json(previous['metadata_json']) != metadata:
                        raise ValueError('CREDIT_USAGE_IDENTITY_CONFLICT')
                    return
                cursor.execute('''insert into public.billing_events(id,user_id,event_category,event_type,event_status,
                    idempotency_key,metadata_json,occurred_at) values(%s,%s,'audit','credit.usage_reported','PENDING',%s,%s,%s)''',
                    (str(uuid.uuid4()),context.userId,key,Json(metadata),datetime.now(timezone.utc)))
        self.repository._run(operation)

    def recoverUsage(self,limit=100):
        def load(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select id,metadata_json from public.billing_events where event_type='credit.usage_reported' and event_status='PENDING' and user_id is not null order by coalesce((metadata_json->>'lastRecoveryAt')::timestamptz,updated_at),id limit %s",(limit,))
                return list(cursor.fetchall())
        summary={'settled':0,'errors':0}
        for row in self.repository._run(load):
            data=self.repository._json(row['metadata_json'])
            context=CreditOperationContext(data['userId'],data['lifecycleId'],data['creditPeriodId'],data['operationId'],
                data['operationType'],data['operationId'],_utc(data['admittedAt']))
            try:
                self.settle(context,data['tokensUsed'],data['runId'])
                summary['settled']+=1
            except Exception:
                summary['errors']+=1
                self.repository.recordRecoveryCheck(str(row['id']))
        return summary
