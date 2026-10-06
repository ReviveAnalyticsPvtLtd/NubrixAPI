"""Exercise production transaction methods against a local SQL adapter.

SQLite runs the actual queries/rollback for deterministic unit coverage. It
does not prove PostgreSQL advisory-lock or Redis interleaving behaviour;
those are exercised separately by the opt-in integration suites.
"""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
from api.services.billing.manualBillingRepository import ManualBillingRepository


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
USER = "runtime-user"
SUB = "d049a66e-89fc-41ac-aaf7-919143c79bf8"
LIFE = "9cdff447-e185-496a-ae6a-6c87327882fe"


@contextmanager
def sqlTransaction(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SqlCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, query, params=()):
        query = query.replace("public.", "")
        query = re.sub(r"\s+for update\b", "", query, flags=re.I)
        query = re.sub(r"::(?:uuid|jsonb|text|timestamptz)", "", query)
        query = query.replace("%s", "?")
        values = []
        for value in params:
            if hasattr(value, "adapted"):
                value = json.dumps(value.adapted, default=str)
            elif isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, bool):
                value = int(value)
            values.append(value)
        self.cursor.execute(query, values)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount


class SqlConnection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function("now", 0, lambda: NOW.isoformat())
        self.connection.create_function("pg_advisory_xact_lock", 1, lambda _: 1)

    def cursor(self, **kwargs):
        return SqlCursor(self.connection.cursor())

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    connection.executescript('''
      CREATE TABLE "Users" (
        "userId" TEXT PRIMARY KEY, email TEXT, password TEXT, onboarded BOOLEAN,
        "currentWorkspaceId" TEXT, "profileImage" TEXT, "isBanned" BOOLEAN DEFAULT 0
      );
      CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY, user_id TEXT, is_canonical BOOLEAN,
        billing_mode TEXT, status TEXT, plan_type TEXT,
        current_period_start TEXT, current_period_end TEXT, renewal_due_at TEXT,
        auto_renew_enabled BOOLEAN DEFAULT 0, renewal_opt_out BOOLEAN DEFAULT 0,
        payment_collection_mode TEXT, default_currency TEXT DEFAULT 'INR',
        version INTEGER DEFAULT 0, erasure_pending BOOLEAN DEFAULT 0,
        subscribed_experts TEXT DEFAULT '[]', domain_count INTEGER DEFAULT 0,
        pending_removals TEXT DEFAULT '[]', pending_additions TEXT DEFAULT '[]',
        billing_state TEXT DEFAULT '{}', cancellation_reason TEXT,
        updated_at TEXT
      );
      CREATE TABLE "Invoices" (
        id TEXT PRIMARY KEY, "userId" TEXT, subscription_id TEXT,
        status TEXT, billing_reason TEXT, amount INTEGER, total_amount INTEGER,
        currency TEXT, razorpay_order_id TEXT, "razorpayPaymentId" TEXT,
        "paidAt" TEXT, period_start TEXT, period_end TEXT, due_date TEXT,
        metadata_json TEXT DEFAULT '{}'
      );
      CREATE TABLE billing_events (
        id TEXT PRIMARY KEY, user_id TEXT, subscription_id TEXT, invoice_id TEXT,
        event_category TEXT, event_type TEXT, event_status TEXT,
        payment_attempt_type TEXT, payment_status TEXT, provider TEXT,
        provider_order_id TEXT, provider_payment_id TEXT UNIQUE,
        amount INTEGER, currency TEXT, idempotency_key TEXT UNIQUE,
        metadata_json TEXT DEFAULT '{}', period_start TEXT, period_end TEXT,
        attempted_at TEXT, occurred_at TEXT, completed_at TEXT,
        failure_reason TEXT, updated_at TEXT, cycle_key TEXT
      );
      CREATE TABLE credit_balances (
        user_id TEXT PRIMARY KEY, subscription_id TEXT, plan_tier TEXT,
        domain_count INTEGER, monthly_token_quota INTEGER, used_tokens INTEGER,
        remaining_tokens INTEGER, topup_tokens INTEGER DEFAULT 0,
        period_start TEXT, period_end TEXT, lifecycle_id TEXT, credit_period_id TEXT,
        balance_version INTEGER DEFAULT 0, last_reset_at TEXT, updated_at TEXT
      );
    ''')
    connection.execute('INSERT INTO "Users"("userId") VALUES(?)', (USER,))
    connection.execute(
        "INSERT INTO subscriptions(id,user_id,is_canonical,billing_mode,status,plan_type,billing_state) VALUES(?,?,1,'none','none','none',?)",
        (SUB, USER, json.dumps({"manualBilling": {"lifecycleId": LIFE}})),
    )
    connection.commit()
    connection.close()
    repository = ManualBillingRepository(lambda: SqlConnection(path))
    with patch("api.services.billing.manualBillingRepository._now", return_value=NOW):
        yield repository, path


