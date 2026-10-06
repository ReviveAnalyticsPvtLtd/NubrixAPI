from datetime import timedelta
from unittest.mock import patch

import pytest
from test.test_manual_billing_runtime import database, USER, NOW, read_row
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request, evidence


def subscription_service(repository, monkeypatch):
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from unittest.mock import Mock
    service = SubscriptionService.__new__(SubscriptionService)
    service.razorpayClient = Mock()
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repository)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda: NOW)
    return service


def test_checkout_returns_persisted_configured_deadline(checkout_database, monkeypatch):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    repository, _ = checkout_database
    monkeypatch.setenv('MANUAL_CHECKOUT_TTL_SECONDS', '600')
    manual, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    intent = manual.createCheckout(request)
    result = serializeCheckoutIntent(intent, 'public-key')
    assert result['expiresAt'] == intent.expiresAt.isoformat() == (NOW+timedelta(seconds=600)).isoformat()
    assert result['razorpayOrderId'] == result['orderId'] == intent.razorpayOrderId
    assert result['razorpayKeyId'] == result['razorpayKey'] == 'public-key'


def test_early_renewal_replay_retains_next_period(checkout_database, monkeypatch):
    repository, path = checkout_database
    manual, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    intent = manual.createCheckout(request)
    service = subscription_service(repository, monkeypatch)
    payment = {'id':'future-payment', 'order_id':intent.razorpayOrderId,
        'amount':intent.amount, 'currency':intent.currency, 'status':'captured'}
    first = service._finalizeManualCheckout(intent.invoiceId,intent.razorpayOrderId,'future-payment',payment)
    replay = service._finalizeManualCheckout(intent.invoiceId,intent.razorpayOrderId,'future-payment',payment)
    assert first['nextPeriod'] == replay['nextPeriod'] and replay['nextPeriod'] is not None
    assert first['currentPeriod'] == replay['currentPeriod'] and replay['currentPeriod'] is not None
    assert replay['invoiceId'] == intent.invoiceId and replay['attemptId'] == intent.attemptId
    assert replay['creditState'] == 'ready' and not replay['creditsRefilled']


def test_annual_checkout_does_not_expose_internal_sentinel(checkout_database):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    manual, request = paid_then_request(checkout_database,'annual_prepaid','initial_purchase')
    result = serializeCheckoutIntent(manual.createCheckout(request),'public-key')
    assert result['expiresAt'] is None


def test_monthly_cancellation_trims_reason_and_limits_length(checkout_database):
    repository, _ = checkout_database
    manual, request = paid_then_request(checkout_database,'monthly_prepaid','topup')
    with pytest.raises(ValueError, match='INVALID_CANCELLATION_REASON'):
        repository.setRenewalOptOut(USER, True, 'x'*1001, 'long')
    row = repository.setRenewalOptOut(USER, True, '  done  ', 'cancel')
    assert row['cancellation_reason'] == 'done'
    assert row['finalPaidEnd'] == row['current_period_end']
