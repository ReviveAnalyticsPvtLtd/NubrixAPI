"""Read-only, authenticated totals before checkout or contact entry."""
from datetime import datetime, timedelta, timezone

import pytest

from test.test_manual_billing_runtime import database, USER
from test.test_manual_checkout_http import checkout_database, checkout_client, counts
from api.services.billing.pricingContext import issuePricingContext


@pytest.mark.parametrize('currency,mode,base', [
    ('INR', 'monthly_prepaid', 200000), ('USD', 'monthly_prepaid', 2000),
    ('INR', 'annual_prepaid', 2000000), ('USD', 'annual_prepaid', 20000),
])
def test_preview_returns_selection_total_without_financial_writes(checkout_client, monkeypatch, currency, mode, base):
    client, provider, path = checkout_client
    monkeypatch.setattr('api.services.billing.pricingService.getPlanPrice',
                        lambda cur, cycle: {'amount': base, 'currency': cur, 'plan_id': 'plan_preview'})
    before = counts(path)
    response = client.post('/pricing-preview', json={
        'domains': ['manufacturing', 'banking'], 'billingMode': mode,
        'pricingContext': issuePricingContext(USER, currency, 'hosting')})
    assert response.status_code == 200
    result = response.json()
    assert result['subject'] == USER
    assert result['domains'] == ['banking', 'manufacturing']
    assert result['billingMode'] == mode
    assert datetime.fromisoformat(result['expiresAt']) > datetime.now(timezone.utc)
    breakdown = result['breakdown']
    assert breakdown['currency'] == currency
    assert breakdown['subtotal'] == base * 2
    assert breakdown['tax'] == round(base * 2 * .18)
    assert breakdown['total'] == breakdown['subtotal'] + breakdown['tax']
    assert sum(row['amount'] for row in breakdown['taxes']) == breakdown['tax']
    assert 'no-store' in response.headers['cache-control']
    assert counts(path) == before
    assert provider.order.createdPayloads == []


@pytest.mark.parametrize('patch', [
    {'domains': []}, {'domains': ['unknown']}, {'domains': ['banking', 'banking']},
    {'billingMode': 'monthly_recurring'}, {'amount': 1}, {'currency': 'USD'},
    {'contact': '+919999999999'}, {'pricingContext': 'invalid'},
])
def test_preview_rejects_invalid_or_client_pricing(checkout_client, patch):
    client, provider, path = checkout_client
    response = client.post('/pricing-preview', json={
        'domains': ['banking'], 'billingMode': 'monthly_prepaid',
        'pricingContext': issuePricingContext(USER, 'INR', 'hosting'), **patch})
    assert response.status_code == 422
    assert counts(path) == (0, 0)
    assert provider.order.createdPayloads == []


@pytest.mark.parametrize('subject,age', [('another-user', 0), (USER, 601)])
def test_preview_rejects_wrong_owner_or_expired_context(checkout_client, subject, age):
    client, provider, path = checkout_client
    response = client.post('/pricing-preview', json={
        'domains': ['banking'], 'billingMode': 'monthly_prepaid',
        'pricingContext': issuePricingContext(subject, 'USD', 'hosting',
            now=datetime.now(timezone.utc) - timedelta(seconds=age))})
    assert response.status_code == 422
    assert counts(path) == (0, 0)
    assert provider.order.createdPayloads == []
