from types import SimpleNamespace

import pytest

from api.services.subscriptions.subscriptionWorkerService import SubscriptionWorkerService
from test.subscriptionModelFixtures import NOW


@pytest.mark.parametrize('cap', [100, 1000])
@pytest.mark.parametrize('interval', ['month', 'year'])
def test_workers_reach_paid_owners_beyond_the_data_api_limit(cap, interval):
    rows = [dict(id=f'{i:05d}', user_id=f'owner-{i:05d}', erasure_pending=False) for i in range(1502)]
    class Query:
        def __init__(self):
            self.after, self.maximum = None, 1000
        def select(self, _fields): return self
        def eq(self, field, value):
            assert field == 'is_canonical' and value is True
            return self
        def order(self, field):
            assert field == 'id'
            return self
        def gt(self, field, value):
            assert field == 'id'
            self.after = value
            return self
        def limit(self, maximum):
            self.maximum = maximum
            return self
        def execute(self):
            selected = [row for row in rows if self.after is None or row['id'] > self.after]
            return SimpleNamespace(data=selected[:min(cap, self.maximum)])
    class Client:
        def table(self, name):
            assert name == 'subscriptions'
            return Query()
    materialized = []
    class Snapshots:
        def get(self, owner, now):
            paid = owner == rows[-1]['user_id']
            return SimpleNamespace(subscriptionId=owner.removeprefix('owner-'),
                billingInterval=interval if paid else None, accessSource='paid' if paid else 'none',
                accessAllowed=paid, status='active' if paid else 'none')
    repository = SimpleNamespace(activateDueCoverage=lambda owner, now: materialized.append(owner))
    result = SubscriptionWorkerService(Client(), repository, Snapshots()).execute(interval=interval, now=NOW)
    assert result['scanned'] == 1502 and result['errors'] == 0
    assert result['skipped'] == 1501 and result['materialized'] == 1
    assert materialized == ['owner-01501']


def test_annual_reminder_retries_when_repricing_waits_for_an_authorized_payment():
    from datetime import timedelta
    row = dict(id='00001', user_id='owner-00001', erasure_pending=False)
    class Client:
        def table(self, name):
            return self
        def select(self, _fields): return self
        def eq(self, *_): return self
        def order(self, _field): return self
        def limit(self, _maximum): return self
        def gt(self, *_): return self
        def execute(self):
            rows, self.rows = getattr(self, 'rows', [row]), []
            return SimpleNamespace(data=rows)
    owned = SimpleNamespace(subscriptionId='00001', billingInterval='year', accessSource='paid', accessAllowed=True,
        status='active', renewalOptOut=False, nextPeriod=None, purchaseBlockReason=None, renewalAvailable=True,
        currentPeriod=SimpleNamespace(end=NOW + timedelta(days=5)), lifecycleId='life')
    def deferred(invoiceId):
        raise RuntimeError('RENEWAL_REPRICING_DEFERRED')
    enqueued = []
    repository = SimpleNamespace(activateDueCoverage=lambda owner, now: None,
        getRenewalContext=lambda owner, now: {}, createAnnualRenewalInvoice=lambda owner, sid: {'id': 'invoice'},
        prepareAnnualRenewalInvoice=deferred, enqueueSubscriptionNotification=lambda *args: enqueued.append(args))
    result = SubscriptionWorkerService(Client(), repository, SimpleNamespace(get=lambda owner, now: owned)).execute(
        reminders=True, now=NOW)
    assert result['errors'] == 0 and result['skipped'] == 1 and result['prepared'] == 0 and not enqueued
