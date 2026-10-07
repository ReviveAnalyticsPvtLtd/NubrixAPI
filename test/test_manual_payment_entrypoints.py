"""Production orchestration with SQL authority and strict provider transport."""
from datetime import timedelta
from dataclasses import replace
from unittest.mock import patch

import pytest

from test.test_manual_billing_runtime import database, USER, NOW, read_row, sqlTransaction
from test.test_manual_checkout_http import checkout_database, request
from test.test_customer_free_payments import StrictFakeRazorpayClient
from api.services.billing.manualPaymentService import ManualPaymentService
from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence


def paid_then_request(checkout_database, mode, purpose):
    import json
    from dateutil.relativedelta import relativedelta
    repository, path = checkout_database
    service = ManualPaymentService.forProduction(RecoverableProvider(path), repository)
    if purpose != 'initial_purchase':
        initial = service.createCheckout(request('initial', mode=mode))
        service.finalizeCapturedPayment(evidence(initial, 'initial-payment'))
        if purpose == 'renewal':
            sub = read_row(path, 'subscriptions')
            start = sub['current_period_end']
            from api.services.billing.manualBillingRepository import _utc
            end = _utc(start) + (relativedelta(years=1) if mode == 'annual_prepaid' else relativedelta(months=1))
            metadata = {'manualBilling': {'domains': ['banking'], 'revision': 1}}
            with sqlTransaction(path) as connection:
                connection.execute('''INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,
                    total_amount,currency,period_start,period_end,metadata_json)
                    VALUES('renewal',?,?,'UPCOMING','renewal',10000,'INR',?,?,?)''',
                    (USER, sub['id'], start, end.isoformat(), json.dumps(metadata)))
    kwargs = {'invoiceId': 'renewal'} if purpose == 'renewal' else {'packId': 'medium'} if purpose == 'topup' else {}
    domains = ('telecom',) if purpose == 'expert_addition' else ('banking',)
    return service, request('next-payment', mode=mode, purpose=purpose, domains=domains, **kwargs)


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
def test_all_production_purposes_reserve_before_provider_call(checkout_database, mode, purpose):
    service, checkout = paid_then_request(checkout_database, mode, purpose)
    intent = service.createCheckout(checkout)
    result = service.finalizeCapturedPayment(evidence(intent))
    assert result.finalized
    _, path = checkout_database
    replay = service.finalizeCapturedPayment(evidence(intent))
    assert replay.state == 'already_finalized' and not replay.creditsRefilled
    if purpose == 'topup':
        assert read_row(path, 'credit_balances')['topup_tokens'] == 5000000
    if purpose == 'renewal':
        assert result.creditsRefilled == (mode == 'annual_prepaid')


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
@pytest.mark.parametrize('first', ['browser', 'webhook', 'worker'])
@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
def test_browser_webhook_worker_share_financial_identity(checkout_database, monkeypatch, mode, first, purpose):
    import hashlib, hmac, os
    from jose import jwt
    from unittest.mock import Mock
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from api.services.webhookService import WebhookService
    from api.services.credits.topupService import TopupService
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from test.test_manual_auth_coverage import SqlRestClient
    repository, path = checkout_database
    manual, checkout = paid_then_request(checkout_database, mode, purpose)
    provider = manual.razorpayClient
    intent = manual.createCheckout(checkout)
    payment = {'id': 'channel-payment', 'order_id': intent.razorpayOrderId,
        'amount': intent.amount, 'currency': intent.currency, 'status': 'captured',
        'notes': provider.order.orders[intent.razorpayOrderId]['notes']}
    provider.payments[payment['id']] = payment
    provider.paymentsByOrder[intent.razorpayOrderId] = [payment]
    subscription = SubscriptionService.__new__(SubscriptionService)
    subscription.client, subscription.razorpayClient = SqlRestClient(path), provider
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repository)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.subscriptionService', subscription)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda: NOW)
    token = jwt.encode({'userId': USER, 'email': 'channel@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    with sqlTransaction(path) as connection:
        connection.execute('''CREATE TABLE "Sessions"("userId" TEXT,email TEXT,"accessToken" TEXT UNIQUE,
            "sessionStartTime" TEXT,"expiresAt" TEXT,"lastActivity" TEXT,"createdAt" TEXT)''')
        connection.execute('INSERT INTO "Sessions"("userId",email,"accessToken") VALUES(?,?,?)',
            (USER, 'channel@example.test', token))
    signature = hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),
        f'{intent.razorpayOrderId}|channel-payment'.encode(), hashlib.sha256).hexdigest()
    topup = TopupService()
    topup.client, topup.razorpayClient = subscription.client, provider
    verify = {'initial_purchase': subscription.verifySubscription, 'renewal': subscription.verifyRenewalPayment,
        'expert_addition': subscription.verifyDomainUpgrade,
        'topup': topup.verifyTopupPayment}[purpose]
    browser = lambda: verify({'razorpayOrderId': intent.razorpayOrderId,
        'razorpayPaymentId': 'channel-payment', 'razorpaySignature': signature, 'invoiceId': intent.invoiceId}, token)
    webhook = WebhookService.__new__(WebhookService)
    webhook.client = subscription.client
    worker = ManualBillingRecoveryService(repository, provider)
    expectedVersion = 1 if purpose == 'initial_purchase' or (purpose == 'renewal' and mode == 'monthly_prepaid') else 2
    def assert_committed():
        with sqlTransaction(path) as connection:
            assert connection.execute('SELECT status FROM "Invoices" WHERE id=?', (intent.invoiceId,)).fetchone()[0] == 'PAID'
        assert read_row(path, 'credit_balances')['balance_version'] == expectedVersion
    with patch('api.services.billing.manualBillingRecoveryService.datetime', Mock(now=lambda _: NOW)):
        operations = {'browser': browser, 'webhook': lambda: webhook._handlePaymentCaptured(
            {'payload': {'payment': {'entity': payment}}}),
            'worker': lambda: worker.recoverAttempt(repository.attemptById(USER, intent.attemptId))}
        operations[first]()
        assert_committed()
        for operation in operations.values():
            operation()
    assert_committed()
    with sqlTransaction(path) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE provider_payment_id='channel-payment'").fetchone()[0] == 1