def seed_payment(database, purpose="initial_purchase", invoice="invoice-one", order="order-one", domains=None):
    repository, path = database
    start, end = "2026-10-20T12:00:00+00:00", "2026-11-20T12:00:00+00:00"
    metadata = {"manualBilling": {"lifecycleId": LIFE, "purpose": purpose,
                "billingMode": "monthly_prepaid", "domains": domains or ["banking"],
                "coverageState": "estimated", "revision": 1}}
    with sqlTransaction(path) as connection:
        connection.execute('INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json) VALUES(?,?,?,\'PAYMENT_PENDING\',?,3000,\'INR\',?,?,?)',
                           (invoice, USER, SUB, purpose, start, end, json.dumps(metadata)))
    attempt = repository.reserveCheckoutIntent(USER, purpose, invoice, invoice, {
        "subscriptionId": SUB, "invoiceId": invoice, "lifecycleId": LIFE,
        "purpose": purpose, "billingMode": "monthly_prepaid", "domains": domains or ["banking"],
        "amount": 3000, "expiresAt": "2026-10-06T12:30:00+00:00",
        "periodStart": start, "periodEnd": end,
    })
    repository.bindProviderOrder(attempt.attemptId, {"id": order})
    return VerifiedPaymentEvidence(attempt.attemptId, invoice, USER, order, "payment-one",
        purpose, "INR", "captured", "server_observation", 3000, NOW, NOW, None, True)


def read_row(path, table):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    row = connection.execute(f'SELECT * FROM "{table}"').fetchone()
    connection.close()
    return dict(row) if row else None


def test_real_repository_finalizes_once_with_shared_calendar_dates(database):
    repository, path = database
    evidence = seed_payment(database)
    first = repository.finalizeCapturedPayment(evidence)
    replay = repository.finalizeCapturedPayment(evidence)
    assert first.finalized and first.state == "activated"
    assert replay.state == "already_finalized"
    assert first.currentPeriod.start == NOW
    assert first.currentPeriod.end.isoformat() == "2026-11-06T12:00:00+00:00"
    credit = read_row(path, "credit_balances")
    assert credit["period_start"] == NOW.isoformat()
    assert credit["period_end"] == first.currentPeriod.end.isoformat()
    assert credit["credit_period_id"] == first.currentPeriod.creditPeriodId


def test_wrong_owner_rolls_back_without_paid_invoice(database):
    repository, path = database
    evidence = seed_payment(database)
    from dataclasses import replace
    with pytest.raises(ValueError, match="OWNERSHIP"):
        repository.finalizeCapturedPayment(replace(evidence, userId="other-owner"))
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert read_row(path, "credit_balances") is None


def test_paid_future_activates_at_original_boundary_not_early(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET status='active',plan_type='pro',billing_mode='monthly_prepaid',current_period_start=?,current_period_end=?,subscribed_experts=?",
            ("2026-09-20T12:00:00+00:00", "2026-10-20T12:00:00+00:00", '["banking"]'))
    evidence = seed_payment(database, purpose="renewal")
    scheduled = repository.finalizeCapturedPayment(evidence)
    assert scheduled.state == "paid_scheduled" and not scheduled.creditsRefilled
    state=json.loads(read_row(path,"subscriptions")["billing_state"])
    assert state["manualBilling"]["paidFutureEnd"]=="2026-11-20T12:00:00+00:00"
    assert read_row(path, "credit_balances") is None
    boundary = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    activated = repository.activateDueCoverage(USER, boundary)
    assert activated.state == "activated"
    assert activated.currentPeriod.start == boundary
    assert activated.currentPeriod.end.isoformat() == "2026-11-20T12:00:00+00:00"
    again = repository.activateDueCoverage(USER, boundary)
    assert not again.creditsRefilled


