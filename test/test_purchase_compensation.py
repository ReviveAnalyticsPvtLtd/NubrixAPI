"""Real SQL compensation with a synthetic payment/refund boundary."""
from dataclasses import replace
import importlib
import importlib.util
import os
from uuid import uuid4

import pytest
from psycopg2.extras import Json

from test.purchaseProviderFixtures import SyntheticPurchaseProvider
from test.test_purchase_checkout_postgres import checkoutDb, drafts, controlledPurchase, authorization
from test.test_subscription_erasure_postgres import erasureDb
from test.subscriptionRuntimeFixtures import postgres, runtimeDb, query

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Disposable PostgreSQL opt-in; skip is UNVERIFIED')


class SyntheticRefundProvider(SyntheticPurchaseProvider):
    def __init__(self):
        super().__init__()
        self.refunds, self.refundCalls = {}, []
        self.refundFault, self.refundStatus = None, 'pending'
        self.onRefund = None
        self.payment.refund = self.refund
        self.payment.fetch_multiple_refund = self.refundList

    def refundList(self, paymentId, params, **options):
        items = [dict(r) for r in self.refunds.values() if r['payment_id'] == paymentId]
        return {'items': items[params['skip']:params['skip']+params['count']]}

    def externalRefund(self, paymentId, amount, status='processed', notes=None):
        key = 'rfnd-synthetic-' + str(uuid4())
        self.refunds[key] = {'id': key, 'payment_id': paymentId, 'currency': self.payments[paymentId]['currency'],
            'amount': amount, 'status': status, 'notes': notes or {}}
        self.payments[paymentId]['amount_refunded'] = sum(r['amount'] for r in self.refunds.values()
            if r['payment_id'] == paymentId and r['status'] == 'processed')
        return dict(self.refunds[key])

    def refund(self, paymentId, data, **kwargs):
        self.refundCalls.append((paymentId, dict(data), kwargs))
        if self.onRefund:
            self.onRefund()
        if self.refundFault == 'before':
            raise TimeoutError('synthetic unknown submission')
        result = self.externalRefund(paymentId, data['amount'], self.refundStatus, data['notes'])
        if self.refundFault == 'after':
            raise TimeoutError('synthetic response lost')
        return result


def closedCapture(drafts, monkeypatch, **selection):
    repo, original, intent, cancel = controlledPurchase(drafts, monkeypatch, **selection)
    provider = SyntheticRefundProvider()
    provider.orders = original.orders
    cancel.provider = provider
    cancel.cancel(intent.userId, intent.attemptId, 1, 'cancel-for-correction')
    evidence = replace(authorization(repo, provider, intent), financialStatus='captured')
    provider.payments[evidence.providerPaymentId]['status'] = 'captured'
    provider.payments[evidence.providerPaymentId]['amount_refunded'] = 0
    result = repo.finalizeCapturedPayment(evidence)
    assert result.state == 'requires_reconciliation'
    name = 'api.services.billing.purchaseCompensationService'
    assert importlib.util.find_spec(name), 'Dedicated full compensation handler is required'
    service = importlib.import_module(name).PurchaseCompensationService(repo, provider)
    return repo, provider, intent, evidence, service


def test_duplicate_late_capture_reserves_one_full_compensation(drafts, monkeypatch):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    first = service.reserveForClosedCapture(evidence)
    second = service.reserveForClosedCapture(replace(evidence, provenCaptureAt=evidence.observedAt,
        timingVerified=True))
    assert str(first['id']) == str(second['id'])
    assert first['kind'] == 'refund' and first['reason'] == 'paid_after_close'
    assert first['dedupe_key'] == 'payment:refund:' + evidence.providerPaymentId
    assert first['amount'] == intent.amount and first['currency'] == 'INR'
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed
    result = service.execute(str(first['id']))
    assert result['state'] == 'submitted'
    service.execute(str(first['id']))
    assert len(provider.refundCalls) == 1
    assert provider.refundCalls[0][1]['amount'] == intent.amount
    assert provider.refundCalls[0][1]['notes']['purchaseOperationId'] == str(first['id'])
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


