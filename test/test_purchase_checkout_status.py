"""The owner's payment status refresh observes provider money without opening another checkout."""
import hashlib
import hmac
import json
import os
import sys
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database, checkout_client, counts


@pytest.fixture
def status_checkout(checkout_client, monkeypatch):
    client, provider, path = checkout_client
    # Other legacy modules install a process-wide logger stub at collection.
    # Keep this fixture's failure-path logging independent of collection order.
    monkeypatch.setitem(sys.modules, 'utils.logger', SimpleNamespace(logger=Mock()))
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda: NOW)
    # Token issuance has its own session tests; this fixture owns no Sessions table.
    import api.routers.subscriptions as routes
    monkeypatch.setattr(routes.subscriptionService, '_reissueTokenWithUpdatedClaims', lambda token, *_: token)
    provider.order.payments = lambda order, params: {
        'items': provider.paymentsByOrder.get(order, [])[params['skip']:params['skip'] + params['count']]}
    first = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''},
                        headers={'Idempotency-Key': 'status-original'})
    assert first.status_code == 200
    return client, provider, path, first.json()


def payment(provider, intent, status='authorized', identity='status-payment'):
    item = {'id': identity, 'order_id': intent['orderId'], 'amount': intent['amount'],
            'currency': intent['currency'], 'status': status}
    provider.payments[identity] = item
    provider.paymentsByOrder[intent['orderId']] = [item]
    return item


def verify(client, intent, item):
    signature = hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),
        f"{intent['orderId']}|{item['id']}".encode(), hashlib.sha256).hexdigest()
    return client.post('/verifySubscription', json={'razorpayOrderId': intent['orderId'],
        'razorpayPaymentId': item['id'], 'razorpaySignature': signature})


def status(client, intent=None):
    """The owner's payment status; refreshing it never needs an attempt identity from the browser."""
    return client.get('/payment-status')


def checkout(response):
    assert response.status_code == 200
    return response.json()['checkout']


def event_count(path):
    with sqlTransaction(path) as db:
        return db.execute('SELECT count(*) FROM billing_events').fetchone()[0]


def test_authorized_verification_persists_awaiting_capture(status_checkout, checkout_database):
    client, provider, _, intent = status_checkout
    repository, _ = checkout_database
    result = verify(client, intent, payment(provider, intent))
    assert result.status_code == 200
    assert result.json()['state'] == 'awaiting_capture' and not result.json()['finalized']
    attempt = repository.attemptById(USER, intent['attemptId'])
    assert attempt['payment_status'] == 'authorized'
    observation = repository._json(attempt['metadata_json'])['manualBilling']['providerObservation']
    assert observation['outcome'] == 'authorized' and observation['hasPayments']
    assert not repository.getCoverageSnapshot(USER).accessAllowed


def test_status_refresh_never_creates_invoice_order_or_key(status_checkout, checkout_database):
    client, provider, path, intent = status_checkout
    payment(provider, intent)
    before = counts(path), event_count(path), len(provider.order.createdPayloads)
    response = status(client)
    assert response.json() == {'checkout': {'state': 'confirming_earlier_payment', 'attemptId': intent['attemptId'],
        'amount': intent['amount'], 'currency': intent['currency'], 'selectionLabel': 'Pro monthly with Banking'},
        'refunds': [], 'paymentAttention': None}
    for identity in (intent['invoiceId'], intent['orderId'], 'status-payment'):
        assert identity not in response.text
    assert (counts(path), event_count(path), len(provider.order.createdPayloads)) == before
    assert provider.forbiddenCalls == []
    assert checkout_database[0].attemptById(USER, intent['attemptId'])['payment_status'] == 'authorized'


def test_captured_status_finalizes_original_once(status_checkout, checkout_database):
    client, provider, path, intent = status_checkout
    repository, _ = checkout_database
    payment(provider, intent, 'captured')
    first, replay = checkout(status(client)), checkout(status(client))
    assert first['state'] == replay['state'] == 'completed'
    assert counts(path) == (1, 1) and len(provider.order.createdPayloads) == 1
    assert repository.getCoverageSnapshot(USER).accessAllowed
    with sqlTransaction(path) as db:
        assert db.execute("SELECT count(*) FROM billing_events WHERE event_type='payment.capture'").fetchone()[0] == 1