def test_uncaptured_payment_does_not_grant(database):
    repository, path = database
    from dataclasses import replace
    evidence = seed_payment(database)
    result = repository.finalizeCapturedPayment(replace(evidence, financialStatus="authorized"))
    assert not result.finalized and result.state == "awaiting_capture"
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"


def test_second_capture_is_tracked_without_second_period(database):
    repository, path = database
    from dataclasses import replace
    evidence = seed_payment(database)
    first = repository.finalizeCapturedPayment(evidence)
    duplicate = repository.finalizeCapturedPayment(replace(evidence, providerPaymentId="payment-two"))
    assert duplicate.state == "requires_reconciliation"
    assert duplicate.anomalyId
    assert read_row(path, "subscriptions")["current_period_end"] == first.currentPeriod.end.isoformat()


def test_usage_counts_distinct_llm_runs_and_deduplicates_replay(database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository, path = database
    repository.finalizeCapturedPayment(seed_payment(database))
    credits = ManualCreditRepository(repository)
    with patch("api.services.credits.manualCreditRepository.datetime") as clock:
        clock.now.return_value = NOW
        context = credits.admit(USER, "reporting_query", "workflow-one")
        before = read_row(path, "credit_balances")["remaining_tokens"]
        credits.settle(context, 100, "llm-one")
        credits.settle(context, 100, "llm-one")
        credits.settle(context, 200, "llm-two")
    assert read_row(path, "credit_balances")["remaining_tokens"] == before - 300


def test_delayed_usage_never_debits_new_period_quota(database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository, path = database
    repository.finalizeCapturedPayment(seed_payment(database))
    credits = ManualCreditRepository(repository)
    with patch("api.services.credits.manualCreditRepository.datetime") as clock:
        clock.now.return_value = NOW
        context = credits.admit(USER, "reporting_query", "old-workflow")
        with sqlTransaction(path) as connection:
            connection.execute("UPDATE credit_balances SET credit_period_id='new-paid-period',remaining_tokens=1000")
        result = credits.settle(context, 200, "old-llm")
    assert result["historicalPeriod"]
    assert result["monthlyCharged"] == 200
    assert read_row(path, "credit_balances")["remaining_tokens"] == 1000


def test_expired_unpaid_period_cannot_admit_usage_or_refill(database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository, path = database
    repository.finalizeCapturedPayment(seed_payment(database))
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE credit_balances SET topup_tokens=1234")
    after = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    with patch("api.services.credits.manualCreditRepository.datetime") as clock:
        clock.now.return_value = after
        with pytest.raises(ValueError, match="PAID_COVERAGE"):
            ManualCreditRepository(repository).admit(USER, "reporting_query", "unpaid-work")
    row = read_row(path, "credit_balances")
    assert row["remaining_tokens"] == 0 and row["topup_tokens"] == 1234


def test_staff_refund_atomically_expires_access_and_preserves_topups(database):
    from api.services.billing.manualBillingContracts import RefundQuote
    from datetime import timedelta
    repository, path = database
    repository.finalizeCapturedPayment(seed_payment(database))
    start, end = NOW - timedelta(days=10), NOW + timedelta(days=20)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Invoices" SET period_start=?,period_end=?', (start.isoformat(),end.isoformat()))
        connection.execute('UPDATE subscriptions SET current_period_start=?,current_period_end=?', (start.isoformat(),end.isoformat()))
        connection.execute('UPDATE credit_balances SET topup_tokens=4321')
    quote = RefundQuote('quote-one',USER,'email-case-123','INR',NOW,NOW+timedelta(minutes=5),2000,
        ({'invoiceId':'invoice-one','paymentId':'payment-one'},),True,False)
    repository.saveRefundQuote('staff-one',quote,'Approved exceptional return')
    intent = repository.reserveUnusedTimeRefund('quote-one','staff-one','email-case-123','Approved exceptional return',2000,'request-one')
    assert intent.amount == 2000 and intent.accessExpired
    assert read_row(path,'subscriptions')['status'] == 'expired'
    credit = read_row(path,'credit_balances')
    assert credit['remaining_tokens'] == 0 and credit['topup_tokens'] == 4321
    replay = repository.reserveUnusedTimeRefund('quote-one','staff-one','email-case-123','Approved exceptional return',2000,'request-one')
    assert replay.refundIntentId == intent.refundIntentId
    pending = repository.settleRefundEvidence(intent.refundIntentId,{'refunds':[{'id':'refund-one','payment_id':'payment-one','amount':2000,'status':'pending'}]})
    assert pending['refundState'] == 'pending'
    done = repository.settleRefundEvidence(intent.refundIntentId,{'refunds':[{'id':'refund-one','payment_id':'payment-one','amount':2000,'status':'processed'}]})
    assert done['refundState'] == 'processed' and not done['accessRestored']


def test_expert_capture_preserves_current_dates_and_consumed_usage(database):
    from dataclasses import replace
    repository,path=database
    first=repository.finalizeCapturedPayment(seed_payment(database))
    evidence=seed_payment(database,'expert_addition','invoice-two','order-two',['telecom'])
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Invoices" SET billing_reason=\'proration\',period_start=?,period_end=? WHERE id=\'invoice-two\'',(NOW.isoformat(),first.currentPeriod.end.isoformat()))
        connection.execute('UPDATE subscriptions SET pending_additions=?',(json.dumps([{'domain':'telecom','orderId':'order-two','state':'awaiting_payment'}]),))
        connection.execute('UPDATE credit_balances SET used_tokens=200,remaining_tokens=remaining_tokens-200,topup_tokens=1234')
    result=repository.finalizeCapturedPayment(replace(evidence,providerPaymentId='payment-two'))
    assert result.finalized and result.state=='expert_activated' and not result.creditsRefilled
    subscription=read_row(path,'subscriptions')
    assert subscription['current_period_end']==first.currentPeriod.end.isoformat()
    assert json.loads(subscription['subscribed_experts'])==['banking','telecom']
    credit=read_row(path,'credit_balances')
    assert credit['used_tokens']==200 and credit['topup_tokens']==1234
    assert credit['remaining_tokens']==credit['monthly_token_quota']-200


def test_removal_changes_only_unpaid_next_selection_and_closes_checkout(database):
    repository,path=database
    repository.finalizeCapturedPayment(seed_payment(database))
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET subscribed_experts=\'["banking","telecom"]\',domain_count=2')
        connection.execute('INSERT INTO "Invoices"(id,"userId",subscription_id,billing_reason,status,period_start,period_end) VALUES(?,?,?,\'renewal\',\'PAYMENT_PENDING\',?,?)',
            ('renewal-unpaid',USER,SUB,'2026-11-06T12:00:00+00:00','2026-12-06T12:00:00+00:00'))
    result=repository.scheduleExpertRemoval(USER,['telecom'])
    assert result['pendingRemovals']==['telecom']
    assert json.loads(read_row(path,'subscriptions')['subscribed_experts'])==['banking','telecom']
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT status FROM "Invoices" WHERE id=\'renewal-unpaid\'').fetchone()[0]=='VOID'


def test_renewal_snapshot_creation_rejects_stale_selection_version(database):
    repository,path=database
    first=repository.finalizeCapturedPayment(seed_payment(database))
    row=read_row(path,'subscriptions')
    payload={'userId':USER,'subscription_id':SUB,'billing_reason':'renewal','status':'PAYMENT_PENDING',
        'total_amount':3000,'currency':'INR','period_start':first.currentPeriod.end.isoformat(),
        'period_end':'2026-12-06T12:00:00+00:00','metadata_json':{'manualBilling':{'lifecycleId':LIFE,'domains':['banking']}}}
    with pytest.raises(ValueError,match='STALE_SUBSCRIPTION_VERSION'):
        repository.createFrozenRenewalInvoice(payload,row['version']-1)
    invoice=repository.createFrozenRenewalInvoice(payload,row['version'])
    again=repository.createFrozenRenewalInvoice(payload,row['version'])
    assert again['id']==invoice['id']


def test_monthly_attempt_ttl_can_replace_known_expired_order(database):
    from datetime import timedelta
    repository,path=database
    seed_payment(database)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET billing_mode=\'monthly_prepaid\',current_period_start=?,current_period_end=?',
            (NOW.isoformat(),(NOW+timedelta(days=20)).isoformat()))
    snapshot={'subscriptionId':SUB,'invoiceId':'invoice-one','lifecycleId':LIFE,'billingMode':'monthly_prepaid',
        'amount':3000,'currency':'INR','domains':['banking'],'expiresAt':(NOW+timedelta(days=20)).isoformat()}
    attempt=repository.reserveCheckoutIntent(USER,'renewal','renewal-key','snapshot-hash',snapshot)
    assert attempt.expiresAt==NOW+timedelta(minutes=30)
    repository.bindProviderOrder(attempt.attemptId,{'id':'renewal-order'})
    with patch(__name__ + '.NOW', NOW+timedelta(minutes=31)):
        replacement=repository.reserveCheckoutIntent(USER,'renewal','renewal-key','snapshot-hash',snapshot)
    assert replacement.attemptId != attempt.attemptId and replacement.revision==2


