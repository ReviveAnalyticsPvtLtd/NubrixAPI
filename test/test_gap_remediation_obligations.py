import json
from datetime import timedelta
from unittest.mock import Mock, patch
from test.test_manual_billing_runtime import database, USER, NOW, seed_payment, sqlTransaction, read_row, SqlConnection, SqlCursor
from test.test_manual_obligation_reports import obligations
from test.test_billing_notification_revisions import deliveries
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request


def test_held_notification_is_reported_as_held(obligations):
    service, _ = obligations
    report = service.listManualObligations()
    assert report['totals']['notification_held'] == 1
    assert next(row for row in report['items'] if row['id']=='bridge')['category']=='notification_held'


def test_operator_page_fetches_bounded_metadata(obligations):
    service, path = obligations
    with sqlTransaction(path) as db:
        for index in range(1000):
            db.execute("insert into billing_events(id,user_id,event_category,event_type,event_status,payment_status,metadata_json,occurred_at) values(?,?,'payment_attempt','payment.attempt','failed','failed','{}',?)", (f'history-{index}',USER,NOW.isoformat()))
    counts=[]
    class CountedCursor(SqlCursor):
        def execute(self, query, params=()):
            try:
                return super().execute(query, params)
            except Exception as error:
                print('SQL adapter diagnostic:', type(error).__name__, str(error))
                raise
        def fetchall(self):
            rows=super().fetchall()
            if rows and 'metadata_json' in rows[0]: counts.append(len(rows))
            return rows
    class CountedConnection(SqlConnection):
        def cursor(self, **kwargs): return CountedCursor(self.connection.cursor())
    service.repository.connectionFactory=lambda:CountedConnection(path)
    page=service.listManualObligations(limit=2)
    assert page['available'] and page['total']==7
    assert max(counts)<=3


def test_zero_measured_run_is_durably_settled(database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo,path=database
    repo.finalizeCapturedPayment(seed_payment(database))
    credits=ManualCreditRepository(repo)
    with patch('api.services.credits.manualCreditRepository.datetime',Mock(now=lambda _:NOW)):
        context=credits.admit(USER,'report','zero-run')
        before=read_row(path,'credit_balances')['remaining_tokens']
        credits.reportUsage(context,0,'zero-llm')
        result=credits.settle(context,0,'zero-llm')
    assert result['settled']
    assert read_row(path,'credit_balances')['remaining_tokens']==before
    with sqlTransaction(path) as db:
        assert db.execute("select event_status from billing_events where idempotency_key=?",('credit-report:'+USER+':zero-run:zero-llm',)).fetchone()[0]=='SETTLED'


def test_cancelled_unbound_renewal_releases_evidence_hold(checkout_database):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','renewal')
    reserved=repo.reserveCheckout(checkout)
    repo.setRenewalOptOut(USER,True,'Finished','cancel')
    later=NOW+timedelta(hours=1)
    with patch('api.services.billing.manualBillingRepository._now',return_value=later):
        ManualBillingRecoveryService(repo,manual.razorpayClient).recoverAttempt(repo.attemptById(USER,reserved.attemptId))
    attempt=repo.attemptById(USER,reserved.attemptId)
    assert json.loads(attempt['metadata_json'])['manualBilling']['closureReconciledAt']
