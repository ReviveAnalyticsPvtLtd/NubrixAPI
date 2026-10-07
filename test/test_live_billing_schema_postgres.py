"""Financial regressions against the actual pre-expansion PG17 schema shape.

Fixture definitions contain no customer data. Only an explicitly opted-in,
task-owned localhost manual_billing_test_* database may be recreated.
"""
import os
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from api.services.billing.manualBillingRepository import ManualBillingRepository
from scripts.manual_billing_migration_plan import buildMigrationPlan

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get('RUN_LIVE_SCHEMA_INTEGRATION') != '1',
    reason='Actual-schema disposable PostgreSQL opt-in required; skipped is UNVERIFIED',
)
TABLES = ('credit_balances', 'subscriptions', 'Invoices', 'billing_events', 'admin_audit_log',
    'admin_credit_reset_operations', 'admin_credit_reset_targets')
RPCS = (
    ('decrement_topup_tokens', ('no-such-test-user', 1)),
    ('grant_topup_tokens', ('no-such-test-order', 'no-such-test-payment')),
    ('clawback_topup_tokens', ('no-such-test-refund', 'no-such-test-payment', 1)),
)


@pytest.fixture(scope='module')
def actualSchema():
    url = os.environ.get('LIVE_SCHEMA_TEST_DATABASE_URL', '')
    parsed = urlparse(url)
    assert parsed.hostname in ('127.0.0.1', 'localhost')
    assert parsed.path.startswith('/manual_billing_test_')
    assert url != os.environ.get('DATABASE_URL')
    with closing(psycopg2.connect(url)) as connection, connection:
        with connection.cursor() as cursor:
            cursor.execute('drop schema public cascade; create schema public')
            for role in ('anon', 'authenticated', 'service_role', 'supabase_admin'):
                cursor.execute('select 1 from pg_roles where rolname=%s', (role,))
                if cursor.fetchone() is None:
                    cursor.execute(sql.SQL('create role {} nologin {}').format(
                        sql.Identifier(role), sql.SQL('bypassrls' if role == 'service_role' else 'nobypassrls')))
            cursor.execute((ROOT/'test/fixtures/pre_manual_billing_financial.sql').read_text(encoding='utf-8'))
            # The starting CHECK and ordinary roles are the important live differences.
            cursor.execute("select pg_get_constraintdef(oid) from pg_constraint where conname='credit_balances_domain_count_check'")
            assert '>= 1' in cursor.fetchone()[0]
            cursor.execute("select bool_or(rolsuper or rolbypassrls) from pg_roles where rolname in ('anon','authenticated')")
            assert cursor.fetchone()[0] is False
            for migration in buildMigrationPlan(set(), 'expand'):
                # The catalog fixture already contains this non-idempotent table,
                # indexes and claim function. This is an effect-based baseline,
                # not a claim about absent production migration-history records.
                if migration.name == '20260917170828_create_notification_deliveries.sql':
                    continue
                cursor.execute(migration.read_text(encoding='utf-8'))
    return url


def query(url, statement, params=()):
    with closing(psycopg2.connect(url)) as connection, connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, params)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []


def asRole(url, role, statement, params=()):
    connection = psycopg2.connect(url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL('set local role {}').format(sql.Identifier(role)))
            cursor.execute(statement, params)
            return cursor.fetchall() if cursor.description else None
    finally:
        # Even an incorrectly permitted destructive action cannot survive a test.
        connection.rollback()
        connection.close()


def seedPaid(url, *, ended=False):
    user = 'schema-test-' + uuid.uuid4().hex
    subscription, invoice, lifecycle, period = [str(uuid.uuid4()) for _ in range(4)]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=32 if ended else 1)
    end = now - timedelta(days=1) if ended else now + timedelta(days=29)
    metadata = {'manualBilling': {'lifecycleId': lifecycle, 'creditPeriodId': period,
        'billingMode': 'monthly_prepaid', 'purpose': 'initial_purchase',
        'domains': ['banking'], 'coverageState': 'active'}}
    query(url, 'insert into public."Users"("userId",email) values(%s,%s)', (user, user+'@example.invalid'))
    query(url, '''insert into public.subscriptions(id,user_id,is_canonical,billing_mode,status,
        plan_type,current_period_start,current_period_end,subscribed_experts,domain_count,billing_state)
        values(%s,%s,true,'monthly_prepaid','active','pro',%s,%s,%s,1,%s)''',
        (subscription,user,start,end,Json(['banking']),Json({'manualBilling':{'lifecycleId':lifecycle,'paidFutureEnd':end.isoformat()}})))
    query(url, '''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,
        "razorpayPaymentId",total_amount,period_start,period_end,metadata_json)
        values(%s,%s,%s,'PAID','initial_purchase',%s,3000,%s,%s,%s)''',
        (invoice,user,subscription,'test-capture-'+uuid.uuid4().hex,start,end,Json(metadata)))
    query(url, '''insert into public.credit_balances(user_id,subscription_id,plan_tier,domain_count,
        lifecycle_id,credit_period_id,monthly_token_quota,used_tokens,remaining_tokens,topup_tokens,
        period_start,period_end) values(%s,%s,'pro',1,%s,%s,10000,7000,3000,2500,%s,%s)''',
        (user,subscription,lifecycle,period,start,end))
    return user, subscription, now


