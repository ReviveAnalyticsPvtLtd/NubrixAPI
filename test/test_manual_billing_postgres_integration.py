"""Actual PostgreSQL migration, rollback and concurrent capture tests.

Opt-in only: localhost database named manual_billing_test_*. This fixture
owns its public schema. It is a baseline fixture, not a live schema export.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch
import psycopg2
import pytest
from psycopg2.extras import Json
from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence, RefundQuote
from api.services.billing.manualBillingRepository import ManualBillingRepository

pytestmark = pytest.mark.skipif(os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") != "1", reason="Disposable PostgreSQL opt-in required; skipped is UNVERIFIED")
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)
BASELINE = '''
CREATE TABLE public."Users" ("userId" text primary key,"isBanned" boolean default false);
CREATE TABLE public.subscriptions (
 id uuid primary key default gen_random_uuid(),user_id text not null references "Users"("userId"),
 billing_mode text not null default 'none' check(billing_mode in ('none','monthly_recurring','annual_prepaid')),
 status text not null default 'none',plan_type text default 'none',
 current_period_start timestamptz,current_period_end timestamptz,renewal_due_at timestamptz,
 auto_renew_enabled boolean default false,payment_collection_mode text default 'authenticated_checkout',
 default_currency text default 'INR',version integer default 1,erasure_pending boolean default false,
 subscribed_experts jsonb default '[]',domain_count integer default 0 check(domain_count between 0 and 4),
 pending_removals jsonb default '[]',pending_additions jsonb default '[]',billing_state jsonb default '{}',
 cancellation_reason text,razorpay_customer_id text,razorpay_token_id text,subscription_anchor_day integer,
 recurring_failures integer default 0,created_at timestamptz default now(),updated_at timestamptz default now());
CREATE TABLE public."Invoices" (
 id uuid primary key default gen_random_uuid(),"userId" text references "Users"("userId"),
 subscription_id uuid references subscriptions(id),status text,billing_reason text,amount bigint,total_amount bigint,
 currency text,razorpay_order_id text,"razorpayPaymentId" text,"paidAt" timestamptz,
 period_start timestamptz,period_end timestamptz,due_date timestamptz,metadata_json jsonb default '{}',
 payment_flow text,requires_customer_auth boolean,amount_before_tax bigint,tax_amount bigint,
 tax_breakdown_json jsonb,tax_rule_version text,place_of_supply_snapshot text,
 pricing_version text,pricing_reference_snapshot_json jsonb);
CREATE TABLE public.billing_events (
 id uuid primary key default gen_random_uuid(),user_id text references "Users"("userId"),
 subscription_id uuid references subscriptions(id),invoice_id uuid references "Invoices"(id),
 event_category text not null check(event_category in ('audit','payment_attempt','notification','reconciliation','system')),
 event_type text not null,event_status text,payment_attempt_type text check(payment_attempt_type in ('token_debit','checkout','reconciliation_update')),
 payment_status text check(payment_status in ('created','precheck_failed','pending_provider_ack','authorized','captured','failed','cancelled','expired','investigated')),
 provider text default 'razorpay',provider_order_id text,provider_payment_id text unique,
 amount bigint,currency text,idempotency_key text unique,metadata_json jsonb default '{}',
 period_start timestamptz,period_end timestamptz,attempted_at timestamptz,completed_at timestamptz,
 occurred_at timestamptz default now(),created_at timestamptz default now(),updated_at timestamptz default now(),failure_reason text,
 CONSTRAINT billing_events_payment_attempt_required_fields check(event_category <> 'payment_attempt' or
 (payment_attempt_type is not null and payment_status is not null and attempted_at is not null)));
CREATE TABLE public.credit_balances (
 user_id text primary key references "Users"("userId"),subscription_id uuid references subscriptions(id),
 plan_tier text default 'none',domain_count integer default 0,monthly_token_quota bigint default 0,
 used_tokens bigint default 0,remaining_tokens bigint default 0,topup_tokens bigint default 0,
 period_start timestamptz,period_end timestamptz,last_reset_at timestamptz,updated_at timestamptz default now());
CREATE TABLE public."WebhookEvents" (id uuid primary key default gen_random_uuid(),status text);
'''

@pytest.fixture(scope="module")
def postgres():
    url = os.environ.get("MANUAL_BILLING_TEST_DATABASE_URL", "")
    parsed = urlparse(url)
    assert parsed.hostname in ("localhost","127.0.0.1"), "Only disposable localhost DB permitted"
    assert parsed.path.startswith("/manual_billing_test_"), "Disposable DB name required"
    assert url != os.environ.get("DATABASE_URL"), "Never use production DATABASE_URL"
    connection = psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("drop schema public cascade; create schema public")
            cursor.execute(BASELINE)
            cursor.execute("""DO $$ BEGIN CREATE ROLE anon; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                DO $$ BEGIN CREATE ROLE authenticated; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                DO $$ BEGIN CREATE ROLE service_role; EXCEPTION WHEN duplicate_object THEN NULL; END $$;""")
            for name in ('20260813112853_create_admin_auth.sql',
                         '20260823220448_create_admin_free_trial_extensions.sql',
                         '20260901194813_simplify_admin_trial_extensions.sql',
                         '20260911120000_create_admin_free_trial_reductions.sql'):
                cursor.execute((ROOT/'supabase/migrations'/name).read_text())
            from scripts.manual_billing_migration_plan import buildMigrationPlan
            for migration in buildMigrationPlan(set(),'expand'):
                cursor.execute(migration.read_text())
        connection.commit()
    finally: connection.close()
    return url


@pytest.mark.parametrize('request_key', [None, 'browser-key'])
def test_two_sessions_initial_reservation_creates_one_invoice_and_attempt(postgres, request_key):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    user = 'checkout-race-' + str(uuid.uuid4())
    with psycopg2.connect(postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute('insert into public."Users"("userId") values(%s)', (user,))
    repository = ManualBillingRepository(lambda: psycopg2.connect(postgres))
    repository.ensureCanonicalSubscription(user)
    request = CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, request_key)
    reference = {'amount': 10000, 'currency': 'INR', 'source': 'razorpay_plan_fetch'}
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', return_value=reference):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(repository.reserveCheckout, [request, request]))
    assert results[0].attemptId == results[1].attemptId
    assert results[0].expiresAt == results[1].expiresAt
    with psycopg2.connect(postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute('select count(*) from public."Invoices" where "userId"=%s', (user,))
            assert cursor.fetchone()[0] == 1
            cursor.execute("select count(*) from public.billing_events where user_id=%s and event_category='payment_attempt'", (user,))
            assert cursor.fetchone()[0] == 1

@pytest.fixture
def payment(postgres):
    repository=ManualBillingRepository(lambda: psycopg2.connect(postgres))
    user="integration-"+str(uuid.uuid4())
    subscription,invoice,lifecycle=map(str,(uuid.uuid4(),uuid.uuid4(),uuid.uuid4()))
    connection=psycopg2.connect(postgres)
    try:
        with connection.cursor() as cursor:
            cursor.execute('insert into public."Users"("userId") values(%s)',(user,))
            cursor.execute("insert into public.subscriptions(id,user_id,is_canonical,billing_state) values(%s,%s,true,%s)",
                (subscription,user,Json({'manualBilling':{'lifecycleId':lifecycle}})))
            cursor.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,metadata_json)
                values(%s,%s,%s,'PAYMENT_PENDING','initial_purchase',3000,'INR',%s)''',
                (invoice,user,subscription,Json({'manualBilling':{'lifecycleId':lifecycle,'billingMode':'monthly_prepaid'}})))
        connection.commit()
    finally: connection.close()
    with patch('api.services.billing.manualBillingRepository._now',return_value=NOW):
        attempt=repository.reserveCheckoutIntent(user,'initial_purchase',invoice,invoice,{
            'subscriptionId':subscription,'invoiceId':invoice,'lifecycleId':lifecycle,'billingMode':'monthly_prepaid',
            'amount':3000,'currency':'INR','domains':['banking'],'expiresAt':(NOW+timedelta(minutes=30)).isoformat()})
        order='order-'+str(uuid.uuid4())
        repository.bindProviderOrder(attempt.attemptId,{'id':order})
        evidence=VerifiedPaymentEvidence(attempt.attemptId,invoice,user,order,'pay-'+str(uuid.uuid4()),
            'initial_purchase','INR','captured','server_observation',3000,NOW,None,None,False)
        yield repository,evidence,postgres

def test_concurrent_duplicate_capture_grants_one_period(payment):
    repository,evidence,url=payment
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(repository.finalizeCapturedPayment,[evidence,evidence]))
    assert sorted(result.state for result in results)==['activated','already_finalized']
    assert sum(result.creditsRefilled for result in results)==1
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select count(*) from public.billing_events where provider_payment_id=%s',(evidence.providerPaymentId,))
            assert cursor.fetchone()[0]==1
            cursor.execute('select current_period_start,current_period_end from public.subscriptions where user_id=%s',(evidence.userId,))
            start,end=cursor.fetchone()
            cursor.execute('select period_start,period_end,balance_version from public.credit_balances where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()==(start,end,1)
    finally: connection.close()

def test_failure_before_commit_rolls_back_paid_invoice_and_quota(payment):
    repository,evidence,url=payment
    with patch.object(repository,'_recordNotification',side_effect=RuntimeError('injected intent failure')):
        with pytest.raises(RuntimeError): repository.finalizeCapturedPayment(evidence)
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select status from public."Invoices" where id=%s',(evidence.invoiceId,))
            assert cursor.fetchone()[0]=='PAYMENT_PENDING'
            cursor.execute('select count(*) from public.credit_balances where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()[0]==0
    finally: connection.close()
    assert repository.finalizeCapturedPayment(evidence).creditsRefilled

@pytest.mark.parametrize('blocker',['token','recurring_mode','credit_identity'])
def test_contract_refuses_unretired_or_unmapped_state(postgres,blocker):
    connection=psycopg2.connect(postgres)
    try:
        with connection.cursor() as cursor:
            user='contract-blocker-'+str(uuid.uuid4())
            cursor.execute('insert into public."Users"("userId") values(%s)',(user,))
            cursor.execute('''insert into public.subscriptions(user_id,is_canonical,billing_mode,status,current_period_start,current_period_end,razorpay_token_id)
                values(%s,true,%s,'active',now(),now()+interval '1 month',%s)''',
                (user,'monthly_recurring' if blocker=='recurring_mode' else 'monthly_prepaid' if blocker=='credit_identity' else 'none',
                 'test-only-mandate' if blocker=='token' else None))
            with pytest.raises(psycopg2.Error,match='CONTRACT_PRECONDITION_FAILED'):
                cursor.execute((ROOT/'supabase/migrations/20261005195626_contract_recurring_billing_fields.sql').read_text())
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from information_schema.columns where table_schema='public' and table_name='subscriptions' and column_name='razorpay_token_id'")
            assert cursor.fetchone()[0]==1
    finally: connection.rollback(); connection.close()


def test_contract_migration_executes_after_retirement_preconditions(postgres):
    connection=psycopg2.connect(postgres)
    try:
        with connection.cursor() as cursor:
            cursor.execute((ROOT/'supabase/migrations/20261005195626_contract_recurring_billing_fields.sql').read_text())
            cursor.execute("select column_name from information_schema.columns where table_schema='public' and table_name='subscriptions'")
            assert not {'razorpay_customer_id','razorpay_token_id','subscription_anchor_day','recurring_failures'} & {row[0] for row in cursor.fetchall()}
        connection.commit()
    finally: connection.close()


def test_topup_race_after_expiry_stores_tokens_without_refill(payment):
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    invoice,order,paymentId=map(str,(uuid.uuid4(),uuid.uuid4(),uuid.uuid4()))
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,metadata_json)
                values(%s,%s,%s,'PAYMENT_PENDING','add_on',150,'INR',%s)''',
                (invoice,evidence.userId,period.subscriptionId,Json({'tokens':500})))
        connection.commit()
    finally: connection.close()
    with patch('api.services.billing.manualBillingRepository._now',return_value=period.end-timedelta(minutes=10)):
        attempt=repository.reserveCheckoutIntent(evidence.userId,'topup',invoice,invoice,{'subscriptionId':period.subscriptionId,
            'invoiceId':invoice,'lifecycleId':period.lifecycleId,'billingMode':'monthly_prepaid','tokens':500,
            'amount':150,'currency':'INR','expiresAt':(period.end+timedelta(minutes=20)).isoformat()})
        repository.bindProviderOrder(attempt.attemptId,{'id':order})
    late=period.end+timedelta(minutes=10)
    repository.activateDueCoverage(evidence.userId,late)
    capture=VerifiedPaymentEvidence(attempt.attemptId,invoice,evidence.userId,order,paymentId,'topup','INR','captured',
        'attested_capture',150,late,NOW+timedelta(minutes=1),None,True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(repository.finalizeCapturedPayment,[capture,capture]))
    assert sorted(result.state for result in results)==['already_finalized','topup_granted']
    assert not any(result.creditsRefilled for result in results)
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select remaining_tokens,monthly_token_quota,topup_tokens from public.credit_balances where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()==(0,0,500)
            cursor.execute('select status from public.subscriptions where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()[0]=='expired'
    finally: connection.close()


def _renewal(repository,evidence,url):
    from dateutil.relativedelta import relativedelta
    first=repository.finalizeCapturedPayment(evidence)
    start=first.currentPeriod.end
    end=start+relativedelta(months=1)
    invoice=str(uuid.uuid4())
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json)
                values(%s,%s,%s,'PAYMENT_PENDING','renewal',3000,'INR',%s,%s,%s)''',
                (invoice,evidence.userId,first.currentPeriod.subscriptionId,start,end,
                    Json({'manualBilling':{'lifecycleId':first.currentPeriod.lifecycleId,'billingMode':'monthly_prepaid'}})))
        connection.commit()
    finally: connection.close()
    attempt=repository.reserveCheckoutIntent(evidence.userId,'renewal',invoice,invoice,{
        'subscriptionId':first.currentPeriod.subscriptionId,'invoiceId':invoice,'lifecycleId':first.currentPeriod.lifecycleId,
        'billingMode':'monthly_prepaid','amount':3000,'currency':'INR','domains':['banking'],
        'expiresAt':start.isoformat()})
    order='order-'+str(uuid.uuid4())
    repository.bindProviderOrder(attempt.attemptId,{'id':order})
    paid=VerifiedPaymentEvidence(attempt.attemptId,invoice,evidence.userId,order,'pay-'+str(uuid.uuid4()),
        'renewal','INR','captured','server_observation',3000,NOW,None,None,False)
    assert repository.finalizeCapturedPayment(paid).state=='paid_scheduled'
    return start,end


def test_boundary_race_refills_once_and_delayed_usage_keeps_new_quota(payment):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,evidence,url=payment
    start,end=_renewal(repository,evidence,url)
    credit=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credit.admit(evidence.userId,'report','workflow')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:repository.activateDueCoverage(evidence.userId,start),range(2)))
    assert sum(result.creditsRefilled for result in results)==1
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=start+timedelta(seconds=1)
        outcomes=[credit.settle(context,100,'llm-run'),credit.settle(context,100,'llm-run')]
    assert all(outcome['historicalPeriod'] for outcome in outcomes)
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select remaining_tokens,monthly_token_quota,used_tokens,period_start,period_end from public.credit_balances where user_id=%s',(evidence.userId,))
            remaining,quota,used,actualStart,actualEnd=cursor.fetchone()
            assert remaining==quota and used==0 and (actualStart,actualEnd)==(start,end)
            cursor.execute("select count(*) from public.billing_events where user_id=%s and event_type='credit.operation_settled'",(evidence.userId,))
            assert cursor.fetchone()[0]==1
    finally: connection.close()


def test_refund_race_reserves_once_expires_access_and_preserves_topups(payment):
    repository,evidence,url=payment
    first=repository.finalizeCapturedPayment(evidence)
    cutoff=NOW+timedelta(days=10)
    amount=3000*((first.currentPeriod.end-cutoff)//timedelta(microseconds=1))//((first.currentPeriod.end-NOW)//timedelta(microseconds=1))
    quote=RefundQuote('quote-'+str(uuid.uuid4()),evidence.userId,'email-case','INR',cutoff,
        cutoff+timedelta(minutes=5),amount,({'invoiceId':evidence.invoiceId},),True,False)
    repository.saveRefundQuote('staff',quote,'Approved unused time')
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('update public.credit_balances set topup_tokens=1234 where user_id=%s',(evidence.userId,))
        connection.commit()
    finally: connection.close()
    def reserve(key):
        try: return repository.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',amount,key)
        except ValueError: return None
    with patch('api.services.billing.manualBillingRepository._now',return_value=cutoff):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve,['request-1','request-2']))
    assert sum(result is not None for result in results)==1
    intent=next(result for result in results if result)
    assert repository.claimRefundSubmission(intent.refundIntentId,evidence.providerPaymentId)
    assert not repository.claimRefundSubmission(intent.refundIntentId,evidence.providerPaymentId)
    processed=repository.settleRefundEvidence(intent.refundIntentId,{'refunds':[{'id':'refund-'+str(uuid.uuid4()),
        'payment_id':evidence.providerPaymentId,'amount':amount,'status':'processed'}]})
    assert processed['refundState']=='processed' and not processed['accessRestored']
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select status,current_period_end from public.subscriptions where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()==('expired',cutoff)
            cursor.execute('select remaining_tokens,topup_tokens from public.credit_balances where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()==(0,1234)
    finally: connection.close()


def test_sql_notification_bridge_recovers_commit_without_duplicate_delivery(payment):
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    repository.finalizeCapturedPayment(evidence)
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    with patch('api.services.notifications.notificationDeliveryRepository.getNotificationDeliveryRepository',return_value=deliveries):
        repository.bridgeNotificationIntents()
        repository.bridgeNotificationIntents()
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select count(*) from public.notification_deliveries where user_id=%s and notification_type=\'payment_receipt\'',(evidence.userId,))
            assert cursor.fetchone()[0]==1
    finally: connection.close()


def test_reviewed_backfill_preserves_consumption_and_maps_current_identity(payment,tmp_path,monkeypatch):
    import json
    from scripts.manual_billing_backfill import applyMapping
    repository,evidence,url=payment
    first=repository.finalizeCapturedPayment(evidence)
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('update public.subscriptions set billing_state=\'{}\'::jsonb where user_id=%s',(evidence.userId,))
            cursor.execute('update public."Invoices" set metadata_json=\'{}\'::jsonb where id=%s',(evidence.invoiceId,))
            cursor.execute('update public.credit_balances set lifecycle_id=null,credit_period_id=null,used_tokens=200,remaining_tokens=monthly_token_quota-200,topup_tokens=1234 where user_id=%s',(evidence.userId,))
            cursor.execute('select version from public.subscriptions where user_id=%s',(evidence.userId,))
            version=cursor.fetchone()[0]
        connection.commit()
    finally: connection.close()
    lifecycle,period=map(str,(uuid.uuid4(),uuid.uuid4()))
    mapping=tmp_path/'operator-mapping.json'
    mapping.write_text(json.dumps({'reviewed':True,'approved_by':'disposable-test-operator','mappings':[{
        'user_id':evidence.userId,'promote_id':first.currentPeriod.subscriptionId,'expected_version':version,
        'current_invoice_id':evidence.invoiceId,'lifecycle_id':lifecycle,'credit_period_id':period}]}))
    monkeypatch.setenv('DATABASE_URL',url)
    assert applyMapping(str(mapping))['promoted']==1
    assert repository.getCoverageSnapshot(evidence.userId).accessAllowed
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select used_tokens,remaining_tokens,monthly_token_quota,topup_tokens,lifecycle_id,credit_period_id from public.credit_balances where user_id=%s',(evidence.userId,))
            used,remaining,quota,topups,actualLife,actualPeriod=cursor.fetchone()
            assert used==200 and remaining==quota-200 and topups==1234
            assert str(actualLife)==lifecycle and str(actualPeriod)==period
    finally: connection.close()


@pytest.mark.parametrize('existing_balance',[False,True])
def test_trial_staff_refill_uses_canonical_lifecycle_and_fences_old_usage(postgres,existing_balance):
    from api.services.adminTrialExtensionRepository import AdminTrialExtensionRepository
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    user='trial-staff-'+str(uuid.uuid4())
    admin=str(uuid.uuid4())
    repository=ManualBillingRepository(lambda:psycopg2.connect(postgres))
    with psycopg2.connect(postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute('insert into public."Users"("userId") values(%s)',(user,))
            cursor.execute('insert into public.admin_users(id,email,name,password_hash) values(%s,%s,\'Staff\',\'test-hash\')',(admin,admin+'@example.test'))
    repository.ensureCanonicalSubscription(user)
    trial=repository.activateTrial(user,('banking',))
    credits=ManualCreditRepository(repository)
    old=None
    if existing_balance:
        old=credits.admit(user,'reporting_query','before-staff-refill')
        with psycopg2.connect(postgres) as connection:
            with connection.cursor() as cursor:
                cursor.execute('update public.credit_balances set topup_tokens=123 where user_id=%s',(user,))
    extensions=AdminTrialExtensionRepository(lambda:psycopg2.connect(postgres),freeQuotaProvider=lambda:12345)
    operation=extensions.createOrGetExtension(str(uuid.uuid4()),'a'*64,user,3,'Approved trial extension',admin)
    result=extensions.extendUser(str(operation['id']),user,3,NOW+timedelta(minutes=1))
    assert result['outcome']=='EXTENDED'
    fresh=credits.balanceSnapshot(user)
    assert fresh['remaining_tokens']==12345 and fresh['topup_tokens']==(123 if existing_balance else 0)
    again=extensions.extendUser(str(operation['id']),user,3,NOW+timedelta(minutes=1))
    assert again['outcome']=='EXTENDED'
    assert credits.balanceSnapshot(user)['credit_period_id']==fresh['credit_period_id']
    if old:
        assert fresh['credit_period_id']!=old.creditPeriodId
        assert credits.settle(old,100,'old-run')['historicalPeriod']
        assert credits.balanceSnapshot(user)['remaining_tokens']==12345


def test_bridge_replay_keeps_claimed_payload_version_unchanged(payment):
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    args=(evidence.userId,period.subscriptionId,'payment_receipt','revision-test:'+evidence.providerPaymentId,
          period.end.isoformat(),{'invoiceId':evidence.invoiceId,'amount':3000})
    first,_=deliveries.enqueueBillingNotification(*args)
    deliveries.claimDue('worker-repeat',limit=500)
    repeated,_=deliveries.enqueueBillingNotification(*args)
    assert repeated['payload_version']==first['payload_version'] and repeated['status']=='SENDING'


def test_concurrent_extra_money_is_applied_or_audited_once(payment):
    from dataclasses import replace
    repository,evidence,url=payment
    second=replace(evidence,providerPaymentId='extra-'+str(uuid.uuid4()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(repository.finalizeCapturedPayment,[evidence,second]))
    assert sum(result.finalized for result in results)==1
    assert sum(result.state=='requires_reconciliation' for result in results)==1
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select event_status,sum(amount) from public.billing_events where invoice_id=%s and event_type='payment.capture' group by event_status",(evidence.invoiceId,))
            totals=dict(cursor.fetchall())
            assert totals=={'FINALIZED':3000,'REQUIRES_RECONCILIATION':3000}
            cursor.execute('select balance_version from public.credit_balances where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()[0]==1


def test_concurrent_revision_and_submission_keep_one_provider_identity(payment):
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    args=(evidence.userId,period.subscriptionId,'payment_receipt','race-revision:'+evidence.providerPaymentId,
          period.end.isoformat())
    row,_=deliveries.enqueueBillingNotification(*args,{'invoiceId':evidence.invoiceId,'amount':3000})
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("update public.notification_deliveries set status='SENDING',lease_owner='worker-race',claimed_payload_version=payload_version where id=%s",(row['id'],))
    with ThreadPoolExecutor(max_workers=2) as pool:
        send=pool.submit(deliveries.authorizeBillingSubmission,str(row['id']),'worker-race',1)
        revise=pool.submit(deliveries.enqueueBillingNotification,*args,{'invoiceId':evidence.invoiceId,'amount':3000,'finalPaidEnd':period.end.isoformat()})
        submitted=send.result(); revised=revise.result()[0]
    if submitted:
        assert revised['payload_version']==1
        assert deliveries.markAccepted(str(row['id']),'worker-race','provider-message',NOW.isoformat(),payloadVersion=1)
    else:
        assert revised['payload_version']==2
        assert not deliveries.markAccepted(str(row['id']),'worker-race','provider-message',NOW.isoformat(),payloadVersion=1)
    assert not deliveries.authorizeBillingSubmission(str(row['id']),'worker-race',1)


def test_expired_dispatch_lease_cannot_start_provider_submission(payment):
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    row,_=deliveries.enqueueBillingNotification(evidence.userId,period.subscriptionId,'payment_receipt',
        'expired-lease:'+evidence.providerPaymentId,period.end.isoformat(),{'invoiceId':evidence.invoiceId})
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("update public.notification_deliveries set status='SENDING',lease_owner='old-worker',claimed_payload_version=payload_version,lease_expires_at=now()-interval '1 second' where id=%s",(row['id'],))
    assert not deliveries.authorizeBillingSubmission(str(row['id']),'old-worker',1)


def _pendingRenewal(payment):
    from dateutil.relativedelta import relativedelta
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    invoice=str(uuid.uuid4())
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json)
                values(%s,%s,%s,'PAYMENT_PENDING','renewal',3000,'INR',%s,%s,%s)''',
                (invoice,evidence.userId,period.subscriptionId,period.end,period.end+relativedelta(months=1),
                 Json({'manualBilling':{'lifecycleId':period.lifecycleId,'billingMode':'monthly_prepaid'}})))
    attempt=repository.reserveCheckoutIntent(evidence.userId,'renewal',invoice,invoice,{
        'subscriptionId':period.subscriptionId,'invoiceId':invoice,'lifecycleId':period.lifecycleId,
        'billingMode':'monthly_prepaid','amount':3000,'currency':'INR','domains':['banking'],
        'expiresAt':(NOW+timedelta(minutes=30)).isoformat()})
    order='order-'+str(uuid.uuid4())
    repository.bindProviderOrder(attempt.attemptId,{'id':order})
    capture=VerifiedPaymentEvidence(attempt.attemptId,invoice,evidence.userId,order,'pay-'+str(uuid.uuid4()),
        'renewal','INR','captured','attested_capture',3000,NOW+timedelta(minutes=2),NOW+timedelta(minutes=1),None,True)
    return period,capture


def test_optout_races_proven_earlier_capture_and_preserves_both_paid_months(payment):
    repository,evidence,url=payment
    period,capture=_pendingRenewal(payment)
    with patch('api.services.billing.manualBillingRepository._now',return_value=NOW+timedelta(minutes=2)):
        with ThreadPoolExecutor(max_workers=2) as pool:
            paid=pool.submit(repository.finalizeCapturedPayment,capture)
            cancelled=pool.submit(repository.setRenewalOptOut,evidence.userId,True,'Finished project','race-optout')
            assert paid.result().state=='paid_scheduled'
            cancelled.result()
    snapshot=repository.getCoverageSnapshot(evidence.userId,NOW+timedelta(minutes=3))
    assert snapshot.currentPeriod.end==period.end and snapshot.nextPeriod.start==period.end
    assert snapshot.finalPaidEnd==snapshot.nextPeriod.end
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute('select renewal_opt_out from public.subscriptions where user_id=%s',(evidence.userId,))
            assert cursor.fetchone()[0]


def test_refund_races_future_capture_without_reopening_closed_coverage(payment):
    repository,evidence,url=payment
    period,capture=_pendingRenewal(payment)
    cutoff=NOW+timedelta(days=10)
    amount=3000*((period.end-cutoff)//timedelta(microseconds=1))//((period.end-period.start)//timedelta(microseconds=1))
    quote=RefundQuote('quote-'+str(uuid.uuid4()),evidence.userId,'email-case','INR',cutoff,
        cutoff+timedelta(minutes=5),amount,({'invoiceId':evidence.invoiceId},),True,False)
    repository.saveRefundQuote('staff',quote,'Unused service')
    from dataclasses import replace
    capture=replace(capture,observedAt=cutoff)
    def reserve():
        try: return repository.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Unused service',amount,'refund-capture-race')
        except ValueError as error:
            assert str(error)=='CURRENT_TERMINATION_REQUIRES_FUTURE_SETTLEMENT'
            return None
    with patch('api.services.billing.manualBillingRepository._now',return_value=cutoff):
        with ThreadPoolExecutor(max_workers=2) as pool:
            refund=pool.submit(reserve); paid=pool.submit(repository.finalizeCapturedPayment,capture)
            reserved=refund.result(); result=paid.result()
    snapshot=repository.getCoverageSnapshot(evidence.userId,cutoff)
    if reserved:
        assert not snapshot.accessAllowed and snapshot.nextPeriod is None
        assert result.state=='requires_reconciliation'
    else:
        assert snapshot.accessAllowed and snapshot.nextPeriod is not None
        assert result.state=='paid_scheduled'


def test_settlement_races_staff_reset_without_debiting_the_new_allocation(payment):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,evidence,url=payment
    repository.finalizeCapturedPayment(evidence)
    credits=ManualCreditRepository(repository)
    context=credits.admit(evidence.userId,'reporting_query','reset-race')
    with ThreadPoolExecutor(max_workers=2) as pool:
        old=pool.submit(credits.settle,context,100,'old-run')
        reset=pool.submit(credits.resizeQuota,evidence.userId,1,False,True)
        old.result(); assert reset.result()['applied']
    balance=credits.balanceSnapshot(evidence.userId)
    assert balance['credit_period_id']!=context.creditPeriodId
    assert balance['used_tokens']==0 and balance['remaining_tokens']==balance['monthly_token_quota']


def test_topup_clawback_races_usage_without_losing_the_unfunded_obligation(payment):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    credits=ManualCreditRepository(repository)
    context=credits.admit(evidence.userId,'reporting_query','clawback-race')
    paymentId='topup-pay-'+str(uuid.uuid4())
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,"razorpayPaymentId",metadata_json)
                values(%s,%s,%s,'PAID','add_on',150,'INR',%s,%s)''',
                (str(uuid.uuid4()),evidence.userId,period.subscriptionId,paymentId,Json({'tokens':500})))
            cursor.execute('update public.credit_balances set remaining_tokens=0,used_tokens=monthly_token_quota,topup_tokens=500 where user_id=%s',(evidence.userId,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        usage=pool.submit(credits.settle,context,100,'usage-run')
        refund=pool.submit(credits.clawbackTopup,evidence.userId,'refund-'+paymentId,paymentId,150)
        usage.result(); refund.result()
    assert credits.balanceSnapshot(evidence.userId)['topup_tokens']==0
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select sum((metadata_json->>'unfundedTokens')::bigint) from public.billing_events where user_id=%s and event_type in ('credit.operation_settled','credit.topup_refunded')",(evidence.userId,))
            assert cursor.fetchone()[0]==100


def test_distinct_expert_captures_respect_the_catalogue_limit(payment):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repository,evidence,url=payment
    repository.finalizeCapturedPayment(evidence)
    reference={'amount':10000,'currency':'INR','source':'razorpay_plan_fetch'}
    def reserve(domain):
        return repository.reserveCheckout(CheckoutRequest(evidence.userId,'expert_addition','monthly_prepaid',{'domains':[domain]},'add-'+domain))
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=reference):
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts=list(pool.map(reserve,['telecom','manufacturing','supplychain']))
    captures=[]
    for attempt in attempts:
        order='expert-order-'+str(uuid.uuid4())
        repository.bindProviderOrder(attempt.attemptId,{'id':order})
        captures.append(VerifiedPaymentEvidence(attempt.attemptId,attempt.invoiceId,evidence.userId,
            order,'expert-pay-'+str(uuid.uuid4()),'expert_addition','INR','captured',
            'server_observation',attempt.amount,NOW+timedelta(minutes=1),None,None,False))
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(repository.finalizeCapturedPayment,captures))
    assert all(result.finalized for result in results)
    assert set(repository.getCoverageSnapshot(evidence.userId).currentPeriod.domains)=={'banking','telecom','manufacturing','supplychain'}
    with pytest.raises(ValueError,match='EXPERT_SELECTION_CONFLICT'):
        with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=reference):
            repository.reserveCheckout(CheckoutRequest(evidence.userId,'expert_addition','monthly_prepaid',{'domains':['telecom']},'duplicate-new-key'))


def test_erasure_races_capture_and_dispatch_without_new_access_or_repeat_send(payment):
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    row,_=deliveries.enqueueBillingNotification(evidence.userId,period.subscriptionId,'payment_receipt',
        'erasure-race:'+evidence.providerPaymentId,period.end.isoformat(),{'invoiceId':evidence.invoiceId})
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("update public.notification_deliveries set status='SENDING',lease_owner='race-worker',claimed_payload_version=payload_version,lease_expires_at=now()+interval '5 minutes' where id=%s",(row['id'],))
    def erase():
        def operation(connection):
            with connection.cursor() as cursor:
                repository._lockUser(cursor,evidence.userId)
                cursor.execute('update public.subscriptions set erasure_pending=true where user_id=%s and is_canonical=true',(evidence.userId,))
        repository._run(operation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent=pool.submit(deliveries.authorizeBillingSubmission,str(row['id']),'race-worker',1)
        erased=pool.submit(erase)
        sent.result(); erased.result()
    assert not repository.getCoverageSnapshot(evidence.userId).accessAllowed
    assert not deliveries.authorizeBillingSubmission(str(row['id']),'race-worker',1)
    assert repository.finalizeCapturedPayment(evidence).state=='already_finalized'


def test_inventory_cli_is_read_only_and_redacted_after_contraction(postgres,monkeypatch,capsys):
    from scripts.manual_billing_inventory import main
    owner='sensitive-inventory-owner-'+str(uuid.uuid4())
    with psycopg2.connect(postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute('insert into public."Users"("userId") values(%s)',(owner,))
            cursor.execute("insert into public.subscriptions(user_id,is_canonical) values(%s,true),(%s,false)",(owner,owner))
    monkeypatch.setenv('DATABASE_URL',postgres)
    assert main(['--json'])==0
    output=capsys.readouterr().out
    assert owner not in output
    with psycopg2.connect(postgres) as connection:
        with connection.cursor() as cursor:
            cursor.execute('select count(*) from public.subscriptions where user_id=%s',(owner,))
            assert cursor.fetchone()[0]==2


@pytest.mark.parametrize('operation',['coverage','admission'])
def test_lock_wait_past_expiry_uses_wall_clock(payment,operation,monkeypatch):
    import threading,time
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,evidence,url=payment
    repository.finalizeCapturedPayment(evidence)
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("update public.\"Invoices\" set period_end=clock_timestamp()+interval '1 second' where id=%s",(evidence.invoiceId,))
            cursor.execute("update public.subscriptions set current_period_end=clock_timestamp()+interval '1 second' where user_id=%s",(evidence.userId,))
    blocker=repository.connectionFactory()
    with blocker.cursor() as cursor: repository._lockUser(cursor,evidence.userId)
    # Hold the owner lock inside the exact method being checked. Pre-activation
    # is unnecessary for this current period and would mask admit's stale clock.
    monkeypatch.setattr(repository,'activateDueCoverage',lambda *args:None)
    started=threading.Event()
    originalLock=repository._lockUser
    def observedLock(cursor,userId):
        started.set();originalLock(cursor,userId)
    monkeypatch.setattr(repository,'_lockUser',observedLock)
    def execute():
        if operation=='coverage': return repository.getCoverageSnapshot(evidence.userId)
        try: return ManualCreditRepository(repository).admit(evidence.userId,'reporting_query','after-lock')
        except ValueError as exc: return str(exc)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(execute)
            assert started.wait(5)
            time.sleep(1.2)
            blocker.commit()
            observed=result.result(timeout=5)
        if operation=='coverage': assert not observed.accessAllowed
        else: assert observed=='CREDIT_ADMISSION_REQUIRES_PAID_COVERAGE'
    finally: blocker.close()



def test_submission_lock_wait_past_lease_expiry_is_rejected(payment,monkeypatch):
    import threading,time
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    deliveries=NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    row,_=deliveries.enqueueBillingNotification(evidence.userId,period.subscriptionId,'payment_receipt',
        'waiting-lease:'+evidence.providerPaymentId,period.end.isoformat(),{'invoiceId':evidence.invoiceId})
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("update public.notification_deliveries set status='SENDING',lease_owner='waiting-worker',claimed_payload_version=payload_version,lease_expires_at=clock_timestamp()+interval '1 second' where id=%s",(row['id'],))
    blocker=repository.connectionFactory()
    with blocker.cursor() as cursor: repository._lockUser(cursor,evidence.userId)
    started=threading.Event()
    originalLock=ManualBillingRepository._lockUser
    def observedLock(self,cursor,userId):
        started.set();originalLock(self,cursor,userId)
    monkeypatch.setattr(ManualBillingRepository,'_lockUser',observedLock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result=pool.submit(deliveries.authorizeBillingSubmission,str(row['id']),'waiting-worker',1)
            assert started.wait(5)
            time.sleep(1.2);blocker.commit()
            assert not result.result(timeout=5)
        with psycopg2.connect(url) as connection:
            with connection.cursor() as cursor:
                cursor.execute('select submission_started_at from public.notification_deliveries where id=%s',(row['id'],))
                assert cursor.fetchone()[0] is None
    finally: blocker.close()
