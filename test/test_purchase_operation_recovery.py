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


RETIRED_FLAG = 'SUBSCRIPTION_CANCELLATION_' + 'COMPENSATION_ENABLED'


def test_refund_engine_has_no_runtime_gate():
    module = importlib.import_module('api.services.billing.paymentRefundService')
    assert not hasattr(module, 'compensationReady')
    worker = importlib.import_module('api.services.billing.paymentOperationWorker')
    assert not hasattr(worker, 'HANDLER_VERSION')


def test_submit_route_fails_closed_before_provider_io(checkout_client, monkeypatch):
    client, provider, path = checkout_client
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', raising=False)
    monkeypatch.setenv(RETIRED_FLAG, 'true')
    before = counts(path)
    response = client.post('/purchase-draft/checkout', headers={'Idempotency-Key': 'pay'}, json={
        'draftRevision': 1, 'contact': '', 'pricingContext': 'unused', 'expectedCatalogRevision': 1})
    assert response.status_code == 409 and response.json()['errorCode'] == 'PURCHASE_ACTIONS_UNAVAILABLE'
    assert counts(path) == before and not provider.order.createdPayloads


def test_purchase_actions_follow_only_the_editable_checkout_flag(monkeypatch):
    module = importlib.import_module('api.services.billing.purchaseCheckoutService')
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.delenv(RETIRED_FLAG, raising=False)
    assert module.purchaseActionsReady() is True
    monkeypatch.setenv(RETIRED_FLAG, 'false')
    assert module.purchaseActionsReady() is True
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    monkeypatch.setenv(RETIRED_FLAG, 'true')
    assert module.purchaseActionsReady() is False