def test_authorized_observation_cannot_downgrade_capture(status_checkout, checkout_database):
    client, provider, _, intent = status_checkout
    repository, _ = checkout_database
    item = payment(provider, intent, 'captured')
    assert verify(client, intent, item).json()['finalized']
    payment(provider, intent, 'authorized')
    assert checkout(status(client))['state'] == 'completed'
    assert repository.attemptById(USER, intent['attemptId'])['payment_status'] == 'captured'
    assert repository.getCoverageSnapshot(USER).accessAllowed


def test_status_never_reads_or_shows_another_owners_checkout(status_checkout):
    client, provider, path, intent = status_checkout
    with sqlTransaction(path) as db:
        db.execute('UPDATE billing_events SET user_id=? WHERE id=?', ('another-user', intent['attemptId']))
    provider.order.fetch = Mock(side_effect=AssertionError('No provider read for unowned checkout'))
    assert status(client).json() == {'checkout': None, 'refunds': [], 'paymentAttention': None}
    provider.order.fetch.assert_not_called()


def test_unknown_provider_never_reports_completed(status_checkout):
    client, provider, _, intent = status_checkout
    provider.order.fetch = Mock(side_effect=TimeoutError('private provider diagnostic'))
    response = status(client)
    assert checkout(response)['state'] == 'ready'
    assert 'private provider diagnostic' not in response.text


def test_status_without_browser_key(status_checkout):
    client, _, _, intent = status_checkout
    assert checkout(status(client)) == {'state': 'ready', 'attemptId': intent['attemptId'],
        'amount': intent['amount'], 'currency': intent['currency'], 'selectionLabel': 'Pro monthly with Banking'}


def test_existing_exact_key_replay_exposes_recorded_authorization(status_checkout):
    client, provider, _, intent = status_checkout
    assert verify(client, intent, payment(provider, intent)).status_code == 200
    replay = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''},
        headers={'Idempotency-Key': 'status-original'})
    assert replay.status_code == 200 and replay.json()['state'] == 'awaiting_capture'
    assert replay.json()['attemptId'] == intent['attemptId']
    assert len(provider.order.createdPayloads) == 1


def test_expired_unpaid_status_is_ready_for_a_replacing_pay(status_checkout, checkout_database):
    client, _, path, intent = status_checkout
    repository, _ = checkout_database
    attempt = repository.attemptById(USER, intent['attemptId'])
    metadata = repository._json(attempt['metadata_json'])
    metadata['manualBilling']['expiresAt'] = (NOW - timedelta(seconds=1)).isoformat()
    with sqlTransaction(path) as db:
        db.execute('UPDATE billing_events SET metadata_json=? WHERE id=?', (json.dumps(metadata), intent['attemptId']))
    assert checkout(status(client))['state'] == 'ready'


def test_status_unknown_order_ack_does_not_create_replacement(status_checkout):
    client, provider, path, intent = status_checkout
    with sqlTransaction(path) as db:
        db.execute('UPDATE billing_events SET provider_order_id=NULL WHERE id=?', (intent['attemptId'],))
    provider.order.fetch = Mock(side_effect=AssertionError('No Order to read'))
    assert checkout(status(client))['state'] == 'ready'
    assert len(provider.order.createdPayloads) == 1 and counts(path) == (1, 1)


def test_invalid_authorization_evidence_cannot_mutate_attempt(status_checkout, checkout_database):
    client, provider, _, intent = status_checkout
    repository, _ = checkout_database
    item = payment(provider, intent)
    item['amount'] += 1
    response = verify(client, intent, item)
    assert response.status_code == 400
    assert repository.attemptById(USER, intent['attemptId'])['payment_status'] == 'pending_provider_ack'