def test_expert_cancellation_closes_bundle_under_same_capture_lock(database):
    repository,path=database
    repository.finalizeCapturedPayment(seed_payment(database))
    seed_payment(database,'expert_addition','invoice-two','order-two',['telecom'])
    result=repository.cancelExpertAddition(USER,'telecom')
    assert result['closedAttemptDomains']==['telecom']
    with sqlTransaction(path) as connection:
        assert connection.execute('SELECT status FROM "Invoices" WHERE id=\'invoice-two\'').fetchone()[0]=='VOID'
        assert connection.execute('SELECT payment_status FROM billing_events WHERE provider_order_id=\'order-two\'').fetchone()[0]=='cancelled'


def test_production_checkout_and_browser_finalizer_use_same_repository(database):
    from datetime import timedelta
    from unittest.mock import Mock
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repository,path=database
    with sqlTransaction(path) as connection:
        connection.execute('INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,metadata_json) VALUES(?,?,?,\'PAYMENT_PENDING\',\'initial_purchase\',3000,\'INR\',?)',
            ('service-invoice',USER,SUB,json.dumps({'manualBilling':{'lifecycleId':LIFE,'billingMode':'monthly_prepaid','domains':['banking']}})))
    invoice=read_row(path,'Invoices')
    invoice['metadata_json']=json.loads(invoice['metadata_json'])
    subscription=read_row(path,'subscriptions')
    service=SubscriptionService.__new__(SubscriptionService)
    service.razorpayClient=Mock()
    service.razorpayClient.order.create.side_effect=lambda payload:{**payload,'id':'service-order','status':'created'}
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository',return_value=repository):
        intent = repository.reserveCheckoutIntent(USER,'initial_purchase','service-invoice','service',{
            'subscriptionId':SUB,'invoiceId':'service-invoice','lifecycleId':LIFE,'billingMode':'monthly_prepaid','domains':['banking'],
            'amount':3000,'currency':'INR','expiresAt':(NOW+timedelta(minutes=30)).isoformat()})
        repository.claimProviderOrderCreation(intent.attemptId)
        order=service.razorpayClient.order.create({'amount':3000,'currency':'INR','receipt':intent.attemptId,
            'notes':{'attemptId':intent.attemptId}})
        repository.bindProviderOrder(intent.attemptId,order)
        payment={'id':'service-payment','order_id':order['id'],'amount':3000,'currency':'INR','status':'captured'}
        result=service._finalizeManualCheckout('service-invoice',order['id'],'service-payment',payment,now=NOW)
        replay=service._finalizeManualCheckout('service-invoice',order['id'],'service-payment',payment,now=NOW)
    assert result['creditsRefilled'] and replay['alreadyFinalized']
    assert order['receipt']==order['notes']['attemptId']
    assert not {'customer_id','token','recurring'} & set(order)
    assert read_row(path,'Invoices')['status']=='PAID'