def evidence(intent, payment='verified-payment'):
    return VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, intent.userId,
        intent.razorpayOrderId, payment, intent.purpose, intent.currency, 'captured',
        'server_observation', intent.amount, NOW, NOW, None, True)


class RecoverableProvider(StrictFakeRazorpayClient):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.loseAck = False
        original = self.order.create
        def create(payload):
            with sqlTransaction(path) as connection:
                row = connection.execute('SELECT payment_status FROM billing_events WHERE id=?', (payload['receipt'],)).fetchone()
            assert row == ('pending_provider_ack',)
            order = original(payload)
            order.update(receipt=payload['receipt'], notes=payload['notes'])
            if self.loseAck:
                self.loseAck = False
                raise TimeoutError('provider accepted order, acknowledgement lost')
            return order
        self.order.create = create
        self.order.all = lambda params: {'items': [order for order in self.order.orders.values() if order['receipt'] == params['receipt']]}
        self.order.payments = lambda order, params=None: {'items': self.paymentsByOrder.get(order, [])}


def test_default_service_uses_production_repository():
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    assert isinstance(ManualPaymentService().repository, ManualBillingRepository)


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_production_reserves_before_provider_call(checkout_database, mode):
    repository, path = checkout_database
    provider = RecoverableProvider(path)
    service = ManualPaymentService.forProduction(provider, repository)
    intent = service.createCheckout(request('production', mode=mode))
    assert intent.razorpayOrderId
    assert len(provider.order.createdPayloads) == 1


def test_order_ack_loss_recovers_original_order(checkout_database):
    repository, path = checkout_database
    provider = RecoverableProvider(path)
    provider.loseAck = True
    service = ManualPaymentService.forProduction(provider, repository)
    with pytest.raises(Exception):
        service.createCheckout(request('lost-ack'))
    recovered = service.createCheckout(request('lost-ack'))
    assert recovered.razorpayOrderId == 'order_1'
    assert len(provider.order.createdPayloads) == 1


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_shared_finalizer_rolls_back_all_grants_on_crash(checkout_database, mode):
    repository, path = checkout_database
    service = ManualPaymentService.forProduction(RecoverableProvider(path), repository)
    intent = service.createCheckout(request('crash', mode=mode))
    with patch.object(repository, '_recordNotification', side_effect=RuntimeError('transaction failure')):
        with pytest.raises(RuntimeError):
            service.finalizeCapturedPayment(evidence(intent))
    assert read_row(path, 'Invoices')['status'] == 'PAYMENT_PENDING'
    assert read_row(path, 'credit_balances') is None
    with sqlTransaction(path) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE provider_payment_id='verified-payment'").fetchone()[0] == 0


def test_delayed_failure_cannot_downgrade_captured_payment(checkout_database):
    repository, path = checkout_database
    service = ManualPaymentService.forProduction(RecoverableProvider(path), repository)
    intent = service.createCheckout(request('capture'))
    first = service.finalizeCapturedPayment(evidence(intent))
    service.finalizeCapturedPayment(replace(evidence(intent), financialStatus='failed'))
    assert first.finalized and read_row(path, 'Invoices')['status'] == 'PAID'
