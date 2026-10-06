"""Production SQL assertions replacing tests of retired RPC/customer write paths."""
import json
from datetime import datetime,timedelta
from unittest.mock import patch,Mock
import pytest
from test.test_manual_billing_runtime import database,USER,NOW,read_row,sqlTransaction
from test.test_manual_checkout_http import checkout_database,checkout_client,request
from test.test_manual_payment_entrypoints import paid_then_request,evidence


@pytest.mark.parametrize('mode',['monthly_prepaid','annual_prepaid'])
def test_initializer_preserves_usage_topups_and_committed_allocation(checkout_database,monkeypatch,mode):
    repo,path=checkout_database
    paid_then_request(checkout_database,mode,'topup')
    with sqlTransaction(path) as db:
        db.execute('UPDATE credit_balances SET used_tokens=500,remaining_tokens=remaining_tokens-500,topup_tokens=123')
    before=read_row(path,'credit_balances')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    from api.services.credits.creditService import CreditService
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        balance=CreditService().initializeCreditBalance(USER,'annual' if mode=='annual_prepaid' else 'pro')
    for key in ('used_tokens','remaining_tokens','topup_tokens','credit_period_id','domain_count'):
        assert balance[key]==before[key]


def test_trial_initializer_allocates_once_without_topups(database,monkeypatch):
    repo,path=database
    repo.activateTrial(USER,('banking',))
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    from api.services.credits.creditService import CreditService
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        service=CreditService()
        first=service.initializeCreditBalance(USER,'free')
        second=service.initializeCreditBalance(USER,'free')
    assert first['credit_period_id']==second['credit_period_id'] and second['topup_tokens']==0


def test_initializer_three_experts_reads_frozen_paid_quota(checkout_database,monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from test.test_customer_free_payments import StrictFakeRazorpayClient
    repo,path=checkout_database
    intent=ManualPaymentService.forProduction(StrictFakeRazorpayClient(),repo).createCheckout(request(domains=('banking','telecom','manufacturing')))
    repo.finalizeCapturedPayment(evidence(intent))
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    from api.services.credits.creditService import CreditService
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        balance=CreditService().initializeCreditBalance(USER,'pro',domainCount=3)
    assert balance['domain_count']==3 and balance['monthly_token_quota']==30000000


@pytest.mark.parametrize('purpose',['initial_purchase','renewal'])
def test_webhook_recovers_owned_checkout_without_notes(checkout_database,monkeypatch,purpose):
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid',purpose)
    intent=manual.createCheckout(checkout)
    payment={'id':'webhook-no-notes','order_id':intent.razorpayOrderId,'amount':intent.amount,'currency':intent.currency,'status':'captured'}
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    from api.services.webhookService import WebhookService
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow',lambda:NOW)
    webhook=WebhookService.__new__(WebhookService)
    webhook._handlePaymentCaptured({'payload':{'payment':{'entity':payment}}})
    webhook._handlePaymentCaptured({'payload':{'payment':{'entity':payment}}})
    with sqlTransaction(path) as db:
        assert db.execute('SELECT status FROM "Invoices" WHERE id=?',(intent.invoiceId,)).fetchone()[0]=='PAID'
        assert db.execute("SELECT count(*) FROM billing_events WHERE provider_payment_id='webhook-no-notes'").fetchone()[0]==1


def test_initial_purchase_rejects_opted_out_but_unexpired_service(checkout_client,checkout_database):
    client,_,_=checkout_client
    repo,_=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    repo.setRenewalOptOut(USER,True,'Finished project','cancel')
    response=client.post('/createSubscription',json={'domains':['banking'],'contact':''})
    assert response.status_code==409


def test_initial_purchase_allows_fresh_lifecycle_after_expiry(checkout_client,checkout_database):
    client,_,path=checkout_client
    manual,_=paid_then_request(checkout_database,'monthly_prepaid','topup')
    import api.routers.subscriptions as routes
    routes.subscriptionService.razorpayClient=manual.razorpayClient
    with patch('test.test_manual_billing_runtime.NOW',NOW+timedelta(days=32)):
        response=client.post('/createSubscription',json={'domains':['banking'],'contact':''})
    assert response.status_code==200


@pytest.mark.parametrize('mode',['monthly_prepaid','annual_prepaid'])
def test_renewal_invoice_must_belong_to_current_canonical_subscription(checkout_database,mode):
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,mode,'renewal')
    with sqlTransaction(path) as db:
        db.execute("UPDATE \"Invoices\" SET subscription_id='historical-other' WHERE id='renewal'")
    with pytest.raises(ValueError,match='OWNED_INVOICE_NOT_FOUND'):
        manual.createCheckout(checkout)


@pytest.mark.parametrize('case',['amount','annual_metadata'])
def test_actual_initial_verification_checks_amount_and_preserves_snapshot(checkout_database,monkeypatch,case):
    from test.test_topup_durable_verification import topup_verification
    # Every mode/purpose browser verification is additionally tested in the entrypoint matrix.
    mode='annual_prepaid' if case=='annual_metadata' else 'monthly_prepaid'
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,mode,'initial_purchase')
    intent=manual.createCheckout(checkout)
    with sqlTransaction(path) as db:
        before=json.loads(db.execute('SELECT metadata_json FROM "Invoices" WHERE id=?',(intent.invoiceId,)).fetchone()[0])
    if case=='amount':
        from dataclasses import replace
        with pytest.raises(ValueError,match='EVIDENCE_MISMATCH'):
            repo.finalizeCapturedPayment(replace(evidence(intent),amount=intent.amount+1))
        assert read_row(path,'credit_balances') is None
    else:
        result=repo.finalizeCapturedPayment(evidence(intent))
        after=json.loads(read_row(path,'Invoices')['metadata_json'])
        assert result.creditsRefilled and result.currentPeriod.billingMode=='annual_prepaid'
        for key in ('pricingSnapshot','taxSnapshot'):
            if key in before: assert after[key]==before[key]
        assert after['manualBilling']['domains']==before['manualBilling']['domains']