def test_erased_account_capture_keeps_money_identity_without_access(database):
    from dataclasses import replace
    repository,path=database
    evidence=seed_payment(database)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Invoices" SET "userId"=null,subscription_id=null,metadata_json=\'{}\'')
        connection.execute('UPDATE billing_events SET user_id=null,subscription_id=null,metadata_json=\'{}\'')
        connection.execute('DELETE FROM subscriptions')
    result=repository.finalizeCapturedPayment(replace(evidence,userId=None))
    replay=repository.finalizeCapturedPayment(replace(evidence,userId=None))
    assert result.state=='requires_reconciliation' and not result.finalized and not result.creditsRefilled
    assert replay.anomalyId==result.anomalyId
    assert read_row(path,'credit_balances') is None


def test_manual_balance_without_identity_cannot_fall_back_to_redis():
    from unittest.mock import Mock
    from api.services.credits.creditService import CreditService
    service=CreditService.__new__(CreditService)
    service._redis=Mock()
    with patch('api.services.credits.manualCreditRepository.ManualCreditRepository.balanceSnapshot',
            side_effect=RuntimeError('CREDIT_PERIOD_MISSING')):
        with pytest.raises(RuntimeError,match='CREDIT_PERIOD_MISSING'):
            service._manualBalance(USER)
    service._redis.assert_not_called()


