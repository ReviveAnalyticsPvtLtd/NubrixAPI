"""IR-20: replace an abandoned unpaid checkout without losing money history."""
import hashlib
import hmac
import json
import os
from unittest.mock import patch

import pytest

from api.services.billing.manualPaymentService import ManualPaymentService
from test.test_customer_free_payments import StrictFakeRazorpayClient
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database, checkout_client, counts, request
from test.test_manual_payment_entrypoints import evidence


def invoice(path, invoice_id):
    with sqlTransaction(path) as db:
        db.row_factory = __import__('sqlite3').Row
        return dict(db.execute('SELECT * FROM "Invoices" WHERE id=?', (invoice_id,)).fetchone())


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
@pytest.mark.parametrize('replacement_key', [None, 'changed-selection'])
def test_http_changed_selection_replaces_unpaid_checkout(checkout_client, mode, replacement_key):
    client, provider, path = checkout_client
    first = client.post('/createSubscription', json={
        'domains': ['banking'], 'contact': '', 'billingMode': mode},
        headers={'Idempotency-Key': 'original'}).json()
    original_invoice = invoice(path, first['invoiceId'])
    headers = {'Idempotency-Key': replacement_key} if replacement_key else {}
    payload = {'domains': ['banking', 'telecom'], 'contact': '', 'billingMode': mode}
    replacement = client.post('/createSubscription', json=payload, headers=headers)
    assert replacement.status_code == 200
    changed = replacement.json()
    assert changed['attemptId'] != first['attemptId']
    assert changed['orderId'] != first['orderId']
    assert changed['domains'] == ['banking', 'telecom']
    assert changed['amount'] > first['amount']
    assert counts(path) == (2, 2)
    assert len(provider.order.createdPayloads) == 2
    retry = client.post('/createSubscription', json=payload, headers=headers)
    assert retry.status_code == 200 and retry.json()['attemptId'] == changed['attemptId']
    closed = invoice(path, first['invoiceId'])
    assert closed['status'] == 'VOID'
    assert closed['total_amount'] == original_invoice['total_amount']
    old_metadata = json.loads(closed['metadata_json'])
    assert old_metadata['domains'] == ['banking']
    assert old_metadata['manualBilling']['closedReason'] == 'SUPERSEDED_BY_REQUEST'
    assert old_metadata['manualBilling']['closedAt']
    assert old_metadata['manualBilling']['supersededByInvoiceId'] == changed['invoiceId']
    assert json.loads(invoice(path, changed['invoiceId'])['metadata_json'])['manualBilling']['replacesInvoiceIds'] == [first['invoiceId']]
    replay = client.post('/createSubscription', json={
        'domains': ['banking'], 'contact': '', 'billingMode': mode},
        headers={'Idempotency-Key': 'original'})
    assert replay.status_code == 200
    assert replay.json()['attemptId'] == first['attemptId']
    assert replay.json()['state'] == 'cancelled'
    assert len(provider.order.createdPayloads) == 2


@pytest.mark.parametrize('channel', ['browser', 'webhook', 'worker'])
def test_old_capture_after_replacement_never_grants_access(checkout_database, checkout_client, channel):
    from types import SimpleNamespace
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.webhookService import WebhookService
    repo, path = checkout_database
    client, provider, _ = checkout_client
    original = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''}).json()
    replacement = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''})
    assert replacement.status_code == 200
    payment = {'id': 'old-captured-money', 'order_id': original['orderId'], 'status': 'captured',
               'amount': original['amount'], 'currency': original['currency']}
    provider.payments[payment['id']] = payment
    provider.paymentsByOrder[original['orderId']] = [payment]
    if channel == 'browser':
        signature = hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),
            f"{original['orderId']}|{payment['id']}".encode(), hashlib.sha256).hexdigest()
        payload = {'razorpayOrderId': original['orderId'], 'razorpayPaymentId': payment['id'],
            'razorpaySignature': signature}
        result = client.post('/verifySubscription', json=payload)
        assert result.status_code == 200
        assert result.json()['state'] == 'requires_reconciliation'
    elif channel == 'webhook':
        service = WebhookService.__new__(WebhookService)
        from test.test_manual_auth_coverage import SqlRestClient
        service.client = SqlRestClient(path)
        service._handlePaymentCaptured({'payload': {'payment': {'entity': payment}}})
    else:
        provider.order.payments = lambda order, params: {'items': provider.paymentsByOrder[order]}
        with patch('api.services.billing.manualBillingRecoveryService.datetime', SimpleNamespace(now=lambda _: NOW)):
            ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(USER, original['attemptId']))
    assert invoice(path, original['invoiceId'])['status'] == 'VOID'
    assert invoice(path, replacement.json()['invoiceId'])['status'] == 'PAYMENT_PENDING'
    assert not repo.getCoverageSnapshot(USER).accessAllowed
    with sqlTransaction(path) as db:
        captures = db.execute('SELECT event_status FROM billing_events WHERE provider_payment_id=?', (payment['id'],)).fetchall()
        assert captures == [('REQUIRES_RECONCILIATION',)]
        assert db.execute('SELECT count(*) FROM credit_balances').fetchone()[0] == 0
    new_intent = repo._intentFromAttemptRow(repo.attemptById(USER, replacement.json()['attemptId']),
        repo._json(repo.attemptById(USER, replacement.json()['attemptId'])['metadata_json']), USER, 'initial_purchase')
    activated = repo.finalizeCapturedPayment(evidence(new_intent, 'new-captured-money'))
    assert activated.finalized and activated.currentPeriod.domains == ('telecom',)
    again = repo.finalizeCapturedPayment(evidence(repo._intentFromAttemptRow(
        repo.attemptById(USER, original['attemptId']),
        repo._json(repo.attemptById(USER, original['attemptId'])['metadata_json']), USER, 'initial_purchase'), payment['id']))
    assert again.state == 'requires_reconciliation'
    assert repo.getCoverageSnapshot(USER).currentPeriod.domains == ('telecom',)