def test_partial_positive_observation_does_not_overwrite_captured_observation(status_checkout, checkout_database):
    _, _, _, intent = status_checkout
    repository, _ = checkout_database
    captured = CheckoutProviderSnapshot(intent['attemptId'], intent['orderId'], 'captured', NOW,
        True, intent['amount'], intent['currency'], ({'id': 'captured'},))
    repository.recordProviderObservation(intent['attemptId'], captured)
    repository.recordProviderObservation(intent['attemptId'], replace(captured, outcome='authorized',
        observedAt=NOW + timedelta(seconds=1), complete=False, payments=({'id': 'authorized'},)))
    attempt = repository.attemptById(USER, intent['attemptId'])
    assert repository._json(attempt['metadata_json'])['manualBilling']['providerObservation']['outcome'] == 'captured'


@pytest.mark.parametrize('field,value', [('order_id', 'foreign-order'), ('amount', 1), ('currency', 'USD')])
def test_mismatched_provider_payment_never_finalizes(status_checkout, checkout_database, field, value):
    client, provider, path, intent = status_checkout
    item = payment(provider, intent, 'captured')
    item[field] = value
    assert checkout(status(client))['state'] == 'ready'
    assert not checkout_database[0].getCoverageSnapshot(USER).accessAllowed
    assert counts(path) == (1, 1)


def test_incomplete_provider_listing_never_finalizes_partial_capture(status_checkout, checkout_database):
    client, provider, _, intent = status_checkout
    item = payment(provider, intent, 'captured')
    def truncated(params):
        if params['skip']:
            raise TimeoutError('incomplete listing')
        return {'items': [dict(item, id=f'payment-{index}') for index in range(100)]}
    provider.order.payments = lambda order, params: truncated(params)
    assert checkout(status(client))['state'] == 'ready'
    assert not checkout_database[0].getCoverageSnapshot(USER).accessAllowed


def test_status_after_replacement_shows_the_new_checkout(status_checkout):
    client, provider, _, original = status_checkout
    replacement = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''},
        headers={'Idempotency-Key': 'replacement'})
    assert replacement.status_code == 200
    shown = checkout(status(client))
    assert shown['state'] == 'ready' and shown['attemptId'] == replacement.json()['attemptId']
    assert shown['selectionLabel'] == 'Pro monthly with Telecom'
    assert len(provider.order.createdPayloads) == 2


def test_late_capture_on_closed_original_reports_attention(status_checkout, checkout_database):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.billing.paymentStatusPresentation import supportReference
    client, provider, path, original = status_checkout
    repository, _ = checkout_database
    assert client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''},
        headers={'Idempotency-Key': 'replacement'}).status_code == 200
    payment(provider, original, 'captured')
    ManualBillingRecoveryService(repository, provider).recoverAttempt(repository.attemptById(USER, original['attemptId']))
    with sqlTransaction(path) as db:
        captureId = db.execute("SELECT id FROM billing_events WHERE event_type='payment.capture'").fetchone()[0]
    body = status(client).json()
    assert body['checkout']['state'] == 'ready'
    assert body['paymentAttention'] == {'reference': supportReference(captureId)}
    assert not repository.getCoverageSnapshot(USER).accessAllowed


def test_repository_outage_returns_sanitized_unavailable(status_checkout, monkeypatch):
    client, _, _, intent = status_checkout
    monkeypatch.setattr('api.services.billing.paymentStatusPresentation.presentPaymentStatus',
                        Mock(side_effect=RuntimeError('private database DSN')))
    response = status(client)
    assert response.status_code == 503
    assert response.json()['errorCode'] == 'PURCHASE_STATUS_UNAVAILABLE'
    assert 'private database' not in response.text


def test_status_requires_authentication(status_checkout):
    from api.commons import verifyToken
    client, _, _, intent = status_checkout
    client.app.dependency_overrides.pop(verifyToken)
    assert status(client).status_code in (401, 403)


def test_status_response_is_not_cacheable(status_checkout):
    client, _, _, intent = status_checkout
    assert status(client).headers['cache-control'] == 'private, no-store'
