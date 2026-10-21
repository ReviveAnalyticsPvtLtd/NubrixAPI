"""Bounded keyset traversal and conservative recovery capabilities."""
from datetime import datetime, timezone
import importlib
import importlib.util


def test_worker_scans_past_1000_and_does_not_chase_new_highwater_rows():
    name = 'api.services.billing.purchaseOperationWorker'
    assert importlib.util.find_spec(name), 'Durable worker must exist'
    worker = importlib.import_module(name).PurchaseOperationWorker
    class Inventory:
        def __init__(self):
            self.ids = list(range(1,1003))
            self.seen = []
        def purchaseScanHighWater(self, source):
            return 1001 if source == 'refund' else None
        def purchaseScanPage(self, source, highWater, afterId, now, limit=100):
            assert source == 'refund' and limit == 100
            return [{'id':n,'state':'succeeded','kind':source} for n in self.ids
                if (afterId is None or n>afterId) and n<=highWater][:limit]
        def recoverPurchaseItem(self, source, row, provider, now):
            self.seen.append(row['id'])
            return {'resolved':True,'escalated':False}
    repo = Inventory()
    result = worker(repo, object()).execute(datetime(2026,10,21,tzinfo=timezone.utc))
    assert repo.seen == list(range(1,1002))
    assert result == {'checked':1001,'resolved':1001,'noProgress':0,'errors':0,'escalated':0}


def test_readiness_depends_only_on_handler_and_valid_provider_timeout(monkeypatch):
    name = 'api.services.billing.purchaseCompensationService'
    assert importlib.util.find_spec(name), 'Compensation readiness must be explicit'
    module = importlib.import_module(name)
    monkeypatch.delenv('PURCHASE_PROVIDER_TIMEOUT_SECONDS',raising=False)
    assert module.compensationReady()
    monkeypatch.setenv('PURCHASE_PROVIDER_TIMEOUT_SECONDS','0')
    assert not module.compensationReady()
