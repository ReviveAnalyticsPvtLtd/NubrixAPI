import json
from datetime import timedelta
from unittest.mock import Mock, patch
import pytest
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database, checkout_client, request
from test.test_manual_payment_entrypoints import paid_then_request, evidence
from test.test_support_refund_http import refund_client, payload


def paid_monthly(checkout_database):
    manual, checkout=paid_then_request(checkout_database,'monthly_prepaid','initial_purchase')
    return manual, manual.finalizeCapturedPayment(evidence(manual.createCheckout(checkout)))


def test_cancel_resume_return_committed_coverage_contract(checkout_client, checkout_database, monkeypatch):
    client,provider,path=checkout_client
    repo,_=checkout_database
    paid_monthly(checkout_database)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda:NOW)
    for route,body in [('/cancelSubscription',{'reason':'Done'}),('/resumeRenewal',{})]:
        response=client.post(route,json=body)
        assert response.status_code==200,response.text
        data=response.json()['data']
        assert {'renewalOptOut','effectiveAt','currentPeriod','nextPeriod','invoiceId','state','refundInitiated'}<=set(data)
        assert data['currentPeriod']['domains']==['banking']
        assert data['refundInitiated'] is False


@pytest.mark.parametrize('scenario,expected',[('reason',422),('expired',403),('optout',409),('remove',409)])
def test_monthly_policy_errors_are_flat_domain_errors(checkout_client,checkout_database,monkeypatch,scenario,expected):
    client,_,path=checkout_client
    repo,_=checkout_database
    _,result=paid_monthly(checkout_database)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow',lambda:NOW)
    if scenario=='reason': route,body='/cancelSubscription',{'reason':'x'*1001}
    elif scenario=='optout':
        repo.setRenewalOptOut(USER,True,None,'optout');route,body='/prepareRenewalInvoice',{}
    elif scenario=='remove': route,body='/removeDomain',{'domains':['banking']}
    else:
        route,body='/cancelSubscription',{}
        monkeypatch.setattr('api.services.billing.manualBillingRepository._now',lambda:result.currentPeriod.end+timedelta(seconds=1))
    response=client.post(route,json=body)
    assert response.status_code==expected,response.text


@pytest.mark.parametrize('mode',['monthly_prepaid','annual_prepaid'])
def test_second_renewal_key_returns_conflict_without_duplicate(checkout_client,checkout_database,mode):
    client,provider,path=checkout_client
    manual,checkout=paid_then_request(checkout_database,mode,'renewal')
    import api.routers.subscriptions as routes
    routes.subscriptionService.razorpayClient=manual.razorpayClient
    provider=manual.razorpayClient
    before=len(provider.order.createdPayloads)
    from pathlib import Path
    source=Path('supabase/migrations/20261005195617_add_manual_billing_transactions.sql').read_text()
    start=source.index('CREATE UNIQUE INDEX idx_billing_events_live_attempt_revision')
    with sqlTransaction(path) as db:
        db.executescript(source[start:source.index(';',start)+1].replace('public.',''))
    first=client.post('/createRenewalPaymentSession',json={'invoiceId':'renewal'},headers={'Idempotency-Key':'first'})
    second=client.post('/createRenewalPaymentSession',json={'invoiceId':'renewal'},headers={'Idempotency-Key':'second'})
    assert first.status_code==200,first.text
    assert second.status_code==409,second.text
    assert len(provider.order.createdPayloads)==before+1


def test_webhook_capture_after_anonymisation_is_retained(checkout_database,monkeypatch):
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from api.services.webhookService import WebhookService
    from test.test_manual_auth_coverage import SqlRestClient
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','initial_purchase')
    pending=manual.createCheckout(checkout)
    with sqlTransaction(path) as db:
        db.execute('update billing_events set user_id=null,metadata_json=\'{}\' where id=?',(pending.attemptId,))
        db.execute('update "Invoices" set "userId"=null,metadata_json=\'{}\' where id=?',(pending.invoiceId,))
    subscription=SubscriptionService.__new__(SubscriptionService)
    subscription.client,subscription.razorpayClient=SqlRestClient(path),manual.razorpayClient
    webhook=WebhookService.__new__(WebhookService);webhook.client=subscription.client
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.subscriptionService',subscription)
    webhook._handlePaymentCaptured({'payload':{'payment':{'entity':{'id':'erased-payment','order_id':pending.razorpayOrderId,'amount':pending.amount,'currency':pending.currency,'status':'captured'}}}})
    with sqlTransaction(path) as db:
        row=db.execute("select user_id,event_status from billing_events where provider_payment_id='erased-payment'").fetchone()
    assert row==(None,'REQUIRES_RECONCILIATION')


def test_expired_monthly_invoice_is_closed_and_public_history_redacted(checkout_client,checkout_database):
    client,_,path=checkout_client
    repo,_=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','renewal')
    with sqlTransaction(path) as db:
        end=db.execute('select current_period_end from subscriptions').fetchone()[0]
        db.execute('update "Invoices" set metadata_json=? where id=\'renewal\'',(json.dumps({'manualBilling':{'domains':['banking'],'backfillApprovedBy':'private-staff','closedReason':'private-reason'},'caseReference':'private-case'}),))
    from api.services.billing.manualBillingRepository import _utc
    later=_utc(end)+timedelta(seconds=1)
    with patch('api.services.billing.manualBillingRepository._now',return_value=later): repo.activateDueCoverage(USER,later)
    with sqlTransaction(path) as db: assert db.execute("select status from \"Invoices\" where id='renewal'").fetchone()[0]=='EXPIRED'
    response=client.get('/invoices')
    assert response.status_code==200,response.text
    assert 'private-staff' not in response.text and 'private-reason' not in response.text and 'private-case' not in response.text


def test_expiry_closure_preserves_attested_original_renewal_contract(checkout_database):
    from dataclasses import replace
    from api.services.billing.manualBillingRepository import _utc
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','renewal')
    intent=manual.createCheckout(checkout)
    end=_utc(intent.snapshot['periodStart'])
    observed=end+timedelta(seconds=1)
    with patch('api.services.billing.manualBillingRepository._now',return_value=observed):
        repo.activateDueCoverage(USER,observed)
        result=manual.finalizeCapturedPayment(replace(evidence(intent,'attested-before-expiry'),
            observedAt=observed,provenCaptureAt=NOW+timedelta(seconds=1),timingVerified=True))
    assert result.finalized and result.currentPeriod.start==end
    assert result.currentPeriod.end==_utc(intent.snapshot['periodEnd'])


def test_refund_provider_read_outage_is_503_without_closure(refund_client):
    client,repo,path,provider,quote=refund_client
    provider.verifyUnreturnedCapture=Mock(side_effect=TimeoutError('provider unavailable'))
    response=client.post('/refunds/initiate',json=payload(quote),headers={'Idempotency-Key':'approval'})
    assert response.status_code==503,response.text
    assert not provider.calls
    with sqlTransaction(path) as db: assert db.execute('select status from subscriptions').fetchone()[0]=='active'


def test_staff_cannot_refund_own_subscription(refund_client):
    client,repo,path,provider,quote=refund_client
    import api.routers.billingAdmin as routes
    client.app.dependency_overrides[routes.verifyBillingAdmin]=lambda:USER
    response=client.post('/refunds/initiate',json=payload(quote),headers={'Idempotency-Key':'approval'})
    assert response.status_code==403,response.text
    assert not provider.calls
