import json
from unittest.mock import Mock, patch
import pytest

from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, checkout_client, request
from test.test_manual_payment_entrypoints import evidence
from test.test_support_refund_http import refund_client, payload


def test_public_cancel_closes_attempt_and_unbound_bundle_only(checkout_client, checkout_database):
    from api.services.billing.manualPaymentService import ManualPaymentService
    client, provider, path = checkout_client
    repo, _ = checkout_database
    manual = ManualPaymentService.forProduction(provider, repo)
    initial = manual.createCheckout(request('initial', mode='annual_prepaid'))
    manual.finalizeCapturedPayment(evidence(initial))
    first = repo.reserveCheckout(request('bundle-one', mode='annual_prepaid', purpose='expert_addition', domains=('telecom',)))
    other = repo.reserveCheckout(request('bundle-two', mode='annual_prepaid', purpose='expert_addition', domains=('manufacturing',)))
    response = client.post('/cancelPendingAddition', json={'domain':'telecom'})
    assert response.status_code == 200
    assert response.json()['data']['closedAttemptDomains'] == ['telecom']
    assert repo.attemptById(USER, first.attemptId)['payment_status'] == 'cancelled'
    assert repo.attemptById(USER, other.attemptId)['payment_status'] == 'created'


def test_annual_remove_finalizes_observed_expert_money_once(checkout_client, checkout_database, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    client, provider, path = checkout_client
    repo, _ = checkout_database
    manual = ManualPaymentService.forProduction(provider, repo)
    manual.finalizeCapturedPayment(evidence(manual.createCheckout(request('initial', mode='annual_prepaid'))))
    added = manual.createCheckout(request('add', mode='annual_prepaid', purpose='expert_addition', domains=('telecom',)))
    payment = {'id':'captured-addition','order_id':added.razorpayOrderId,'amount':added.amount,'currency':added.currency,'status':'captured'}
    provider.order.fetch = Mock(return_value={'status':'paid','notes':{'domains':'telecom','targetQuantity':'2'}})
    provider.order.payments = lambda *a, **kw: {'items':[payment]}
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda:NOW)
    with patch('api.services.billing.manualBillingRecoveryService.datetime', Mock(now=lambda _:NOW)):
        response = client.post('/removeDomain', json={'domains':['banking']})
    assert response.status_code == 200, response.text
    with sqlTransaction(path) as db:
        assert db.execute('select status from "Invoices" where id=?',(added.invoiceId,)).fetchone()[0] == 'PAID'
        assert db.execute("select count(*) from billing_events where event_type='payment.capture' and event_status='FINALIZED' and provider_payment_id='captured-addition'").fetchone()[0] == 1
        assert db.execute("select count(*) from billing_events where idempotency_key='notification:receipt:captured-addition'").fetchone()[0] == 1
    assert read_row(path,'credit_balances')['balance_version'] == 2
    replay = manual.finalizeCapturedPayment(evidence(added,'captured-addition'))
    assert replay.state == 'already_finalized'


def test_refund_replay_submits_reserved_unsubmitted_money_once(refund_client):
    client, repo, path, provider, quote = refund_client
    reserved = repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',quote.amount,'approval')
    assert not provider.calls
    for _ in range(2):
        response = client.post('/refunds/initiate', json=payload(quote), headers={'Idempotency-Key':'approval'})
        assert response.status_code == 200, response.text
        assert response.json()['data']['refundIntentId'] == reserved.refundIntentId
    assert len(provider.calls) == 1
    assert response.json()['data']['refundState'] == 'processed'


def test_recovery_worker_resumes_crash_after_refund_reserve_once(refund_client):
    from types import SimpleNamespace
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    client, repo, path, provider, quote = refund_client
    reserved = repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',quote.amount,'approval')
    def refund(payment, payload):
        return provider.refund(payment,payload['amount'],payload['notes']['refundIntentId'])
    transport = SimpleNamespace(payment=SimpleNamespace(refund=refund,fetch_multiple_refund=lambda *args:{'items':[]}))
    # Translate only PostgreSQL interval syntax for this isolated SQL transport.
    from test.test_manual_billing_runtime import SqlCursor, SqlConnection
    class RecoveryCursor(SqlCursor):
        def execute(self,query,params=()):
            query=query.replace("now()-interval '15 minutes'","datetime(now(),'-15 minutes')")
            query=query.replace("coalesce(metadata_json,'{}'::jsonb) || %s","json_patch(coalesce(metadata_json,'{}'),%s)")
            return super().execute(query,params)
    class RecoveryConnection(SqlConnection):
        def cursor(self,**kwargs): return RecoveryCursor(self.connection.cursor())
    repo.connectionFactory=lambda:RecoveryConnection(path)
    with sqlTransaction(path) as db:
        db.execute("update billing_events set updated_at='2020-01-01T00:00:00+00:00'")
    worker = ManualBillingRecoveryService(repo,transport)
    first = worker.execute()
    second = worker.execute()
    assert first['resolved']==1 and first['errors']==0
    assert second['checked']==0 and len(provider.calls)==1
    with sqlTransaction(path) as db:
        row=db.execute("select event_status from billing_events where id=?",(reserved.refundIntentId,)).fetchone()
    assert row[0]=='processed'
