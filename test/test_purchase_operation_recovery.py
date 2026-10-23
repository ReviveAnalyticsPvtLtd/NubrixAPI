"""Bounded keyset traversal and conservative recovery capabilities."""
from datetime import datetime, timezone
import importlib
import importlib.util

from test.test_manual_billing_runtime import database  # noqa:F401
from test.test_manual_checkout_http import checkout_client, checkout_database, counts  # noqa:F401


def test_worker_scans_past_1000_and_does_not_chase_new_highwater_rows():
    name = 'api.services.billing.paymentOperationWorker'
    assert importlib.util.find_spec(name), 'Durable worker must exist'
    worker = importlib.import_module(name).PaymentOperationWorker
    class Inventory:
        def __init__(self):
            self.ids = list(range(1,1003))
            self.seen = []
        def paymentScanHighWater(self, source):
            return 1001 if source == 'refund' else None
        def paymentScanPage(self, source, highWater, afterId, now, limit=100):
            assert source == 'refund' and limit == 100
            return [{'id':n,'state':'succeeded','kind':source} for n in self.ids
                if (afterId is None or n>afterId) and n<=highWater][:limit]
        def recoverPaymentOperation(self, source, row, provider, now):
            self.seen.append(row['id'])
            return {'resolved':True,'escalated':False}
    repo = Inventory()
    result = worker(repo, object()).execute(datetime(2026,10,21,tzinfo=timezone.utc))
    assert repo.seen == list(range(1,1002))
    assert result == {'checked':1001,'resolved':1001,'noProgress':0,'errors':0,'escalated':0}


def test_readiness_depends_only_on_handler_and_valid_provider_timeout(monkeypatch):
    name = 'api.services.billing.paymentRefundService'
    assert importlib.util.find_spec(name), 'Compensation readiness must be explicit'
    module = importlib.import_module(name)
    monkeypatch.delenv('PURCHASE_PROVIDER_TIMEOUT_SECONDS',raising=False)
    assert module.compensationReady()
    monkeypatch.setenv('PURCHASE_PROVIDER_TIMEOUT_SECONDS','0')
    assert not module.compensationReady()


def test_submit_route_fails_closed_before_provider_io(checkout_client, monkeypatch):
    client, provider, path = checkout_client
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.delenv('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', raising=False)
    before = counts(path)
    response = client.post('/purchase-draft/checkout', headers={'Idempotency-Key': 'pay'}, json={
        'draftRevision': 1, 'contact': '', 'pricingContext': 'unused', 'expectedCatalogRevision': 1})
    assert response.status_code == 409 and response.json()['errorCode'] == 'PURCHASE_ACTIONS_UNAVAILABLE'
    assert counts(path) == before and not provider.order.createdPayloads


def test_purchase_actions_need_both_flags_and_a_valid_provider_timeout(monkeypatch):
    module = importlib.import_module('api.services.billing.purchaseCheckoutService')
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.delenv('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', raising=False)
    assert module.purchaseActionsReady() is False
    monkeypatch.setenv('SUBSCRIPTION_CANCELLATION_COMPENSATION_ENABLED', 'true')
    monkeypatch.setenv('PURCHASE_PROVIDER_TIMEOUT_SECONDS', '0')
    assert module.purchaseActionsReady() is False
