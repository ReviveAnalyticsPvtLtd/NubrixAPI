"""Durable USD checkouts retain currency through provider orders and capture."""
from dataclasses import replace

import pytest

from test.test_manual_billing_runtime import database, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, checkout_client, request, counts
from test.test_manual_payment_entrypoints import evidence
from test.test_customer_free_payments import StrictFakeRazorpayClient
from api.services.billing.manualPaymentService import ManualPaymentService


@pytest.mark.parametrize("mode", ["monthly_prepaid", "annual_prepaid"])
def test_usd_purchase_persists_currency(checkout_database, monkeypatch, mode):
    repository, path = checkout_database
    provider = StrictFakeRazorpayClient()
    service = ManualPaymentService.forProduction(provider, repository)
    for helper in ("_getMonthlyBasePrice", "_getAnnualBasePrice"):
        monkeypatch.setattr("api.services.billing.billingEngine." + helper,
                            lambda currency="INR": {"amount": 1739, "currency": currency, "plan_id": "unit_plan"})
    checkout = replace(request("usd-initial", mode=mode), currency="USD")
    intent = service.createCheckout(checkout)
    assert (intent.currency, provider.order.createdPayloads[-1]["currency"]) == ("USD", "USD")
    assert service.finalizeCapturedPayment(evidence(intent)).finalized
    assert read_row(path, "subscriptions")["default_currency"] == "USD"
    assert service.createCheckout(checkout).attemptId == intent.attemptId


def test_currency_change_cannot_adopt_same_key(checkout_database, monkeypatch):
    repository, _ = checkout_database
    service = ManualPaymentService.forProduction(StrictFakeRazorpayClient(), repository)
    monkeypatch.setattr("api.services.billing.billingEngine._getMonthlyBasePrice",
                        lambda currency="INR": {"amount": 1739, "currency": currency, "plan_id": "unit_plan"})
    checkout = replace(request("same-key"), currency="USD")
    service.createCheckout(checkout)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        service.createCheckout(replace(checkout, currency="INR"))


def test_unsigned_new_purchase_has_no_financial_side_effect(checkout_client):
    client, provider, path = checkout_client
    response = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '', 'pricingContext': None})
    assert response.status_code == 409
    body = response.json()
    assert (body.get('detail') or body)['errorCode'] == 'PRICING_CONTEXT_REQUIRED'
    assert counts(path) == (0, 0)
    assert provider.order.createdPayloads == []


def test_context_from_another_account_cannot_create_order(checkout_client):
    from api.services.billing.pricingContext import issuePricingContext
    client, provider, path = checkout_client
    response = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '',
        'pricingContext': issuePricingContext('someone-else', 'USD', 'hosting')})
    assert response.status_code == 409
    assert counts(path) == (0, 0)
    assert provider.order.createdPayloads == []


def test_expired_context_can_resume_only_owned_frozen_selection(checkout_client):
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_manual_billing_runtime import USER, NOW
    client, provider, path = checkout_client
    first = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''},
                        headers={'Idempotency-Key': 'existing'}).json()
    expired = issuePricingContext(USER, 'INR', 'hosting', now=NOW)
    resumed = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '', 'pricingContext': expired},
                          headers={'Idempotency-Key': 'existing'})
    assert resumed.status_code == 200
    assert resumed.json()['orderId'] == first['orderId']
    assert counts(path) == (1, 1)
    changed = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': '', 'pricingContext': expired},
                          headers={'Idempotency-Key': 'existing'})
    assert changed.status_code == 409
    assert counts(path) == (1, 1)