def test_replacement_failure_rolls_back_old_checkout_closure(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    repo.bindProviderOrder(first.attemptId, {'id': 'original-order'})
    with sqlTransaction(path) as db:
        db.execute("CREATE TRIGGER fail_replacement BEFORE INSERT ON billing_events BEGIN SELECT RAISE(ABORT,'replacement insert failed'); END")
    with pytest.raises(Exception, match='replacement insert failed'):
        repo.reserveCheckout(request('replacement', ('telecom',)))
    assert counts(path) == (1, 1)
    assert invoice(path, first.invoiceId)['status'] == 'PAYMENT_PENDING'
    old = repo.attemptById(USER, first.attemptId)
    assert old['payment_status'] == 'pending_provider_ack'
    assert not repo._json(old['metadata_json'])['manualBilling']['closedAt']


def test_replacement_price_outage_preserves_original(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    repo.bindProviderOrder(first.attemptId, {'id': 'original-order'})
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', side_effect=RuntimeError('price unavailable')):
        with pytest.raises(RuntimeError, match='price unavailable'):
            repo.reserveCheckout(request('replacement', ('telecom',)))
    assert invoice(path, first.invoiceId)['status'] == 'PAYMENT_PENDING'
    assert counts(path) == (1, 1)


def test_reusing_original_key_with_changed_payload_cannot_close_checkout(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        repo.reserveCheckout(request('first', ('telecom',)))
    assert invoice(path, first.invoiceId)['status'] == 'PAYMENT_PENDING'
    assert counts(path) == (1, 1)


def test_old_unbound_key_replay_cannot_create_or_recover_an_order(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    replacement = repo.reserveCheckout(request('replacement', ('telecom',)))
    provider = StrictFakeRazorpayClient()
    provider.order.all = lambda *_: pytest.fail('An unsubmitted cancelled intent must not query the provider')
    replay = ManualPaymentService.forProduction(provider, repo).createCheckout(request('first'))
    assert replay.attemptId == first.attemptId and replay.state == 'cancelled'
    assert not provider.order.createdPayloads
    assert repo.attemptById(USER, replacement.attemptId)['payment_status'] == 'created'


def test_delayed_order_binding_cannot_reopen_superseded_attempt(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    repo.reserveCheckout(request('replacement', ('telecom',)))
    bound = repo.bindProviderOrder(first.attemptId, {'id': 'delayed-original-order'})
    assert bound.state == 'cancelled'
    assert invoice(path, first.invoiceId)['status'] == 'VOID'
    result = repo.finalizeCapturedPayment(evidence(bound, 'delayed-original-payment'))
    assert result.state == 'requires_reconciliation'
    assert not repo.getCoverageSnapshot(USER).accessAllowed


def test_paid_initial_checkout_cannot_be_replaced(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    first = repo.bindProviderOrder(first.attemptId, {'id': 'paid-order'})
    repo.finalizeCapturedPayment(evidence(first))
    with pytest.raises(ValueError, match='EXISTING_PAID_COVERAGE'):
        repo.reserveCheckout(request('replacement', ('telecom',)))
    assert invoice(path, first.invoiceId)['status'] == 'PAID'
    assert repo.getCoverageSnapshot(USER).currentPeriod.domains == ('banking',)


def test_replacement_can_switch_initial_billing_mode(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first', mode='annual_prepaid'))
    repo.bindProviderOrder(first.attemptId, {'id': 'annual-order'})
    changed = repo.reserveCheckout(request('replacement', ('telecom',), mode='monthly_prepaid'))
    assert invoice(path, first.invoiceId)['status'] == 'VOID'
    assert changed.billingMode == 'monthly_prepaid'


def test_unknown_provider_creation_is_recovered_before_replacement(checkout_database):
    from test.test_manual_payment_entrypoints import RecoverableProvider
    from unittest.mock import Mock
    repo, path = checkout_database
    provider = RecoverableProvider(path)
    service = ManualPaymentService.forProduction(provider, repo)
    provider.loseAck = True
    with pytest.raises(RuntimeError):
        service.createCheckout(request('first'))
    provider.order.all = Mock(side_effect=TimeoutError('unknown old provider outcome'))
    with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
        service.createCheckout(request('replacement', ('telecom',)))
    assert counts(path) == (1, 1)
    assert len(provider.order.createdPayloads) == 1


@pytest.mark.parametrize('changed_old_payload', [False, True])
def test_old_key_identity_precedes_unrelated_replacement_recovery(checkout_database, changed_old_payload):
    from test.test_manual_payment_entrypoints import RecoverableProvider
    from unittest.mock import Mock
    repo, path = checkout_database
    provider = RecoverableProvider(path)
    service = ManualPaymentService.forProduction(provider, repo)
    original = service.createCheckout(request('original'))
    provider.loseAck = True
    with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
        service.createCheckout(request('changed', ('telecom',)))
    provider.order.all = Mock(side_effect=TimeoutError('replacement lookup is down'))
    if changed_old_payload:
        with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
            service.createCheckout(request('original', ('manufacturing',)))
    else:
        replay = service.createCheckout(request('original'))
        assert replay.attemptId == original.attemptId and replay.state == 'cancelled'
    provider.order.all.assert_not_called()
    assert counts(path) == (2, 2)
    assert len(provider.order.createdPayloads) == 2
