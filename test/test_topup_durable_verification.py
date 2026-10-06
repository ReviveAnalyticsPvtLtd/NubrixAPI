"""Production security and replay regressions replacing the retired RPC-grant mocks."""
import hashlib, hmac, os
from dataclasses import replace
import pytest
from jose import jwt
from test.test_manual_billing_runtime import database, USER, NOW, read_row, sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request


@pytest.fixture
def topup_verification(checkout_database, monkeypatch):
    from api.services.credits.topupService import TopupService
    repository,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','topup')
    intent=manual.createCheckout(checkout)
    provider=manual.razorpayClient
    payment={'id':'topup-payment','order_id':intent.razorpayOrderId,'status':'captured',
        'amount':intent.amount,'currency':intent.currency,'notes':{}}
    provider.payments[payment['id']]=payment
    service=TopupService()
    service.client,service.razorpayClient=object(),provider
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow',lambda:NOW)
    token=jwt.encode({'userId':USER,'email':'topup@example.test'},os.environ['SECRET_KEY'],algorithm='HS256')
    signature=hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),
        f'{intent.razorpayOrderId}|topup-payment'.encode(),hashlib.sha256).hexdigest()
    return service,token,{'razorpayOrderId':intent.razorpayOrderId,'razorpayPaymentId':payment['id'],
        'razorpaySignature':signature},path,payment,repository


def test_valid_payment_grants_tokens(topup_verification):
    service,token,payload,path,_,_=topup_verification
    result=service.verifyTopupPayment(payload,token)
    assert result['granted'] and result['tokens']==5000000 and result['credits']==500
    assert read_row(path,'credit_balances')['topup_tokens']==5000000


def test_tampered_signature_is_rejected_before_any_grant(topup_verification):
    service,token,payload,path,_,_=topup_verification
    with pytest.raises(Exception,match='INVALID_CHECKOUT_SIGNATURE'):
        service.verifyTopupPayment({**payload,'razorpaySignature':'deadbeef'},token)
    assert read_row(path,'credit_balances')['topup_tokens']==0


def test_order_belonging_to_another_user_is_rejected(topup_verification):
    service,_,payload,path,_,_=topup_verification
    token=jwt.encode({'userId':'other-account'},os.environ['SECRET_KEY'],algorithm='HS256')
    with pytest.raises(Exception,match='another account') as error:
        service.verifyTopupPayment(payload,token)
    assert error.value.statusCode == 403
    assert read_row(path,'credit_balances')['topup_tokens']==0


def test_non_topup_order_is_rejected(topup_verification):
    import json
    service,token,payload,path,_,_=topup_verification
    with sqlTransaction(path) as connection:
        row=connection.execute('SELECT id,metadata_json FROM billing_events WHERE provider_order_id=? AND event_category=\'payment_attempt\'',(payload['razorpayOrderId'],)).fetchone()
        metadata=json.loads(row[1]);metadata['manualBilling']['purpose']='renewal'
        connection.execute('UPDATE billing_events SET metadata_json=? WHERE id=?',(json.dumps(metadata),row[0]))
    with pytest.raises(Exception,match='CHECKOUT_PURPOSE_OR_MODE_MISMATCH'):
        service.verifyTopupPayment(payload,token)
    assert read_row(path,'credit_balances')['topup_tokens']==0


def test_missing_fields_are_rejected(topup_verification):
    service,token,_,path,_,_=topup_verification
    with pytest.raises(Exception,match='INVALID_VERIFICATION_FIELDS'):
        service.verifyTopupPayment({'razorpayOrderId':'anything'},token)
    assert read_row(path,'credit_balances')['topup_tokens']==0


def test_webhook_already_granted_reports_gracefully(topup_verification):
    service,token,payload,path,payment,repository=topup_verification
    from api.services.billing.manualPaymentService import ManualPaymentService
    attempt=repository.attemptForOrder(payload['razorpayOrderId'])
    repository.finalizeCapturedPayment(ManualPaymentService.buildPaymentEvidence(payment,attemptId=attempt['id'],
        invoiceId=attempt['invoice_id'],userId=USER,purpose='topup',observedAt=NOW))
    result=service.verifyTopupPayment(payload,token)
    assert not result['granted'] and result['alreadyFinalized']
    assert read_row(path,'credit_balances')['topup_tokens']==5000000


def test_topup_capture_without_notes_uses_local_owned_attempt(topup_verification, monkeypatch):
    from api.services.webhookService import WebhookService
    from api.services.subscriptions.subscriptionService import SubscriptionService
    service,_,_,path,payment,_=topup_verification
    subscription=SubscriptionService.__new__(SubscriptionService)
    subscription.razorpayClient=service.razorpayClient
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.subscriptionService',subscription)
    WebhookService.__new__(WebhookService)._handlePaymentCaptured({'payload':{'payment':{'entity':payment}}})
    assert read_row(path,'credit_balances')['topup_tokens']==5000000


def test_unknown_capture_is_audited_without_grant(topup_verification):
    from api.services.webhookService import WebhookService
    _,_,_,path,payment,_=topup_verification
    WebhookService.__new__(WebhookService)._handlePaymentCaptured({'payload':{'payment':{'entity':{**payment,'order_id':'unknown'}}}})
    assert read_row(path,'credit_balances')['topup_tokens']==0
    with sqlTransaction(path) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE event_type='payment.unmapped'").fetchone()[0]==1
