"""New purchase actions stay disabled until compensation is actually installed."""
import importlib
import importlib.util

import pytest

from test.test_manual_billing_runtime import database  # noqa:F401
from test.test_manual_checkout_http import checkout_client, checkout_database, counts  # noqa:F401


def test_cancellation_and_submit_routes_fail_closed_before_provider_io(checkout_client, monkeypatch):
    client, provider, path = checkout_client
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.setenv('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', 'true')
    before = counts(path)
    response = client.post('/purchase-checkouts/7b2944da-d7de-49c4-af9c-b1a2832e5332/cancel',
        headers={'Idempotency-Key': 'cancel'}, json={'expectedOperationRevision': 0})
    assert response.status_code == 409 and response.json()['errorCode'] == 'PURCHASE_ACTIONS_UNAVAILABLE'
    response = client.post('/purchase-draft/checkout', headers={'Idempotency-Key': 'pay'}, json={
        'draftRevision': 1, 'contact': '', 'pricingContext': 'unused', 'expectedCatalogRevision': 1})
    assert response.status_code == 409 and response.json()['errorCode'] == 'PURCHASE_ACTIONS_UNAVAILABLE'
    assert counts(path) == before and not provider.order.createdPayloads


def test_missing_compensation_handler_cannot_be_enabled_by_flags_alone(monkeypatch):
    name = 'api.services.billing.purchaseCheckoutService'
    assert importlib.util.find_spec(name), 'Guarded purchase service must exist'
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.setenv('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', 'true')
    module = importlib.import_module(name)
    assert module.purchaseActionsReady() is False