@pytest.mark.parametrize('external,status,expected,calls', [
    ('none','processed','succeeded',1), ('none','pending','submitted',1),
    ('full','processed','succeeded',0), ('full','pending','submitted',0),
    ('partial','processed','succeeded',1), ('partial','pending','submitted',0)])
def test_complete_refund_history_prevents_duplicate_customer_returns(drafts, monkeypatch, external, status, expected, calls):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    if external != 'none':
        provider.externalRefund(evidence.providerPaymentId, intent.amount if external == 'full' else 100, status)
    provider.refundStatus = status
    op = service.reserveForClosedCapture(evidence)
    result = service.execute(str(op['id']))
    assert result['state'] == expected
    assert len(provider.refundCalls) == calls
    if calls:
        assert provider.refundCalls[0][1]['amount'] == intent.amount-(100 if external == 'partial' else 0)
    if expected == 'succeeded':
        frozen = repo._json(repo.attemptById(intent.userId, intent.attemptId)['metadata_json'])['manualBilling']
        assert frozen['closureReconciledAt']
        assert repo.readEditor(intent.userId).checkout.refundState == 'completed'
        assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


@pytest.mark.parametrize('fault', ['before','after'])
def test_refund_unknown_send_is_lookup_only_even_when_gates_disable(drafts, monkeypatch, fault):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    op = service.reserveForClosedCapture(evidence)
    provider.refundFault = fault
    assert service.execute(str(op['id']))['state'] == 'outcome_unknown'
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: False)
    result = service.execute(str(op['id']))
    assert result['state'] == ('submitted' if fault == 'after' else 'outcome_unknown')
    assert len(provider.refundCalls) == 1
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_inconsistent_refund_history_holds_liability_and_escalates_once(drafts, monkeypatch):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    op = service.reserveForClosedCapture(evidence)
    provider.payments[evidence.providerPaymentId]['amount_refunded'] = 100
    service.execute(str(op['id']))
    service.execute(str(op['id']))
    assert not provider.refundCalls
    assert repo.operationById(str(op['id']))['state'] == 'needs_attention'
    events = query(drafts[3], "SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='purchase.operation_escalated'", (intent.userId,))
    assert len(events) == 1 and events[0]['metadata_json']['caseReference'] == 'purchase:'+str(op['id'])


@pytest.mark.parametrize('mutation', ['owner','order','amount','purpose','no_cancellation'])
def test_unrelated_or_invalid_capture_has_no_compensation_authority(drafts, monkeypatch, mutation):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    if mutation == 'no_cancellation':
        otherRepo, otherProvider, otherIntent, _ = controlledPurchaseForOtherOwner(drafts, monkeypatch)
        otherEvidence = replace(authorization(otherRepo, otherProvider, otherIntent), financialStatus='captured')
        otherRepo.finalizeCapturedPayment(otherEvidence)
        result = service.reserveForClosedCapture(otherEvidence)
        assert result is None
    else:
        values = {'owner': {'userId': 'foreign'}, 'order': {'providerOrderId': 'foreign'},
            'amount': {'amount': evidence.amount+1}, 'purpose': {'purpose':'renewal'}}[mutation]
        with pytest.raises(ValueError):
            service.reserveForClosedCapture(replace(evidence, **values))
    assert not provider.refundCalls


def controlledPurchaseForOtherOwner(drafts, monkeypatch):
    repo, billing, _, url = drafts
    user = 'other-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    billing.ensureCanonicalSubscription(user)
    return controlledPurchase((repo,billing,user,url), monkeypatch)


