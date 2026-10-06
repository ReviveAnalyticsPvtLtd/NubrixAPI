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
NOW = datetime(2026,10,6,12,tzinfo=timezone.utc)
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
 period_start timestamptz,period_end timestamptz,metadata_json jsonb default '{}',
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
            for name in ("20261005195608_expand_manual_monthly_billing.sql","20261005195617_add_manual_billing_transactions.sql",
                         "20261006131717_enforce_manual_checkout_order_identity.sql"):
                cursor.execute((ROOT / "supabase/migrations" / name).read_text())
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
        'server_observation',150,late,None,None,False)
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
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""DO $$ BEGIN CREATE ROLE anon; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                DO $$ BEGIN CREATE ROLE authenticated; EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                DO $$ BEGIN CREATE ROLE service_role; EXCEPTION WHEN duplicate_object THEN NULL; END $$;""")
            cursor.execute((ROOT/'supabase/migrations/20260917170828_create_notification_deliveries.sql').read_text())
            cursor.execute((ROOT/'supabase/migrations/20260918100000_harden_notification_claims.sql').read_text())
            cursor.execute((ROOT/'supabase/migrations/20261005195635_extend_monthly_notifications.sql').read_text())
        connection.commit()
    finally: connection.close()
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
    connection=psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute('select used_tokens,remaining_tokens,monthly_token_quota,topup_tokens,lifecycle_id,credit_period_id from public.credit_balances where user_id=%s',(evidence.userId,))
            used,remaining,quota,topups,actualLife,actualPeriod=cursor.fetchone()
            assert used==200 and remaining==quota-200 and topups==1234
            assert str(actualLife)==lifecycle and str(actualPeriod)==period
    finally: connection.close()