@pytest.mark.parametrize('context', ['expired', 'missing'])
@pytest.mark.parametrize('closure', ['before_lookup', 'during_lookup'])
def test_uncreated_order_replacement_requires_fresh_pricing(checkout_client, checkout_database, monkeypatch, context, closure):
    from datetime import timedelta
    from unittest.mock import patch
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_manual_billing_runtime import USER, NOW
    client, provider, path = checkout_client
    repository, _ = checkout_database
    payload = {'domains': ['banking'], 'contact': ''}
    headers = {'Idempotency-Key': 'uncreated-order'}
    with patch.object(provider.order, 'create', side_effect=TimeoutError('not submitted')):
        assert client.post('/createSubscription', json=payload, headers=headers).status_code == 503
    with sqlTransaction(path) as db:
        attemptId = db.execute("SELECT id FROM billing_events WHERE event_category='payment_attempt'").fetchone()[0]

    def close():
        with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(minutes=33)):
            assert repository.closeUncreatedOrder(attemptId)

    if closure == 'before_lookup':
        close()
    else:
        original = repository.initialReplayCurrency
        def lookup(*args):
            currency = original(*args)
            close()
            return currency
        monkeypatch.setattr(repository, 'initialReplayCurrency', lookup)
    stale = None if context == 'missing' else issuePricingContext(USER, 'INR', 'hosting', now=NOW)
    response = client.post('/createSubscription', json={**payload, 'pricingContext': stale}, headers=headers)
    assert response.status_code == 409
    assert counts(path) == (1, 1)
    assert provider.order.createdPayloads == []
    fresh = client.post('/createSubscription', json=payload, headers=headers)
    assert fresh.status_code == 200
    assert counts(path) == (2, 2)
    assert len(provider.order.createdPayloads) == 1


def test_fresh_travel_context_resumes_original_owned_currency(checkout_client):
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_manual_billing_runtime import USER
    client, provider, path = checkout_client
    first = client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''},
                        headers={'Idempotency-Key': 'travel'}).json()
    resumed = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '',
        'pricingContext': issuePricingContext(USER, 'USD', 'hosting', catalogRevision=1)}, headers={'Idempotency-Key': 'travel'})
    assert resumed.status_code == 200
    assert (resumed.json()['orderId'], resumed.json()['currency']) == (first['orderId'], 'INR')
    assert counts(path) == (1, 1)
    assert len(provider.order.createdPayloads) == 1


def test_foreign_currency_new_key_cannot_supersede_live_initial_order(checkout_client, monkeypatch):
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_manual_billing_runtime import USER
    client, provider, path = checkout_client
    monkeypatch.setattr('api.services.billing.billingEngine._getMonthlyBasePrice',
                        lambda currency='INR': {'amount': 1739, 'currency': currency, 'plan_id': 'unit_plan'})
    client.post('/createSubscription', json={'domains': ['banking'], 'contact': ''},
                headers={'Idempotency-Key': 'india'})
    changed = client.post('/createSubscription', json={'domains': ['banking'], 'contact': '',
        'pricingContext': issuePricingContext(USER, 'USD', 'hosting', catalogRevision=1)}, headers={'Idempotency-Key': 'travel-new'})
    assert changed.status_code == 409
    body = changed.json()
    assert (body.get('detail') or body)['errorCode'] == 'LIVE_INITIAL_CHECKOUT_CONFLICT'
    assert counts(path) == (1, 1)
    assert len(provider.order.createdPayloads) == 1


@pytest.mark.parametrize('context', ['fresh', 'expired', 'missing'])
def test_replay_lookup_database_outage_is_retryable(checkout_client, checkout_database, monkeypatch, context):
    import psycopg2
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_manual_billing_runtime import USER, NOW
    client, provider, path = checkout_client
    repository, _ = checkout_database
    def unavailable(*args, **kwargs):
        raise psycopg2.OperationalError('local test database unavailable')
    monkeypatch.setattr(repository, 'initialReplayCurrency', unavailable)
    payload = {'domains': ['banking'], 'contact': ''}
    if context != 'fresh':
        payload['pricingContext'] = None if context == 'missing' else issuePricingContext(USER, 'INR', 'hosting', now=NOW)
    response = client.post('/createSubscription', json=payload, headers={'Idempotency-Key': 'existing'})
    assert response.status_code == 503
    assert counts(path) == (0, 0)
    assert provider.order.createdPayloads == []