def test_verified_late_capture_transport_reserves_and_worker_completes_after_tabs_close(drafts,monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.purchaseOperationWorker import PurchaseOperationWorker
    repo,provider,intent,evidence,_=closedCapture(drafts,monkeypatch)
    provider.refundStatus='processed'
    ManualPaymentService.forProduction(provider,repo).finalizeCapturedPayment(evidence)
    ops=query(drafts[3],"SELECT id FROM payment_operations WHERE attempt_id=%s AND kind='refund'",(intent.attemptId,))
    assert len(ops)==1
    assert not provider.refundCalls
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady',lambda:False)
    PurchaseOperationWorker(repo,provider).execute()
    assert repo.operationById(str(ops[0]['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==1


def test_cancellation_of_authorized_purchase_is_accepted_immediately_and_never_captures(drafts,monkeypatch):
    from api.services.billing.purchaseOperationWorker import PurchaseOperationWorker
    repo,provider,intent,cancel=controlledPurchase(drafts,monkeypatch)
    evidence=authorization(repo,provider,intent)
    cancel.cancel(intent.userId,intent.attemptId,1,'authorized-cancel')
    provider.payments[evidence.providerPaymentId]['status']='failed'
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady',lambda:False)
    PurchaseOperationWorker(repo,provider).execute()
    status=repo.readEditor(intent.userId).checkout
    assert status.cancellationAcceptedAt and status.state=='cancelled'
    frozen=repo._json(repo.purchaseAttempt(intent.attemptId)['metadata_json'])['manualBilling']
    assert frozen['closureReconciledAt']
    assert not provider.captureCalls


def test_late_capture_after_erasure_uses_retained_cancellation_without_recreating_account(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    repo,oldProvider,intent,cancel=controlledPurchase(drafts,monkeypatch)
    provider=SyntheticRefundProvider()
    provider.orders=oldProvider.orders
    cancel.provider=provider
    cancel.cancel(intent.userId,intent.attemptId,1,'before-erasure')
    _,request=runErasure(drafts[3],intent.userId)
    payment=provider.authorize(intent)
    provider.payments[payment['id']].update(status='captured',amount_refunded=0)
    evidence=VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,intent.userId,intent.razorpayOrderId,
        payment['id'],'initial_purchase',intent.currency,'captured','server_observation',intent.amount,_now(),None,None,False)
    repo.finalizeCapturedPayment(evidence)
    from api.services.billing.purchaseCompensationService import PurchaseCompensationService
    service=PurchaseCompensationService(repo,provider)
    op=service.reserveForClosedCapture(evidence)
    assert op['user_id'] is None and str(op['erasure_request_id'])==request
    assert op['kind']=='refund' and op['reason']=='paid_after_close' and op['dedupe_key']=='payment:refund:'+payment['id']
    provider.refundStatus='processed'
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert query(drafts[3],'SELECT * FROM "Users" WHERE "userId"=%s',(intent.userId,))==[]
    assert len(provider.refundCalls)==1
    refund=next(iter(provider.refunds.values()))
    payload={'payload':{'refund':{'entity':refund}}}
    linked=query(drafts[3],'SELECT private.erasure_request_for_webhook(NULL,%s) AS request',(Json(payload),),one=True)
    assert str(linked['request'])==request


def test_obligation_report_exposes_operation_and_one_actionable_case(drafts,monkeypatch):
    from api.services.billing.manualObligationReport import listObligations
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    repo.escalateOperationOnce(str(op['id']),'PURCHASE_RESOLUTION_OVERDUE')
    report=listObligations(repo,limit=500)
    assert report['available']
    items=[r for r in report['items'] if r['id']==str(op['id'])]
    assert len(items)==1 and items[0]['category']=='purchase_refund'
    assert items[0]['caseReference']=='purchase:'+str(op['id'])


def test_worker_backoff_and_escalation_never_release_liability(drafts,monkeypatch):
    from datetime import timedelta
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    when=op['created_at']+timedelta(seconds=1801)
    assert repo.schedulePurchaseRetry(str(op['id']),when)
    updated=repo.operationById(str(op['id']))
    assert updated['retry_count']==1 and updated['next_retry_at']==when+timedelta(seconds=30)
    assert not repo.schedulePurchaseRetry(str(op['id']),when)
    updated=repo.operationById(str(op['id']))
    assert updated['retry_count']==2 and updated['next_retry_at']==when+timedelta(seconds=60)
    assert updated['state']=='queued'
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_worker_heartbeat_required_before_new_purchase_actions(drafts,monkeypatch):
    repo,provider,intent,cancel=controlledPurchase(drafts,monkeypatch)
    query(drafts[3],"UPDATE private.payment_recovery_runtime SET worker_seen_at=clock_timestamp()-interval '10 minutes'")
    with pytest.raises(ValueError,match='PURCHASE_ACTIONS_UNAVAILABLE'):
        cancel.cancel(intent.userId,intent.attemptId,1,'stale-worker')
    assert not repo.readEditor(intent.userId).capabilities.mayRequestCancellation
    repo.recordPurchaseWorkerHeartbeat()
    assert repo.readEditor(intent.userId).capabilities.mayRequestCancellation


def test_fully_refunded_payment_entity_resolves_cancellation_without_new_charge(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    provider.externalRefund(evidence.providerPaymentId,intent.amount,'processed')
    provider.payments[evidence.providerPaymentId]['status']='refunded'
    assert service.execute(str(op['id']))['state']=='succeeded'
    frozen=repo._json(repo.purchaseAttempt(intent.attemptId)['metadata_json'])['manualBilling']
    assert frozen.get('closureReconciledAt')
    assert not provider.refundCalls


def test_concurrent_refund_executors_send_once_without_holding_owner_lock(drafts,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    entered,release=Event(),Event()
    def delayed():
        entered.set()
        assert release.wait(5)
    provider.onRefund=delayed
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(service.execute,str(op['id']))
        assert entered.wait(5)
        try:
            assert pool.submit(repo.readEditor,intent.userId).result(timeout=5).draft.revision==1
            second=pool.submit(service.execute,str(op['id'])).result(timeout=5)
            assert second['state'] in ('submission_started','outcome_unknown','submitted')
        finally:
            release.set()
        assert first.result(timeout=5)['state']=='submitted'
    assert len(provider.refundCalls)==1


def test_sql_keyset_scan_pages_1001_rows_with_fixed_highwater(drafts,monkeypatch):
    from uuid import UUID
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        SELECT lpad(to_hex(n),32,'0')::uuid,%s,%s,%s,'pay-page-'||n,'refund','paid_after_close',%s,%s,'payment:refund:pay-page-'||n
        FROM generate_series(1,1001) n''',(intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    from api.services.billing.manualBillingRepository import _now
    cutoff=_now()
    high=repo.purchaseScanHighWater('refund')
    later=str(UUID(int=UUID(str(high)).int+1))
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,'pay-after-highwater','refund','paid_after_close',%s,%s,'payment:refund:pay-after-highwater')''',
        (later,intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    after=None
    seen=[]
    while True:
        page=repo.purchaseScanPage('refund',high,after,cutoff,limit=100)
        if not page:
            break
        seen.extend(str(r['id']) for r in page if r['user_id']==intent.userId)
        after=page[-1]['id']
    assert len(seen)==1001 and later not in seen
    # These inventory-only identities have no synthetic provider payments.
    # Do not make later functional sweeps contact 1,002 deliberate phantoms.
    query(drafts[3],"UPDATE payment_operations SET state='cancelled' WHERE user_id=%s AND kind='refund' AND state='queued'",(intent.userId,))


def test_worker_scan_excludes_new_rows_with_uuid_below_highwater(drafts,monkeypatch):
    from api.services.billing.manualBillingRepository import _now
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    cutoff=_now()
    high=repo.purchaseScanHighWater('refund')
    newId='00000000-0000-0000-0000-000000002001'
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,'pay-after-start','refund','paid_after_close',%s,%s,'payment:refund:pay-after-start')''',
        (newId,intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    page=repo.purchaseScanPage('refund',high,None,cutoff,limit=100)
    assert newId not in {str(r['id']) for r in page}


@pytest.mark.parametrize('breakage',['trigger','rls','public_write'])
def test_broken_storage_protection_disables_purchase_actions(drafts,monkeypatch,breakage):
    repo,provider,intent,cancel=controlledPurchase(drafts,monkeypatch)
    statements={
        'trigger':('ALTER TABLE payment_operations DISABLE TRIGGER payment_storage_guard',
                   'ALTER TABLE payment_operations ENABLE TRIGGER payment_storage_guard'),
        'rls':('ALTER TABLE payment_operations DISABLE ROW LEVEL SECURITY',
               'ALTER TABLE payment_operations ENABLE ROW LEVEL SECURITY'),
        'public_write':('GRANT INSERT ON payment_operations TO anon',
                        'REVOKE INSERT ON payment_operations FROM anon')}
    change,restore=statements[breakage]
    try:
        query(drafts[3],change)
        assert not repo.readEditor(intent.userId).capabilities.mayRequestCancellation
    finally:
        query(drafts[3],restore)


def test_completed_correction_is_no_longer_reported_as_a_financial_obligation(drafts,monkeypatch):
    from api.services.billing.manualObligationReport import listObligations
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    provider.refundStatus='processed'
    op=service.reserveForClosedCapture(evidence)
    assert service.execute(str(op['id']))['state']=='succeeded'
    cursor=None
    unresolved=[]
    while True:
        report=listObligations(repo,limit=500,cursor=cursor)
        assert report['available']
        unresolved.extend(row for row in report['items'] if row['userId']==intent.userId)
        cursor=report['nextCursor']
        if not cursor:
            break
    assert unresolved==[]


def test_failed_provider_refund_stays_owed_and_creates_one_case(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    provider.refundStatus='failed'
    service.execute(str(op['id']))
    service.recover(str(op['id']))
    row=repo.operationById(str(op['id']))
    assert row['state']=='needs_attention' and row['escalation_reference']=='purchase:'+str(op['id'])
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft
    assert len(provider.refundCalls)==1


def test_worker_retries_only_a_verified_terminal_failed_refund_with_new_submission_identity(drafts,monkeypatch):
    from datetime import timedelta
    from api.services.billing.purchaseOperationWorker import PurchaseOperationWorker
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    provider.refundStatus='failed'
    service.execute(str(op['id']))
    worker=PurchaseOperationWorker(repo,provider)
    worker.execute(op['created_at']+timedelta(seconds=31))
    assert repo.operationById(str(op['id']))['state']=='queued'
    provider.refundStatus='processed'
    worker.execute(op['created_at']+timedelta(seconds=92))
    assert repo.operationById(str(op['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==2
    assert provider.refundCalls[0][2]['headers']!=provider.refundCalls[1][2]['headers']
    assert all(call[1]['amount']==intent.amount for call in provider.refundCalls)


def test_ready_editor_never_reenables_an_expired_original_payment(drafts,monkeypatch):
    from datetime import timedelta
    from api.services.billing.purchaseCheckoutStatusService import PurchaseCheckoutStatusService
    repo,provider,intent,_=controlledPurchase(drafts,monkeypatch)
    later=intent.expiresAt+timedelta(seconds=1)
    for module in ('manualBillingRepository','purchaseCheckoutRepository','purchaseOperationRepository'):
        monkeypatch.setattr('api.services.billing.'+module+'._now',lambda:later)
    status=PurchaseCheckoutStatusService(repo,provider).refresh(intent.userId,intent.attemptId)
    assert not status.capabilities.mayResumeOriginal
    assert not repo.readEditor(intent.userId).capabilities.mayResumeOriginal


def test_installed_sdk_receives_timeout_and_cannot_repost_unknown_refund(drafts,monkeypatch):
    import json
    import razorpay
    from types import SimpleNamespace
    from api.services.billing.purchaseCompensationService import PurchaseCompensationService
    repo,provider,intent,evidence,_=closedCapture(drafts,monkeypatch)
    requests=[]
    class Session:
        def get(self,url,**options):
            requests.append(options)
            if url.endswith('/refunds'):
                payload=provider.refundList(evidence.providerPaymentId,options['params'])
            elif '/payments/' in url:
                payload=provider.payment.fetch(evidence.providerPaymentId)
            elif url.endswith('/payments'):
                payload=provider.orderPayments(intent.razorpayOrderId,options['params'])
            else:
                payload=provider.orders[intent.razorpayOrderId]
            return SimpleNamespace(status_code=200,json=lambda:payload)
        def post(self,url,**options):
            requests.append(options)
            assert url.endswith('/refund')
            provider.refund(evidence.providerPaymentId,json.loads(options['data']),headers=options['headers'])
            raise TimeoutError('synthetic reply lost at SDK HTTP boundary')
    sdk=razorpay.Client(session=Session(),auth=('synthetic','synthetic'))
    service=PurchaseCompensationService(repo,sdk)
    op=service.reserveForClosedCapture(evidence)
    assert service.execute(str(op['id']))['state']=='outcome_unknown'
    assert service.execute(str(op['id']))['state']=='submitted'
    assert len(provider.refundCalls)==1
    assert requests and all(0<r.get('timeout',0)<=30 for r in requests)


def test_provider_internal_retries_cannot_bypass_durable_refund_submission(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    provider.retry_enabled=True
    service.execute(str(op['id']))
    assert not provider.refundCalls
    assert repo.operationById(str(op['id']))['state'] in ('queued','needs_attention')


@pytest.mark.parametrize('phase',['dual','v2'])
@pytest.mark.parametrize('currency',['INR','USD'])
@pytest.mark.parametrize('interval',['month','year'])
def test_full_original_currency_correction_in_each_supported_phase_and_interval(drafts,monkeypatch,phase,currency,interval):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE',phase)
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch,currency=currency,interval=interval)
    provider.refundStatus='processed'
    op=service.reserveForClosedCapture(evidence)
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert op['currency']==currency and provider.refundCalls[0][1]['amount']==intent.amount
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed


def test_corrected_provider_configuration_can_resume_a_never_submitted_correction(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    provider.retry_enabled=True
    service.execute(str(op['id']))
    assert not provider.refundCalls
    provider.retry_enabled=False
    provider.refundStatus='processed'
    service.execute(str(op['id']))
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==1


def test_external_pending_refund_must_finish_failing_before_remaining_correction_can_submit(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    pending=provider.externalRefund(evidence.providerPaymentId,100,'pending')
    op=service.reserveForClosedCapture(evidence)
    assert service.execute(str(op['id']))['state']=='submitted'
    assert not provider.refundCalls
    provider.refunds[pending['id']]['status']='failed'
    provider.refundStatus='processed'
    service.execute(str(op['id']))
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==1 and provider.refundCalls[0][1]['amount']==intent.amount


def test_pending_return_in_payment_aggregate_never_means_refund_completed(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    provider.externalRefund(evidence.providerPaymentId,intent.amount,'pending')
    provider.payments[evidence.providerPaymentId]['amount_refunded']=intent.amount
    op=service.reserveForClosedCapture(evidence)
    assert service.execute(str(op['id']))['state']=='submitted'
    assert not provider.refundCalls
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_provider_capture_timestamp_before_confirmed_cancellation_cannot_break_the_promise(drafts,monkeypatch):
    from datetime import timedelta
    from api.services.billing.purchaseCompensationService import PurchaseCompensationService
    repo,original,intent,cancel=controlledPurchase(drafts,monkeypatch)
    provider=SyntheticRefundProvider()
    provider.orders=original.orders
    cancel.provider=provider
    authorizationEvidence=authorization(repo,provider,intent)
    cancel.cancel(intent.userId,intent.attemptId,1,'cancel-authorized')
    evidence=replace(authorizationEvidence,financialStatus='captured',timingVerified=True,
        provenCaptureAt=authorizationEvidence.observedAt-timedelta(seconds=1))
    provider.payments[evidence.providerPaymentId].update(status='captured',amount_refunded=0)
    assert repo.finalizeCapturedPayment(evidence).state=='requires_reconciliation'
    service=PurchaseCompensationService(repo,provider)
    op=service.reserveForClosedCapture(evidence)
    provider.refundStatus='processed'
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed


def test_concurrent_retry_scheduling_uses_the_locked_retry_count(drafts,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Barrier
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    original=repo.operationById
    rendezvous=Barrier(2)
    def simultaneousRead(key):
        row=original(key)
        rendezvous.wait(timeout=5)
        return row
    monkeypatch.setattr(repo,'operationById',simultaneousRead)
    now=op['created_at']+timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(repo.schedulePurchaseRetry,str(op['id']),now) for _ in range(2)]
        assert all(not f.result(timeout=10) for f in futures)
    row=original(str(op['id']))
    assert row['retry_count']==2
    assert row['next_retry_at']==now+timedelta(seconds=60)


def test_replacement_late_capture_after_erasure_preserves_slot_ownership(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.purchaseCompensationService import PurchaseCompensationService
    repo,original,first,cancel=controlledPurchase(drafts,monkeypatch)
    provider=SyntheticRefundProvider()
    provider.orders=original.orders
    cancel.provider=provider
    cancel.cancel(first.userId,first.attemptId,1,'first-cancel')
    repo.saveDraft(first.userId,1,['banking'],'month')
    pricing={'userId':first.userId,'currency':first.currency,'catalogRevision':1,
        'domains':['banking'],'billingMode':'monthly_prepaid'}
    second=repo.reserveDraftCheckout(first.userId,2,pricing,'replacement-before-erasure')
    second=ManualPaymentService.forProduction(provider,repo).openReservedCheckout(second)
    cancel.cancel(first.userId,second.attemptId,1,'replacement-cancel')
    runErasure(drafts[3],first.userId)
    payment=provider.authorize(second)
    provider.payments[payment['id']].update(status='captured',amount_refunded=0)
    evidence=VerifiedPaymentEvidence(second.attemptId,second.invoiceId,first.userId,second.razorpayOrderId,
        payment['id'],'initial_purchase',second.currency,'captured','server_observation',second.amount,_now(),None,None,False)
    repo.finalizeCapturedPayment(evidence)
    service=PurchaseCompensationService(repo,provider)
    op=service.reserveForClosedCapture(evidence)
    provider.refundStatus='processed'
    failures=[]
    claim=repo.beginCompensationSubmission
    def tracedClaim(*args):
        try:
            return claim(*args)
        except Exception as exc:
            failures.append(type(exc).__name__+':'+str(exc))
            raise
    monkeypatch.setattr(repo,'beginCompensationSubmission',tracedClaim)
    result=service.execute(str(op['id']))
    assert result['state']=='succeeded', failures
    assert not repo.purchaseControl(second.attemptId)['user_id']
    slots={str(repo.purchaseControl(a)['slot_id']) for a in (first.attemptId,second.attemptId)}
    assert len(slots)==1
    assert query(drafts[3],'SELECT user_id FROM payment_slots WHERE slot_id=%s',(slots.pop(),),one=True)['user_id'] is None
    assert len(provider.refundCalls)==1


@pytest.mark.parametrize('actual,expected',[('pending','submitted'),('processed','succeeded'),('failed','needs_attention')])
def test_refund_webhook_uses_provider_truth_for_machine_correction_without_resubmission(drafts,monkeypatch,actual,expected):
    from api.services.webhookService import WebhookService
    from api.services.subscriptions.subscriptionService import subscriptionService
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=service.reserveForClosedCapture(evidence)
    service.execute(str(op['id']))
    refund=next(iter(provider.refunds.values()))
    refund['status']=actual
    provider.payments[evidence.providerPaymentId]['amount_refunded']=intent.amount if actual=='processed' else 0
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository',lambda:repo)
    monkeypatch.setattr(subscriptionService,'razorpayClient',provider)
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady',lambda:False)
    emitted=[]
    webhook=WebhookService.__new__(WebhookService)
    # Isolate only the legacy external audit sink; operation persistence is real SQL.
    webhook._auditLog=lambda *args,**kwargs:emitted.append(kwargs)
    webhook._handleRefundProcessed({'payload':{'refund':{'entity':{**refund,'status':'processed'}}}})
    row=repo.operationById(str(op['id']))
    assert row['state']==expected
    if actual!='processed':
        assert not any(r.get('status')=='processed' for r in emitted)
    assert len(provider.refundCalls)==1
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed
