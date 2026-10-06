"""HTTP request identity and the production SQL reservation boundary."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction, seed_payment


@pytest.fixture
def checkout_database(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        for name, kind in {
            'payment_flow': 'TEXT', 'requires_customer_auth': 'BOOLEAN',
            'amount_before_tax': 'INTEGER', 'tax_amount': 'INTEGER',
            'tax_breakdown_json': 'TEXT', 'tax_rule_version': 'TEXT',
            'place_of_supply_snapshot': 'TEXT', 'pricing_version': 'TEXT',
            'pricing_reference_snapshot_json': 'TEXT',
        }.items():
            connection.execute(f'ALTER TABLE "Invoices" ADD COLUMN {name} {kind}')
        from pathlib import Path
        migration = Path('supabase/migrations/20261006131717_enforce_manual_checkout_order_identity.sql').read_text()
        connection.executescript(migration.replace('public.', ''))
    reference = {'amount': 10000, 'currency': 'INR', 'source': 'razorpay_plan_fetch', 'plan_id': 'plan_test'}
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', return_value=reference), \
         patch('api.services.billing.billingEngine._getAnnualBasePrice', return_value=reference):
        yield database


def request(key=None, domains=('banking',), mode='monthly_prepaid', purpose='initial_purchase', **payload):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    return CheckoutRequest(USER, purpose, mode, {'domains': list(domains), **payload}, key)


def counts(path):
    with sqlTransaction(path) as connection:
        return (connection.execute('SELECT count(*) FROM "Invoices"').fetchone()[0],
                connection.execute("SELECT count(*) FROM billing_events WHERE event_category='payment_attempt'").fetchone()[0])


@pytest.fixture
def checkout_client(checkout_database, monkeypatch):
    import os
    from jose import jwt
    from api.commons import verifyToken
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from test.test_manual_auth_coverage import SqlRestClient
    from test.test_customer_free_payments import StrictFakeRazorpayClient
    import api.routers.subscriptions as routes
    repository, path = checkout_database
    service = SubscriptionService.__new__(SubscriptionService)
    service.client = SqlRestClient(path)
    service.VALID_DOMAINS = {'banking', 'manufacturing', 'supplychain', 'telecom'}
    service.razorpayClient = StrictFakeRazorpayClient()
    monkeypatch.setattr(routes, 'subscriptionService', service)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repository)
    token = jwt.encode({'userId': USER, 'email': 'checkout@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    return TestClient(app), service.razorpayClient, path


def test_http_same_key_returns_original_attempt(checkout_client):
    client, provider, path = checkout_client
    payload = {'domains': ['banking'], 'contact': '+919999999999'}
    first = client.post('/createSubscription', json=payload, headers={'Idempotency-Key': 'browser-key'})
    retry = client.post('/createSubscription', json={**payload, 'contact': '+918888888888'}, headers={'Idempotency-Key': 'browser-key'})
    assert first.status_code == retry.status_code == 200
    assert first.json()['attemptId'] == retry.json()['attemptId']
    assert len(provider.order.createdPayloads) == 1
    assert counts(path) == (1, 1)


def test_http_changed_payload_conflicts(checkout_client):
    client, provider, path = checkout_client
    first = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''}, headers={'Idempotency-Key': 'browser-key'})
    changed = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': ''}, headers={'Idempotency-Key': 'browser-key'})
    assert first.status_code == 200 and changed.status_code == 409
    assert len(provider.order.createdPayloads) == 1
    assert counts(path) == (1, 1)


def test_http_headerless_double_click_creates_one_order(checkout_client):
    client, provider, path = checkout_client
    payload = {'domains': ['banking'], 'contact': ''}
    first = client.post('/createSubscription', json=payload)
    second = client.post('/createSubscription', json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()['attemptId'] == second.json()['attemptId']
    assert len(provider.order.createdPayloads) == 1


def test_http_annual_initial_uses_atomic_customer_free_attempt(checkout_client):
    client, provider, path = checkout_client
    response = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '', 'billingMode': 'annual_prepaid'})
    assert response.status_code == 200
    assert response.json()['billingMode'] == 'annual_prepaid'
    assert len(provider.order.createdPayloads) == 1
    assert counts(path) == (1, 1)


def test_expired_key_replays_frozen_intent(checkout_database):
    repository, path = checkout_database
    first = repository.reserveCheckout(request('original'))
    repository.bindProviderOrder(first.attemptId, {'id': 'owned-order'})
    with patch('test.test_manual_billing_runtime.NOW', NOW + timedelta(hours=1)):
        replay = repository.reserveCheckout(request('original'))
    assert replay.attemptId == first.attemptId and replay.expiresAt == first.expiresAt
    assert counts(path) == (1, 1)


def test_monthly_ttl_does_not_change_annual_session_policy(checkout_database, monkeypatch):
    repository, _ = checkout_database
    monkeypatch.setenv('MANUAL_CHECKOUT_TTL_SECONDS', '600')
    annual = repository.reserveCheckout(request('annual', mode='annual_prepaid'))
    assert annual.expiresAt.year == 9999


def test_topup_route_passes_request_key(monkeypatch):
    import api.routers.credits as routes
    from api.commons import verifyToken
    from api.services.credits.topupService import topupService
    observed = []
    monkeypatch.setattr(topupService, 'createTopupOrder', lambda **kwargs: observed.append(kwargs) or {})
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: 'test-token'
    response = TestClient(app).post('/topup/order', json={'packId': 'medium'}, headers={'Idempotency-Key': 'topup-key'})
    assert response.status_code == 200
    assert observed[0]['requestKey'] == 'topup-key'


def test_provider_order_belongs_to_one_attempt_but_can_have_audit_rows(checkout_database):
    repository, path = checkout_database
    repository.finalizeCapturedPayment(seed_payment(checkout_database))
    first = repository.reserveCheckout(request('pack-one', purpose='topup', packId='medium'))
    second = repository.reserveCheckout(request('pack-two', purpose='topup', packId='medium'))
    repository.bindProviderOrder(first.attemptId, {'id': 'same-order'})
    with pytest.raises(Exception, match='UNIQUE'):
        repository.bindProviderOrder(second.attemptId, {'id': 'same-order'})
    with sqlTransaction(path) as connection:
        connection.execute("INSERT INTO billing_events(id,event_category,provider,provider_order_id) VALUES('audit','payment_transaction','razorpay','same-order')")


def test_due_activation_uses_new_experts_for_addition_validation(checkout_database):
    repository, path = checkout_database
    repository.finalizeCapturedPayment(seed_payment(checkout_database))
    end = NOW + timedelta(days=14)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET current_period_start=?,current_period_end=?', ('2026-09-20T12:00:00+00:00', end.isoformat()))
        connection.execute('UPDATE "Invoices" SET period_start=?,period_end=?', ('2026-09-20T12:00:00+00:00', end.isoformat()))
    from dataclasses import replace
    evidence = seed_payment(checkout_database, purpose='renewal', invoice='future', order='future-order', domains=['telecom'])
    repository.finalizeCapturedPayment(replace(evidence, providerPaymentId='future-payment'))
    with patch('test.test_manual_billing_runtime.NOW', end):
        with pytest.raises(ValueError, match='EXPERT_SELECTION_CONFLICT'):
            repository.reserveCheckout(request('addition', ('telecom',), purpose='expert_addition'))


def test_same_key_returns_frozen_original_attempt(checkout_database):
    repository, path = checkout_database
    first = repository.reserveCheckout(request('checkout-one'))
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', side_effect=RuntimeError('provider down')):
        retry = repository.reserveCheckout(request('checkout-one', contact='changed'))
    assert retry == first
    assert counts(path) == (1, 1)


def test_changed_payload_conflicts(checkout_database):
    repository, path = checkout_database
    repository.reserveCheckout(request('checkout-one'))
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        repository.reserveCheckout(request('checkout-one', ('telecom',)))
    assert counts(path) == (1, 1)


def test_no_header_double_click_reuses_live_intent(checkout_database):
    repository, path = checkout_database
    first = repository.reserveCheckout(request())
    assert repository.reserveCheckout(request()).attemptId == first.attemptId
    assert counts(path) == (1, 1)


def test_initial_rejects_due_paid_continuation(checkout_database):
    repository, path = checkout_database
    repository.finalizeCapturedPayment(seed_payment(checkout_database))
    current_end = NOW + timedelta(days=14)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET current_period_start=?,current_period_end=?',
            ('2026-09-20T12:00:00+00:00', current_end.isoformat()))
        connection.execute('UPDATE "Invoices" SET period_start=?,period_end=?',
            ('2026-09-20T12:00:00+00:00', current_end.isoformat()))
    from dataclasses import replace
    renewal = seed_payment(checkout_database, purpose='renewal', invoice='future', order='future-order')
    scheduled = repository.finalizeCapturedPayment(replace(renewal, providerPaymentId='future-payment'))
    assert scheduled.state == 'paid_scheduled'
    with patch('test.test_manual_billing_runtime.NOW', current_end):
        with pytest.raises(ValueError, match='EXISTING_PAID_COVERAGE'):
            repository.reserveCheckout(request('new-lifecycle'))
    assert counts(path) == (2, 2)


def test_invoice_creation_rolls_back_with_attempt(checkout_database):
    repository, path = checkout_database
    with sqlTransaction(path) as connection:
        connection.execute("CREATE TRIGGER fail_attempt BEFORE INSERT ON billing_events BEGIN SELECT RAISE(ABORT,'injected reservation failure'); END")
    with pytest.raises(Exception, match='injected reservation failure'):
        repository.reserveCheckout(request('checkout-one'))
    assert counts(path) == (0, 0)


@pytest.mark.parametrize('route,payload,method', [
    ('createSubscription', {'domains': ['banking'], 'contact': ''}, 'createSubscription'),
    ('addDomains', {'domains': ['telecom']}, 'addDomains'),
    ('createRenewalPaymentSession', {'invoiceId': 'owned'}, 'createRenewalPaymentSession'),
    ('createAnnualRenewalPaymentSession', {'invoiceId': 'owned'}, 'createAnnualRenewalPaymentSession'),
])
def test_http_passes_request_key(route, payload, method, monkeypatch):
    import api.routers.subscriptions as routes
    from api.commons import verifyToken
    observed = []
    monkeypatch.setattr(routes.subscriptionService, method, lambda **kwargs: observed.append(kwargs) or {})
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: 'test-token'
    response = TestClient(app).post('/' + route, json=payload, headers={'Idempotency-Key': 'browser-key'})
    assert response.status_code == 200
    assert observed[0]['requestKey'] == 'browser-key'


@pytest.mark.parametrize('key', ['', 'x' * 129])
def test_http_rejects_malformed_request_key(key, monkeypatch):
    import api.routers.subscriptions as routes
    from api.commons import verifyToken
    monkeypatch.setattr(routes.subscriptionService, 'createSubscription', lambda **kwargs: {})
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: 'test-token'
    response = TestClient(app).post('/createSubscription', json={'domains': ['banking'], 'contact': ''}, headers={'Idempotency-Key': key})
    assert response.status_code == 422
