"""Pay replaces an earlier initial checkout automatically when its Order shows no moving money."""
import hashlib
import hmac
import json
import os
from datetime import timedelta
from unittest.mock import patch

import pytest

from api.services.billing.manualPaymentService import ManualPaymentService
from test.test_customer_free_payments import StrictFakeRazorpayClient
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database, checkout_client, counts, request
from test.test_manual_payment_entrypoints import evidence


def empty(intent, order, observed=NOW):
    """A complete, fresh provider listing of an Order with no payments, taken before the owner lock."""
    from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
    return CheckoutProviderSnapshot(intent.attemptId, order, 'unpaid', observed, True, intent.amount, intent.currency, ())


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
    assert old_metadata['manualBilling']['closedReason'] == 'SUPERSEDED'
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
        repo.reserveCheckout(request('replacement', ('telecom',)), inspection=empty(first, 'original-order'))
    assert counts(path) == (1, 1)
    assert invoice(path, first.invoiceId)['status'] == 'PAYMENT_PENDING'
    old = repo.attemptById(USER, first.attemptId)
    assert old['payment_status'] == 'pending_provider_ack'
    assert not repo._json(old['metadata_json'])['manualBilling']['closedAt']


def test_replacement_price_outage_preserves_original(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    repo.bindProviderOrder(first.attemptId, {'id': 'original-order'})
    with patch('api.services.billing.pricingCatalogRepository.getPricingCatalogRepository') as catalog_factory:
        catalog_factory.return_value.loadCurrent.side_effect = RuntimeError('price unavailable')
        with pytest.raises(RuntimeError, match='price unavailable'):
            repo.reserveCheckout(request('replacement', ('telecom',)), inspection=empty(first, 'original-order'))
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
    changed = repo.reserveCheckout(request('replacement', ('telecom',), mode='monthly_prepaid'),
                                   inspection=empty(first, 'annual-order'))
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


def test_order_backed_live_attempt_without_provider_evidence_is_busy(checkout_database):
    repo, path = checkout_database
    first = repo.reserveCheckout(request('first'))
    repo.bindProviderOrder(first.attemptId, {'id': 'original-order'})
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        repo.reserveCheckout(request('replacement', ('telecom',)))
    stale = empty(first, 'original-order', NOW - timedelta(seconds=61))
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        repo.reserveCheckout(request('replacement', ('telecom',)), inspection=stale)
    assert counts(path) == (1, 1) and invoice(path, first.invoiceId)['status'] == 'PAYMENT_PENDING'


@pytest.mark.parametrize('payment_status,outcome', [
    ('created', 'replaced'), ('failed', 'replaced'), ('authorized', 'confirming'), ('captured', 'replaced')])
@pytest.mark.parametrize('domains', [('banking',), ('telecom',)])
def test_lapsed_annual_purchase_money_decides_pay(checkout_database, payment_status, outcome, domains):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from test.test_manual_payment_entrypoints import RecoverableProvider
    repo, path = checkout_database
    provider = RecoverableProvider(path)
    service = ManualPaymentService.forProduction(provider, repo)
    original = service.createCheckout(request('original', mode='annual_prepaid'))
    later = NOW + timedelta(hours=1)
    provider.order.orders[original.razorpayOrderId]['status'] = 'paid' if payment_status == 'captured' else 'attempted'
    provider.paymentsByOrder[original.razorpayOrderId] = [{
        'id': 'pending-annual-payment', 'order_id': original.razorpayOrderId, 'created_at': int((later - timedelta(minutes=5)).timestamp()),
        'status': payment_status, 'amount': original.amount, 'currency': original.currency}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(USER, original.attemptId))
        result = service.createCheckout(request('another-browser', domains, mode='annual_prepaid'))
    if outcome == 'confirming':
        # A payment the bank approved is seconds from capture: nothing new opens.
        assert result.state == 'confirming_earlier_payment' and result.attemptId == original.attemptId
        assert counts(path) == (1, 1) and len(provider.order.createdPayloads) == 1
        assert invoice(path, original.invoiceId)['status'] == 'PAYMENT_PENDING'
        return
    # A started-but-unapproved, failed or already-finalized payment never holds Pay.
    assert result.attemptId != original.attemptId and result.razorpayOrderId != original.razorpayOrderId
    assert counts(path) == (2, 2) and len(provider.order.createdPayloads) == 2
    assert invoice(path, original.invoiceId)['status'] == 'VOID'
    retained = repo.attemptById(USER, original.attemptId)
    assert retained['provider_order_id'] == original.razorpayOrderId
    assert repo._json(retained['metadata_json'])['manualBilling']['closedReason'] == 'SUPERSEDED'
    if payment_status == 'captured':
        with sqlTransaction(path) as db:
            assert db.execute("SELECT event_status FROM billing_events WHERE provider_payment_id='pending-annual-payment'").fetchall() == [
                ('REQUIRES_RECONCILIATION',)]


def test_lapsed_annual_purchase_with_empty_order_is_replaced_from_a_fresh_listing(checkout_database):
    from test.test_manual_payment_entrypoints import RecoverableProvider
    repo, path = checkout_database
    provider = RecoverableProvider(path)
    service = ManualPaymentService.forProduction(provider, repo)
    original = service.createCheckout(request('original', mode='annual_prepaid'))
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        replacement = service.createCheckout(request('another-browser', mode='annual_prepaid'))
    assert replacement.attemptId != original.attemptId and replacement.razorpayOrderId != original.razorpayOrderId
    assert repo.attemptById(USER, original.attemptId)['payment_status'] == 'cancelled'
    assert invoice(path, original.invoiceId)['status'] == 'VOID'
    assert counts(path) == (2, 2) and len(provider.order.createdPayloads) == 2


# -- money in flight elsewhere in the slot decides every Pay path (spec 4.3 step 3, 5 cases 1, 4, 4a) --

def _service(path, repo):
    from test.test_manual_payment_entrypoints import RecoverableProvider
    return ManualPaymentService.forProduction(RecoverableProvider(path), repo)


def _authorizeOn(service, intent, created=NOW):
    """The provider shows an authorized payment on the attempt's Order and the recovery scan records it."""
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    provider = service.razorpayClient
    provider.order.orders[intent.razorpayOrderId]['status'] = 'attempted'
    provider.paymentsByOrder[intent.razorpayOrderId] = [{
        'id': 'authorized-' + intent.attemptId, 'order_id': intent.razorpayOrderId, 'status': 'authorized',
        'amount': intent.amount, 'currency': intent.currency, 'created_at': int(created.timestamp())}]
    ManualBillingRecoveryService(service.repository, provider).recoverAttempt(
        service.repository.attemptById(USER, intent.attemptId))


def test_late_authorization_on_a_superseded_attempt_holds_reuse_and_replacement(checkout_database):
    repo, path = checkout_database
    service = _service(path, repo)
    first = service.createCheckout(request('first'))
    live = service.createCheckout(request('second', ('telecom',)))
    assert live.attemptId != first.attemptId and live.razorpayOrderId
    # A payment on the superseded Order late-authorized after the replacement opened.
    _authorizeOn(service, first)
    earlier = {'selectionLabel': 'Pro monthly with Banking', 'amount': first.amount, 'currency': first.currency}
    for key, domains in (('same-as-live', ('telecom',)), ('changed-again', ('manufacturing',))):
        held = service.createCheckout(request(key, domains))
        assert held.state == 'confirming_earlier_payment', key
        assert held.attemptId == first.attemptId and held.earlierPayment == earlier, key
    # Nothing was reused, closed or opened while that money may still capture.
    assert repo.attemptById(USER, live.attemptId)['payment_status'] == 'pending_provider_ack'
    assert not repo._json(repo.attemptById(USER, live.attemptId)['metadata_json'])['manualBilling'].get('closedAt')
    assert counts(path) == (2, 2) and len(service.razorpayClient.order.createdPayloads) == 2


def test_open_pay_waits_for_money_in_flight_on_a_closed_attempt(checkout_database):
    repo, path = checkout_database
    service = _service(path, repo)
    first = service.createCheckout(request('first'))
    _authorizeOn(service, first)
    attempt = repo.attemptById(USER, first.attemptId)
    metadata = repo._json(attempt['metadata_json'])
    metadata['manualBilling'].update(closedAt=NOW.isoformat(), closedReason='CHECKOUT_EXPIRED')
    with sqlTransaction(path) as db:
        db.execute("UPDATE billing_events SET payment_status='expired',metadata_json=? WHERE id=?",
                   (json.dumps(metadata), first.attemptId))
    held = service.createCheckout(request('fresh', ('telecom',)))
    assert held.state == 'confirming_earlier_payment' and held.attemptId == first.attemptId
    assert held.earlierPayment == {'selectionLabel': 'Pro monthly with Banking', 'amount': first.amount,
                                   'currency': first.currency}
    assert counts(path) == (1, 1) and len(service.razorpayClient.order.createdPayloads) == 1


def test_superseded_scan_lasts_until_the_old_checkout_can_no_longer_start_a_payment(checkout_database):
    repo, path = checkout_database
    service = _service(path, repo)
    first = service.createCheckout(request('first'))
    assert first.expiresAt == NOW + timedelta(minutes=30)
    service.createCheckout(request('second', ('telecom',)))
    # Closed at NOW, but its Checkout modal could still start a payment until expiresAt.
    for minutes, reconciled in ((50, False), (74, False), (76, True)):
        at = NOW + timedelta(minutes=minutes)
        with patch('api.services.billing.manualBillingRepository._now', return_value=at):
            assert repo.recordClosedAttemptScan(first.attemptId, empty(first, first.razorpayOrderId, at)) is reconciled


def test_unavailable_slot_is_refused_before_anything_is_retired_or_written(checkout_database, monkeypatch):
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    repo, path = checkout_database
    before = counts(path)
    monkeypatch.setattr(ManualBillingRepository, '_requestSlotLocked', lambda self, *args: ('pro_term', None))
    def retire(*args):
        raise AssertionError('nothing may be retired without a payable slot')
    monkeypatch.setattr(ManualBillingRepository, '_retireReplacedLocked', retire)
    with pytest.raises(ValueError, match='PAYMENT_SLOT_UNAVAILABLE'):
        repo.reserveCheckout(request('first'))
    assert counts(path) == before


@pytest.mark.parametrize('order', ['capture_first', 'replace_first'])
def test_expert_addition_replacement_and_capture_never_grant_twice(checkout_database, order, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from test.test_manual_payment_entrypoints import evidence, paid_then_request, read_row
    repo, path = checkout_database
    service, addition = paid_then_request(checkout_database, 'monthly_prepaid', 'expert_addition')
    first = service.createCheckout(addition)
    provider = service.razorpayClient
    def capture():
        provider.order.orders[first.razorpayOrderId]['status'] = 'paid'
        provider.paymentsByOrder[first.razorpayOrderId] = [{
            'id': 'addition-payment', 'order_id': first.razorpayOrderId, 'status': 'captured',
            'amount': first.amount, 'currency': first.currency, 'created_at': int(NOW.timestamp())}]
        return repo.finalizeCapturedPayment(evidence(first, 'addition-payment'))
    changed = request('changed-set', ('supplychain',), purpose='expert_addition')
    captured = None
    if order == 'capture_first':
        inspect = ManualBillingRecoveryService.inspectAttempt
        def inspectThenCapture(self, attempt):
            nonlocal captured
            snapshot = inspect(self, attempt)  # the empty listing Pay decides from
            captured = capture()               # the capture lands before Pay takes the owner lock
            return snapshot
        monkeypatch.setattr(ManualBillingRecoveryService, 'inspectAttempt', inspectThenCapture)
        result = service.createCheckout(changed)
        assert captured.finalized and result.attemptId == first.attemptId and result.state == 'captured'
    else:
        result = service.createCheckout(changed)
        assert result.attemptId != first.attemptId and result.razorpayOrderId
        captured = capture()
        assert not captured.finalized
    experts = read_row(path, 'subscriptions')['subscribed_experts']
    experts = json.loads(experts) if isinstance(experts, str) else experts
    assert ('telecom' in experts) == (order == 'capture_first') and 'supplychain' not in experts
    with sqlTransaction(path) as db:
        # The payment is never lost: exactly one capture record, granted or awaiting refund.
        assert db.execute("SELECT count(*) FROM billing_events WHERE provider_payment_id='addition-payment' "
                          "AND event_type='payment.capture'").fetchone()[0] == 1
