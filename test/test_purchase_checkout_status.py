"""Owned status refresh observes original money without opening another checkout."""
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


def status(client, intent):
    return client.post('/purchase-checkouts/' + intent['attemptId'] + '/status', json={})


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
    response = status(client, intent)
    assert response.status_code == 200
    body = response.json()
    assert body['schemaVersion'] == 1 and body['userId'] == USER
    assert body['invoiceId'] == intent['invoiceId'] and body['attemptId'] == intent['attemptId']
    assert body['state'] == 'confirming_earlier_payment' and body['providerState'] == 'authorized'
    assert body['capabilities'] == {'mayEditDraft': False, 'maySubmitLatestDraft': False}
    assert not {'operationRevision', 'cancellationAcceptedAt'} & body.keys()
    assert not {'razorpayKey', 'razorpaySignature', 'requestKey', 'orderId'} & body.keys()
    assert (counts(path), event_count(path), len(provider.order.createdPayloads)) == before
    assert provider.forbiddenCalls == []
    assert checkout_database[0].attemptById(USER, intent['attemptId'])['payment_status'] == 'authorized'


def test_captured_status_finalizes_original_once(status_checkout, checkout_database):
    client, provider, path, intent = status_checkout
    repository, _ = checkout_database
    payment(provider, intent, 'captured')
    first, replay = status(client, intent), status(client, intent)
    assert first.status_code == replay.status_code == 200
    assert first.json()['state'] == replay.json()['state'] == 'completed'
    assert first.json()['providerState'] == 'captured'
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
    response = status(client, intent)
    assert response.status_code == 200 and response.json()['state'] == 'completed'
    assert repository.attemptById(USER, intent['attemptId'])['payment_status'] == 'captured'
    assert repository.getCoverageSnapshot(USER).accessAllowed


@pytest.mark.parametrize('foreign', [False, True])
def test_status_owner_mismatch_is_not_found(status_checkout, foreign):
    client, provider, path, intent = status_checkout
    if foreign:
        with sqlTransaction(path) as db:
            db.execute('UPDATE billing_events SET user_id=? WHERE id=?', ('another-user', intent['attemptId']))
    else:
        intent = {**intent, 'attemptId': str(uuid.uuid4())}
    provider.order.fetch = Mock(side_effect=AssertionError('No provider read for unowned checkout'))
    response = status(client, intent)
    assert response.status_code == 404
    assert response.json()['errorCode'] == 'PURCHASE_CHECKOUT_NOT_FOUND'
    provider.order.fetch.assert_not_called()


def test_unknown_provider_never_returns_payable(status_checkout):
    client, provider, _, intent = status_checkout
    provider.order.fetch = Mock(side_effect=TimeoutError('private provider diagnostic'))
    response = status(client, intent)
    assert response.status_code == 200
    assert response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'PROVIDER_STATUS_UNKNOWN'
    assert response.json()['providerState'] == 'unknown'
    assert 'private provider diagnostic' not in response.text


def test_status_without_browser_key(status_checkout):
    client, _, _, intent = status_checkout
    response = status(client, intent)
    assert response.status_code == 200
    assert response.json()['state'] == 'ready' and response.json()['pendingReason'] is None


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
    response = status(client, intent)
    assert response.status_code == 200
    assert response.json()['state'] == 'ready'


def test_status_unknown_order_ack_does_not_create_replacement(status_checkout):
    client, provider, path, intent = status_checkout
    with sqlTransaction(path) as db:
        db.execute('UPDATE billing_events SET provider_order_id=NULL WHERE id=?', (intent['attemptId'],))
    response = status(client, intent)
    assert response.status_code == 200 and response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'PROVIDER_STATUS_UNKNOWN'
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
    response = status(client, intent)
    assert response.status_code == 200 and response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'PROVIDER_STATUS_UNKNOWN'
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
    response = status(client, intent)
    assert response.status_code == 200 and response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'PROVIDER_STATUS_UNKNOWN'
    assert not checkout_database[0].getCoverageSnapshot(USER).accessAllowed


def test_status_of_replaced_original_is_closed(status_checkout):
    client, provider, _, original = status_checkout
    replacement = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''},
        headers={'Idempotency-Key': 'replacement'})
    assert replacement.status_code == 200
    response = status(client, original)
    assert response.status_code == 200 and response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'ORIGINAL_CHECKOUT_CLOSED'
    assert len(provider.order.createdPayloads) == 2


def test_late_capture_on_closed_original_reports_attention(status_checkout, checkout_database):
    client, provider, _, original = status_checkout
    assert client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''},
        headers={'Idempotency-Key': 'replacement'}).status_code == 200
    payment(provider, original, 'captured')
    response = status(client, original)
    assert response.status_code == 200 and response.json()['state'] == 'ready'
    assert response.json()['pendingReason'] == 'PAYMENT_REQUIRES_RECONCILIATION'
    assert not checkout_database[0].getCoverageSnapshot(USER).accessAllowed


def test_repository_outage_returns_sanitized_unavailable(status_checkout, checkout_database, monkeypatch):
    client, _, _, intent = status_checkout
    monkeypatch.setattr(checkout_database[0], 'attemptById', Mock(side_effect=RuntimeError('private database DSN')))
    response = status(client, intent)
    assert response.status_code == 503
    assert response.json()['errorCode'] == 'PURCHASE_STATUS_UNAVAILABLE'
    assert 'private database' not in response.text


def test_status_requires_authentication_even_for_valid_attempt(status_checkout):
    from api.commons import verifyToken
    client, _, _, intent = status_checkout
    client.app.dependency_overrides.pop(verifyToken)
    assert status(client, intent).status_code in (401, 403)


def test_status_response_is_not_cacheable(status_checkout):
    client, _, _, intent = status_checkout
    assert status(client, intent).headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('restriction', ['erasure', 'banned'])
def test_status_never_offers_pay_for_restricted_owner(status_checkout, restriction):
    client, _, path, intent = status_checkout
    with sqlTransaction(path) as db:
        if restriction == 'erasure':
            db.execute('UPDATE subscriptions SET erasure_pending=1 WHERE user_id=?', (USER,))
        else:
            db.execute('UPDATE "Users" SET "isBanned"=1 WHERE "userId"=?', (USER,))
    response = status(client, intent)
    assert response.status_code == 200
    assert response.json()['capabilities'] == {'mayEditDraft': False, 'maySubmitLatestDraft': False}