def test_runtime_expiry_accepts_inactive_zero_domains_without_losing_usage_or_topups(actualSchema):
    user, subscription, now = seedPaid(actualSchema, ended=True)
    repository = ManualBillingRepository(lambda: psycopg2.connect(actualSchema))
    repository.activateDueCoverage(user, now)
    [balance] = query(actualSchema, 'select * from public.credit_balances where user_id=%s', (user,))
    [row] = query(actualSchema, 'select status,domain_count,subscribed_experts from public.subscriptions where id=%s', (subscription,))
    assert (row['status'], row['domain_count'], row['subscribed_experts']) == ('expired', 0, [])
    assert (balance['plan_tier'],balance['domain_count'],balance['monthly_token_quota'],balance['remaining_tokens']) == ('none',0,0,0)
    assert (balance['used_tokens'],balance['topup_tokens']) == (7000,2500)
    assert repository.getCoverageSnapshot(user, now).currentPeriod is None


@pytest.mark.parametrize('count', [-1, 5])
def test_migrated_domain_bound_rejects_invalid_counts(actualSchema, count):
    user, _, _ = seedPaid(actualSchema)
    with pytest.raises(psycopg2.errors.CheckViolation):
        query(actualSchema, 'update public.credit_balances set domain_count=%s where user_id=%s', (count,user))


def test_unmapped_money_reconciliation_deduplicates_against_live_partial_index(actualSchema):
    repository = ManualBillingRepository(lambda: psycopg2.connect(actualSchema))
    payment = {'id': 'schema-payment-'+uuid.uuid4().hex, 'status':'captured', 'amount':100, 'currency':'INR'}
    repository.recordUnmappedPayment(payment)
    repository.recordUnmappedPayment(payment)
    assert len(query(actualSchema, 'select id from public.billing_events where idempotency_key=%s',
        ('unmapped:'+payment['id']+':captured',))) == 1


def test_initial_capture_and_receipt_replay_match_live_partial_index(actualSchema):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    user='schema-capture-'+uuid.uuid4().hex
    subscription,invoice,lifecycle=[str(uuid.uuid4()) for _ in range(3)]
    now=datetime.now(timezone.utc)
    query(actualSchema,'insert into public."Users"("userId",email) values(%s,%s)',(user,user+'@example.invalid'))
    query(actualSchema,'''insert into public.subscriptions(id,user_id,is_canonical,billing_state)
        values(%s,%s,true,%s)''',(subscription,user,Json({'manualBilling':{'lifecycleId':lifecycle}})))
    query(actualSchema,'''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,
        total_amount,currency,metadata_json) values(%s,%s,%s,'PAYMENT_PENDING','initial_purchase',3000,'INR',%s)''',
        (invoice,user,subscription,Json({'manualBilling':{'lifecycleId':lifecycle,'billingMode':'monthly_prepaid'}})))
    repository=ManualBillingRepository(lambda: psycopg2.connect(actualSchema))
    attempt=repository.reserveCheckoutIntent(user,'initial_purchase',invoice,invoice,{
        'subscriptionId':subscription,'invoiceId':invoice,'lifecycleId':lifecycle,'billingMode':'monthly_prepaid',
        'amount':3000,'currency':'INR','domains':['banking'],'expiresAt':(now+timedelta(minutes=30)).isoformat()})
    order='schema-order-'+uuid.uuid4().hex
    repository.bindProviderOrder(attempt.attemptId,{'id':order})
    evidence=VerifiedPaymentEvidence(attempt.attemptId,invoice,user,order,'schema-capture-'+uuid.uuid4().hex,
        'initial_purchase','INR','captured','server_observation',3000,now,None,None,False)
    assert repository.finalizeCapturedPayment(evidence).state=='activated'
    assert repository.finalizeCapturedPayment(evidence).state=='already_finalized'
    assert len(query(actualSchema,"select id from public.billing_events where user_id=%s and event_type='subscription.lifecycle.started'",(user,)))==1
    assert len(query(actualSchema,"select id from public.billing_events where user_id=%s and event_type='email.billing_intent.committed'",(user,)))==1


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
@pytest.mark.parametrize('table', TABLES)
@pytest.mark.parametrize('operation', ['select', 'insert', 'update', 'delete', 'truncate'])
def test_client_roles_cannot_read_or_mutate_financial_tables(actualSchema, role, table, operation):
    target = sql.Identifier('public', table)
    if operation == 'select': statement = sql.SQL('select * from {} limit 0').format(target)
    elif operation == 'insert': statement = sql.SQL('insert into {} default values').format(target)
    elif operation == 'update':
        field = 'user_id' if table in ('credit_balances','subscriptions','billing_events','admin_credit_reset_targets') else 'id'
        statement = sql.SQL('update {} set {}={} where false').format(target,sql.Identifier(field),sql.Identifier(field))
    elif operation == 'delete': statement = sql.SQL('delete from {} where false').format(target)
    else: statement = sql.SQL('truncate {} cascade').format(target)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        asRole(actualSchema, role, statement)


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
@pytest.mark.parametrize('function,args', RPCS)
def test_client_roles_cannot_execute_legacy_financial_rpcs(actualSchema, role, function, args):
    statement = sql.SQL('select * from {}({})').format(sql.Identifier('public',function),sql.SQL(',').join(sql.Placeholder() for _ in args))
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        asRole(actualSchema, role, statement, args)


