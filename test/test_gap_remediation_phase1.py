"""Regressions for the independent review, using real payment transactions."""
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, request
from test.test_manual_payment_entrypoints import paid_then_request, evidence, RecoverableProvider
from test.test_final_review_regressions import annual_task_database


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
@pytest.mark.parametrize('retry_key', [None, 'retry-new-key', 'next-payment'])
def test_zero_order_creation_failure_can_retry_after_deadline(checkout_database, purpose, mode, retry_key):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    manual, checkout = paid_then_request(checkout_database, mode, purpose)
    provider = manual.razorpayClient
    create = provider.order.create
    provider.order.create = Mock(side_effect=TimeoutError('request never reached provider'))
    with pytest.raises(RuntimeError):
        manual.createCheckout(checkout)
    provider.order.create = create
    later = NOW + timedelta(hours=1)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        intent = manual.createCheckout(replace(checkout, requestKey=retry_key))
    assert intent.razorpayOrderId
    assert provider.order.create is create
    repo, path = checkout_database
    with sqlTransaction(path) as db:
        failed = db.execute("select metadata_json from billing_events where payment_status='failed'").fetchall()
    assert any(json.loads(row[0])['manualBilling']['closedReason'] == 'ORDER_NOT_CREATED' for row in failed)


def test_listing_outage_never_creates_second_order(checkout_database):
    manual, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'initial_purchase')
    provider = manual.razorpayClient
    provider.loseAck = True
    with pytest.raises(RuntimeError):
        manual.createCheckout(checkout)
    provider.order.all = Mock(side_effect=TimeoutError('listing unavailable'))
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        with pytest.raises((RuntimeError, ValueError)):
            manual.createCheckout(replace(checkout, requestKey='another'))
    assert len(provider.order.createdPayloads) == 1


@pytest.mark.parametrize('response', [{}, {'items':None}, {'items':{}}, {'items':[None]}])
def test_malformed_order_listing_never_proves_order_absence(checkout_database, response):
    manual, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'initial_purchase')
    provider = manual.razorpayClient
    provider.order.create = Mock(side_effect=TimeoutError('submission outcome unknown'))
    with pytest.raises(RuntimeError):
        manual.createCheckout(checkout)
    provider.order.create = Mock(side_effect=AssertionError('must not recreate unknown order'))
    provider.order.all = Mock(return_value=response)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
            manual.createCheckout(replace(checkout, requestKey='another'))
    provider.order.create.assert_not_called()


def test_legacy_reconciler_cannot_hide_annual_manual_payment(annual_task_database):
    from nubrix.triggers.tasks.reconciliationTask import ReconciliationTask
    repo, path, client = annual_task_database
    manual, checkout = paid_then_request((repo, path), 'annual_prepaid', 'initial_purchase')
    intent = manual.createCheckout(checkout)
    task = ReconciliationTask.__new__(ReconciliationTask)
    task.client = client
    task.razorpayClient = manual.razorpayClient
    task.razorpayClient.order.fetch = Mock(return_value={'status': 'paid'})
    with sqlTransaction(path) as db:
        db.execute("update billing_events set attempted_at='2020-01-01' where id=?", (intent.attemptId,))
    task._reconcileStaleAttempts()
    assert repo.attemptById(USER, intent.attemptId)['payment_status'] != 'captured'
    assert read_row(path, 'Invoices')['status'] == 'PAYMENT_PENDING'


def test_annual_addition_keeps_renewal_invoice_repriceable(annual_task_database, monkeypatch):
    import api.services.billing.invoiceService as invoices
    repo, path, client = annual_task_database
    manual, checkout = paid_then_request((repo, path), 'annual_prepaid', 'renewal')
    added = manual.createCheckout(request('addition', mode='annual_prepaid', purpose='expert_addition', domains=('telecom',)))
    manual.finalizeCapturedPayment(evidence(added, 'addition-payment'))
    with sqlTransaction(path) as db:
        status = db.execute("select status from \"Invoices\" where id='renewal'").fetchone()[0]
    assert status == 'EXPIRED'
    monkeypatch.setattr(invoices, 'client', client)
    monkeypatch.setattr(invoices, 'getBillingRedisClient', lambda: Mock(set=lambda *a, **kw: True))
    with sqlTransaction(path) as db:
        db.row_factory = __import__('sqlite3').Row
        renewal = dict(db.execute("select * from \"Invoices\" where id='renewal'").fetchone())
    renewal['metadata_json'] = json.loads(renewal['metadata_json'])
    row = invoices.prepareDashboardRenewalInvoice(renewal)
    assert row['status'] == 'PAYMENT_PENDING'
    assert row['metadata_json']['renewalDomains'] == ['banking', 'telecom']
    renewed = manual.createCheckout(request('renew-with-added', mode='annual_prepaid', purpose='renewal', invoiceId='renewal'))
    assert renewed.razorpayOrderId


def test_annual_generator_does_not_reuse_historical_void_invoice(annual_task_database,monkeypatch):
    import api.services.billing.invoiceService as invoices
    repo,path,client=annual_task_database
    manual,_=paid_then_request((repo,path),'annual_prepaid','renewal')
    with sqlTransaction(path) as db: db.execute("update \"Invoices\" set status='VOID' where id='renewal'")
    monkeypatch.setattr(invoices,'client',client)
    monkeypatch.setattr(invoices,'getBillingRedisClient',lambda:Mock(set=lambda *a,**kw:True))
    sub=read_row(path,'subscriptions')
    for key in ('subscribed_experts','pending_removals','billing_state'):sub[key]=json.loads(sub[key])
    created=invoices.createUpcomingRenewalInvoice(sub,{'userId':USER})
    assert created['id']!='renewal' and created['status']=='UPCOMING'


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_new_paid_lifecycle_does_not_inherit_cancellation(checkout_database, mode):
    repo, path = checkout_database
    manual, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'initial_purchase')
    initial = manual.createCheckout(checkout)
    result = manual.finalizeCapturedPayment(evidence(initial))
    repo.setRenewalOptOut(USER, True, 'No longer needed', 'cancel-test')
    later = result.currentPeriod.end + timedelta(days=1)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        purchased = manual.createCheckout(request('new-lifecycle', mode=mode))
        final = manual.finalizeCapturedPayment(replace(evidence(purchased, 'new-payment'), observedAt=later, provenCaptureAt=later))
    assert final.finalized
    sub = read_row(path, 'subscriptions')
    assert not sub['renewal_opt_out']
    assert sub['cancellation_reason'] is None
    assert 'cancellationRequestedAt' not in json.loads(sub['billing_state'])['manualBilling']
    with sqlTransaction(path) as db:
        rows = db.execute("select metadata_json from billing_events where event_type='subscription.lifecycle.started'").fetchall()
    assert any(json.loads(row[0])['previousCancellationReason'] == 'No longer needed' for row in rows)