def test_measured_usage_survives_failure_before_balance_settlement(database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,path=database
    repository.finalizeCapturedPayment(seed_payment(database))
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'report','work-one')
        credits.reportUsage(context,200,'run-one')
        credits.reportUsage(context,200,'run-one')
        before=read_row(path,'credit_balances')['remaining_tokens']
        assert credits.recoverUsage()['settled']==1
        assert credits.recoverUsage()['settled']==0
    assert read_row(path,'credit_balances')['remaining_tokens']==before-200


def test_valid_topup_capture_after_expiry_grants_stored_tokens_without_access(database):
    from dataclasses import replace
    from datetime import timedelta
    repository,path=database
    repository.finalizeCapturedPayment(seed_payment(database))
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET current_period_end=?',((NOW+timedelta(minutes=10)).isoformat(),))
        connection.execute('INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,metadata_json) VALUES(?,?,?,\'PAYMENT_PENDING\',\'add_on\',150,\'INR\',?)',
            ('topup-invoice',USER,SUB,json.dumps({'tokens':500})))
    attempt=repository.reserveCheckoutIntent(USER,'topup','topup-key','topup-hash',{
        'subscriptionId':SUB,'invoiceId':'topup-invoice','lifecycleId':LIFE,'billingMode':'monthly_prepaid',
        'tokens':500,'amount':150,'currency':'INR','expiresAt':(NOW+timedelta(minutes=30)).isoformat()})
    repository.bindProviderOrder(attempt.attemptId,{'id':'topup-order'})
    late=NOW+timedelta(minutes=20)
    repository.activateDueCoverage(USER,late)
    evidence=VerifiedPaymentEvidence(attempt.attemptId,'topup-invoice',USER,'topup-order','topup-payment','topup',
        'INR','captured','server_observation',150,late,None,None,False)
    result=repository.finalizeCapturedPayment(evidence)
    assert result.state=='topup_granted' and not result.creditsRefilled
    assert repository.finalizeCapturedPayment(evidence).state=='already_finalized'
    balance=read_row(path,'credit_balances')
    assert balance['topup_tokens']==500 and balance['remaining_tokens']==0
    assert read_row(path,'subscriptions')['status']=='expired'


def test_manual_topup_cannot_fall_back_when_provider_notes_are_missing():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api.services.credits.creditService import CreditService
    service=CreditService.__new__(CreditService)
    service.supabase=Mock()
    service.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.limit.return_value.execute.return_value.data=[
        {'metadata_json':{'manualBilling':{'purpose':'topup'}}}]
    repository=Mock()
    repository.attemptForOrder.return_value={'id':'attempt','invoice_id':'invoice','user_id':USER,
        'metadata_json':{'manualBilling':{'tokens':500,'purpose':'topup'}}}
    repository._json.side_effect=lambda value:value
    repository.finalizeCapturedPayment.return_value=SimpleNamespace(state='requires_reconciliation',anomalyId='late-capture',
        invoiceId='invoice',attemptId='attempt',finalized=False,creditState='pending_materialization',
        creditsRefilled=False,renewalOptOut=False,currentPeriod=None,nextPeriod=None)
    provider=Mock()
    provider.order.fetch.return_value={'id':'topup-order','notes':{}}
    provider.payment.fetch.return_value={'id':'topup-payment','order_id':'topup-order',
        'amount':150,'currency':'INR','status':'captured'}
    with patch('api.services.subscriptions.subscriptionService.subscriptionService.razorpayClient',provider), \
         patch('api.services.billing.manualBillingRepository.getManualBillingRepository',return_value=repository):
        result=service.grantTopupTokens(USER,'topup-order','topup-payment')
    assert result=={'granted':False,'tokens':0,'disposition':'requires_reconciliation','anomalyId':'late-capture'}
    service.supabase.rpc.assert_not_called()
    assert repository.finalizeCapturedPayment.call_args.args[0].purpose=='topup'
