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

    @staticmethod
    def _context(data):
        if not all(data.get(key) is not None for key in ('userId','subscriptionId','billingMode','lifecycleId','creditPeriodId','quotaWatermark')):
            raise ValueError('CREDIT_CONTEXT_LEGACY_REQUIRES_RECONCILIATION')
        return CreditOperationContext(data['userId'],data['lifecycleId'],data['creditPeriodId'],data['operationId'],
            data['operationType'],data['operationId'],_utc(data['admittedAt']),data['subscriptionId'],
            data['billingMode'],int(data['quotaWatermark']))

    def _eligibleLocked(self,cursor,subscription,now):
        snapshot=self.repository._coverageSnapshotLocked(cursor,subscription,now,materialize=True)
        if snapshot.denialReason in ('account_banned','erasure_pending','restricted_subscription'):
            return False
        if subscription.get('billing_mode') in ('monthly_prepaid','annual_prepaid'):
            return snapshot.accessAllowed
        start,end=_utc(subscription.get('current_period_start')),_utc(subscription.get('current_period_end'))
        return subscription.get('status')=='trial' and subscription.get('plan_type')=='free' and bool(start and end and start<=now<end)

    def _balanceLocked(self,cursor,subscription,now):
        from dateutil.relativedelta import relativedelta
        from api.services.credits.creditConfig import getTokenQuotaForPlan
        cursor.execute('select * from public.credit_balances where user_id=%s for update',(subscription['user_id'],))
        balance=cursor.fetchone()
        mode=subscription.get('billing_mode') or 'none'
        state=self.repository._json(subscription.get('billing_state'))
        lifecycle=state.get('manualBilling',{}).get('lifecycleId') or str(subscription['id'])
        plan='annual' if mode=='annual_prepaid' else 'pro' if mode=='monthly_prepaid' else 'free'
        if not balance:
            if plan!='free':
                raise ValueError('CREDIT_PERIOD_MISSING')
            quota=getTokenQuotaForPlan('free',subscription.get('domain_count') or 1)
            cursor.execute('''insert into public.credit_balances(user_id,subscription_id,plan_tier,domain_count,
                monthly_token_quota,used_tokens,remaining_tokens,period_start,period_end,lifecycle_id,
                credit_period_id,balance_version,last_reset_at,updated_at)
                values(%s,%s,'free',%s,%s,0,%s,%s,%s,%s,%s,1,%s,%s)''',
                (subscription['user_id'],subscription['id'],subscription.get('domain_count') or 1,quota,quota,
                subscription['current_period_start'],subscription['current_period_end'],lifecycle,str(uuid.uuid4()),now,now))
            cursor.execute('select * from public.credit_balances where user_id=%s for update',(subscription['user_id'],))
            balance=cursor.fetchone()
        # Legacy rows can receive an identity without a refill; current usage and top-ups survive.
        if not balance.get('credit_period_id') or not balance.get('lifecycle_id'):
            cursor.execute('''update public.credit_balances set subscription_id=%s,lifecycle_id=%s,
                credit_period_id=%s,balance_version=balance_version+1,updated_at=%s where user_id=%s''',
                (subscription['id'],lifecycle,str(uuid.uuid4()),now,subscription['user_id']))
            cursor.execute('select * from public.credit_balances where user_id=%s for update',(subscription['user_id'],))
            balance=cursor.fetchone()
        if str(balance.get('subscription_id'))!=str(subscription['id']) or balance.get('plan_tier')!=plan or str(balance.get('lifecycle_id'))!=str(lifecycle):
            raise ValueError('CREDIT_ALLOCATION_MODE_OR_OWNER_MISMATCH')
        if mode=='annual_prepaid' and _utc(balance['period_end'])<=now:
            start=_utc(balance['period_end']);end=start+relativedelta(months=1)
            while end<=now:
                start,end=end,end+relativedelta(months=1)
            quota=getTokenQuotaForPlan('annual',subscription.get('domain_count') or 1)
            cursor.execute('''update public.credit_balances set monthly_token_quota=%s,used_tokens=0,
                remaining_tokens=%s,period_start=%s,period_end=%s,credit_period_id=%s,
                balance_version=balance_version+1,last_reset_at=%s,updated_at=%s where user_id=%s''',
                (quota,quota,start,end,str(uuid.uuid4()),now,now,subscription['user_id']))
            cursor.execute('select * from public.credit_balances where user_id=%s for update',(subscription['user_id'],))
            balance=cursor.fetchone()
        return balance

    def balanceSnapshot(self,userId):
        now=datetime.now(timezone.utc)
        self.repository.activateDueCoverage(userId,now)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor,userId)
                subscription=self.repository._canonical(cursor,userId)
                if self._eligibleLocked(cursor,subscription,now):
                    return self._balanceLocked(cursor,subscription,now)
                cursor.execute('select * from public.credit_balances where user_id=%s for update',(userId,))
                balance=cursor.fetchone() or {'user_id':userId,'topup_tokens':0}
                return {**balance,'monthly_token_quota':0,'remaining_tokens':0}
        return self.repository._run(operation)

    def admit(self,userId,operationType,operationId):
        from api.services.credits.creditConfig import getOperationMinimum
        if not operationId or not operationType:
            raise ValueError('CREDIT_OPERATION_IDENTITY_REQUIRED')
        now=datetime.now(timezone.utc)
        self.repository.activateDueCoverage(userId,now)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor,userId)
                subscription=self.repository._canonical(cursor,userId)
                # An old admission is a settlement identity, never authorization for a retry.
                if not self._eligibleLocked(cursor,subscription,now):
                    raise ValueError('CREDIT_ADMISSION_REQUIRES_PAID_COVERAGE')
                balance=self._balanceLocked(cursor,subscription,now)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',
                    ('credit-admit:'+userId+':'+operationId,))
                existing=cursor.fetchone()
                if existing:
                    stored=self.repository._json(existing['metadata_json'])
                    context=self._context(stored)
                    if stored['operationType']!=operationType:
                        raise ValueError('CREDIT_OPERATION_IDENTITY_CONFLICT')
                    if (context.billingMode!=subscription['billing_mode'] or context.creditPeriodId!=str(balance['credit_period_id'])
                            or context.lifecycleId!=str(balance['lifecycle_id'])):
                        raise ValueError('CREDIT_OPERATION_RETRY_REQUIRES_NEW_EXECUTION_IDENTITY')
                    return context
                if int(balance['remaining_tokens'])+int(balance.get('topup_tokens') or 0)<getOperationMinimum(operationType):
                    raise ValueError('CREDIT_ADMISSION_INSUFFICIENT_BALANCE')
                allocationKey='credit-allocation:'+userId+':'+str(balance['credit_period_id'])
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',(allocationKey,))
                allocation=cursor.fetchone()
                watermark=(self.repository._json(allocation['metadata_json'])['quotaWatermark'] if allocation else
                    int(balance['remaining_tokens'])+self._monthlySettled(cursor,userId,str(balance['credit_period_id'])))
                mode=subscription.get('billing_mode') or 'none'
                snapshot={'userId':userId,'subscriptionId':str(subscription['id']),'billingMode':mode,
                    'lifecycleId':str(balance['lifecycle_id']),'creditPeriodId':str(balance['credit_period_id']),
                    'quotaWatermark':watermark,'operationId':operationId,'operationType':operationType,'admittedAt':now.isoformat()}
                if not allocation:
                    cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                        values(%s,%s,%s,'audit','credit.allocation_started','ACTIVE',%s,%s,%s)''',
                        (str(uuid.uuid4()),userId,subscription['id'],allocationKey,Json(snapshot),now))
                cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                    values(%s,%s,%s,'audit','credit.operation_admitted','ADMITTED',%s,%s,%s)''',
                    (str(uuid.uuid4()),userId,subscription['id'],'credit-admit:'+userId+':'+operationId,Json(snapshot),now))
                return self._context(snapshot)
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
                    committed=self.repository._json(existing['metadata_json'])
                    if (committed.get('subscriptionId')!=context.subscriptionId or committed.get('billingMode')!=context.billingMode
                            or committed.get('creditPeriodId')!=context.creditPeriodId or committed.get('lifecycleId')!=context.lifecycleId
                            or committed.get('tokensUsed')!=int(tokensUsed)):
                        raise ValueError('CREDIT_CONTEXT_OR_USAGE_IDENTITY_CONFLICT')
                    self._markUsageSettled(cursor,context,runId)
                    return committed
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s', ('credit-admit:'+context.userId+':'+context.operationId,))
                admitted = cursor.fetchone()
                if not admitted: raise ValueError('CREDIT_ADMISSION_MISSING')
                snapshot = self.repository._json(admitted['metadata_json'])
                expected=self._context(snapshot)
                if expected != context:
                    raise ValueError('CREDIT_CONTEXT_MISMATCH')
                subscription=self.repository._canonical(cursor,context.userId)
                cursor.execute('select * from public.credit_balances where user_id=%s for update', (context.userId,))
                balance = cursor.fetchone()
                if not balance: raise ValueError('CREDIT_BALANCE_MISSING')
                now = datetime.now(timezone.utc)
                current = (str(balance.get('credit_period_id')) == context.creditPeriodId
                    and str(balance.get('subscription_id'))==context.subscriptionId
                    and str(balance.get('lifecycle_id'))==context.lifecycleId
                    and subscription.get('billing_mode')==context.billingMode
                    and str(subscription['id'])==context.subscriptionId
                    and _utc(balance['period_end'])>now and int(balance['monthly_token_quota'])>0)
                available = int(balance['remaining_tokens']) if current else max(0, int(snapshot['quotaWatermark']) - self._monthlySettled(cursor, context.userId, context.creditPeriodId))
                monthly = min(available, int(tokensUsed))
                topup = min(int(balance.get('topup_tokens') or 0), int(tokensUsed) - monthly)
                metadata = {'subscriptionId':context.subscriptionId,'billingMode':context.billingMode,'quotaWatermark':context.quotaWatermark, 'creditPeriodId': context.creditPeriodId, 'lifecycleId': context.lifecycleId,
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
        metadata={'subscriptionId':context.subscriptionId,'billingMode':context.billingMode,'quotaWatermark':context.quotaWatermark,'userId':context.userId,'lifecycleId':context.lifecycleId,'creditPeriodId':context.creditPeriodId,
            'operationId':context.operationId,'operationType':context.operationType,'admittedAt':context.admittedAt.isoformat(),
            'runId':str(runId),'tokensUsed':int(tokensUsed)}
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor,context.userId)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',
                    ('credit-admit:'+context.userId+':'+context.operationId,))
                admission=cursor.fetchone()
                if not admission or self._context(self.repository._json(admission['metadata_json']))!=context:
                    raise ValueError('CREDIT_CONTEXT_MISMATCH')
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

    def clawbackTopup(self,userId,refundId,paymentId,amount):
        if not refundId or amount<=0:
            raise ValueError('INVALID_TOPUP_REFUND')
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self.repository._lockUser(cursor,userId)
                self.repository._canonical(cursor,userId)
                key='topup-refund:'+str(refundId)
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',(key,))
                previous=cursor.fetchone()
                if previous:
                    saved=self.repository._json(previous['metadata_json'])
                    if saved['userId']!=userId or saved['paymentId']!=paymentId or saved['amount']!=amount:
                        raise ValueError('TOPUP_REFUND_IDENTITY_CONFLICT')
                    return {'clawed':False,'tokens':0}
                cursor.execute('''select * from public."Invoices" where "userId"=%s and "razorpayPaymentId"=%s
                    and billing_reason='add_on' and status='PAID' for update''',(userId,paymentId))
                invoice=cursor.fetchone()
                if not invoice:
                    raise ValueError('OWNED_PAID_TOPUP_MISSING')
                tokens=int(self.repository._json(invoice['metadata_json']).get('tokens') or 0)
                captured=int(invoice.get('total_amount') or invoice.get('amount') or 0)
                if not tokens or not captured or amount>captured:
                    raise ValueError('INVALID_TOPUP_REFUND_AMOUNT')
                cursor.execute("select metadata_json from public.billing_events where user_id=%s and event_type='credit.topup_refunded'",(userId,))
                refunded=sum(int(self.repository._json(row['metadata_json'])['amount']) for row in cursor.fetchall()
                    if self.repository._json(row['metadata_json'])['paymentId']==paymentId)
                if refunded+amount>captured:
                    raise ValueError('TOPUP_REFUND_EXCEEDS_CAPTURE')
                # Difference of cumulative floors ensures multiple partial refunds add up exactly.
                entitled=(tokens*(refunded+amount)//captured)-(tokens*refunded//captured)
                cursor.execute('select topup_tokens from public.credit_balances where user_id=%s for update',(userId,))
                balance=cursor.fetchone()
                if not balance:
                    raise ValueError('CREDIT_BALANCE_MISSING')
                clawed=min(int(balance['topup_tokens'] or 0),entitled)
                cursor.execute('update public.credit_balances set topup_tokens=topup_tokens-%s,balance_version=balance_version+1,updated_at=%s where user_id=%s',
                    (clawed,datetime.now(timezone.utc),userId))
                metadata={'userId':userId,'paymentId':paymentId,'refundId':refundId,'amount':amount,
                    'tokensClawed':clawed,'unfundedTokens':entitled-clawed}
                cursor.execute('''insert into public.billing_events(id,user_id,invoice_id,event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                    values(%s,%s,%s,'audit','credit.topup_refunded','SETTLED',%s,%s,%s)''',
                    (str(uuid.uuid4()),userId,invoice['id'],key,Json(metadata),datetime.now(timezone.utc)))
                return {'clawed':True,'tokens':clawed}
        return self.repository._run(operation)

    def recoverUsage(self,limit=100):
        def load(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select id,metadata_json from public.billing_events where event_type='credit.usage_reported' and event_status='PENDING' and user_id is not null order by coalesce((metadata_json->>'lastRecoveryAt')::timestamptz,updated_at),id limit %s",(limit,))
                return list(cursor.fetchall())
        summary={'settled':0,'errors':0}
        for row in self.repository._run(load):
            data=self.repository._json(row['metadata_json'])
            context=self._context(data)
            try:
                self.settle(context,data['tokensUsed'],data['runId'])
                summary['settled']+=1
            except Exception:
                summary['errors']+=1
                self.repository.recordRecoveryCheck(str(row['id']))
        return summary