def test_public_inheritance_cannot_restore_financial_access(actualSchema):
    connection = psycopg2.connect(actualSchema)
    try:
        with connection.cursor() as cursor:
            cursor.execute('create role financial_test_public_only nologin nobypassrls')
            cursor.execute('grant usage on schema public to financial_test_public_only')
            cursor.execute('set local role financial_test_public_only')
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cursor.execute("select public.decrement_topup_tokens('missing',1)")
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize('statement', [
    "update public.admin_audit_log set outcome='tampered' where false",
    "update public.admin_audit_log set target_id='tampered' where false",
    "update public.admin_audit_log set details='{}'::jsonb where false",
    'truncate public.admin_audit_log',
])
def test_service_role_cannot_edit_or_truncate_audit(actualSchema, statement):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        asRole(actualSchema, 'service_role', statement)


@pytest.mark.parametrize('statement', [
    'update public.admin_credit_reset_operations set id=id where false',
    'delete from public.admin_credit_reset_operations where false',
    'truncate public.admin_credit_reset_operations cascade',
    'delete from public.admin_credit_reset_targets where false',
    'truncate public.admin_credit_reset_targets',
])
def test_actual_default_grants_do_not_bypass_reset_history_policy(actualSchema, statement):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        asRole(actualSchema,'service_role',statement)


def test_service_role_retains_credit_mutation_and_legacy_topup_paths(actualSchema):
    user, subscription, _ = seedPaid(actualSchema)
    order, payment = 'test-order-'+uuid.uuid4().hex, 'test-payment-'+uuid.uuid4().hex
    query(actualSchema, '''insert into public."Invoices"("userId",subscription_id,status,billing_reason,
        razorpay_order_id,total_amount,pricing_reference_snapshot_json)
        values(%s,%s,'PAYMENT_PENDING','add_on',%s,100,%s)''',
        (user,subscription,order,Json({'tokens':1000})))
    connection = psycopg2.connect(actualSchema)
    try:
        with connection.cursor() as cursor:
            cursor.execute('set local role service_role')
            cursor.execute('select granted,tokens from public.grant_topup_tokens(%s,%s)', (order,payment))
            assert cursor.fetchone() == (True,1000)
            cursor.execute('select public.decrement_topup_tokens(%s,200)', (user,))
            assert cursor.fetchone()[0] == 3300
            cursor.execute('select clawed,tokens from public.clawback_topup_tokens(%s,%s,50)', ('test-refund-'+uuid.uuid4().hex,payment))
            assert cursor.fetchone() == (True,500)
            cursor.execute('update public.credit_balances set used_tokens=7100 where user_id=%s returning topup_tokens', (user,))
            assert cursor.fetchone()[0] == 2800
        connection.rollback()
    finally: connection.close()


def test_actual_schema_admin_reset_is_atomic_and_preserves_topups(actualSchema):
    from api.services.adminAuthService import AdminContext
    from api.services.adminCreditResetRepository import AdminCreditResetRepository
    user, _, _ = seedPaid(actualSchema)
    resets = AdminCreditResetRepository(ManualBillingRepository(lambda: psycopg2.connect(actualSchema)))
    admin = AdminContext(adminId=str(uuid.uuid4()),email='schema-ops@example.invalid',name='Ops',sessionId=str(uuid.uuid4()),token='test-only')
    stored = resets.createOrGetOperation('individual',user,'Schema regression',uuid.uuid4().hex,admin)
    with patch('api.services.credits.creditConfig.getTokenQuotaForPlan', return_value=10000):
        result = resets.resetTarget(str(stored['id']),user)
    assert result['outcome'] == 'RESET'
    [balance] = query(actualSchema,'select used_tokens,remaining_tokens,topup_tokens from public.credit_balances where user_id=%s',(user,))
    assert (balance['used_tokens'],balance['remaining_tokens'],balance['topup_tokens']) == (0,10000,2500)
    assert len(query(actualSchema,"select id from public.admin_audit_log where target_id=%s and action='credits.reset'",(user,))) == 1


def test_forward_fix_migrations_can_be_reapplied_without_regranting_clients(actualSchema):
    fixes = [path for path in buildMigrationPlan(set(),'expand') if path.name.endswith(('_allow_inactive_credit_domains.sql','_restrict_financial_data_access.sql'))]
    assert len(fixes) == 2
    with closing(psycopg2.connect(actualSchema)) as connection, connection:
        with connection.cursor() as cursor:
            for migration in fixes: cursor.execute(migration.read_text(encoding='utf-8'))
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        asRole(actualSchema,'anon','select * from public.credit_balances limit 0')
