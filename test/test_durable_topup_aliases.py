import pytest
from test.test_manual_checkout_http import checkout_database
from test.test_manual_billing_runtime import database,USER,NOW,read_row
from test.test_manual_payment_entrypoints import paid_then_request


def test_retained_grant_and_clawback_aliases_use_owned_durable_identity(checkout_database,monkeypatch):
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from api.services.credits.creditService import CreditService
    from test.test_manual_auth_coverage import SqlRestClient
    repo,path=checkout_database
    manual,request=paid_then_request(checkout_database,'monthly_prepaid','topup')
    intent=manual.createCheckout(request)
    provider=manual.razorpayClient
    payment={'id':'alias-payment','order_id':intent.razorpayOrderId,'status':'captured',
        'amount':intent.amount,'currency':intent.currency}
    provider.payments[payment['id']]=payment
    subscription=SubscriptionService.__new__(SubscriptionService)
    subscription.client,subscription.razorpayClient=SqlRestClient(path),provider
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.subscriptionService',subscription)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow',lambda:NOW)
    credits=CreditService()
    with pytest.raises(ValueError,match='TOPUP_OWNER_OR_PURPOSE_MISMATCH'):
        credits.grantTopupTokens('another-owner',intent.razorpayOrderId,payment['id'])
    assert credits.grantTopupTokens(USER,intent.razorpayOrderId,payment['id'])['granted']
    assert not credits.grantTopupTokens(USER,intent.razorpayOrderId,payment['id'])['granted']
    assert read_row(path,'credit_balances')['topup_tokens']==5000000
    assert credits.clawbackTopupTokens(USER,'alias-refund',payment['id'],intent.amount)['clawed']
    assert not credits.clawbackTopupTokens(USER,'alias-refund',payment['id'],intent.amount)['clawed']
    assert read_row(path,'credit_balances')['topup_tokens']==0
    assert read_row(path,'subscriptions')['status']=='active'
