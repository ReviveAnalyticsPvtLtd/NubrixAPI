"""Real SQL full refund of a captured payment that was never granted, for every purpose, with a synthetic
payment/refund boundary.

Finalization reserves the refund (spec 4.4/4.5); these cases drive its execution, history checks, retries,
escalation and the two customer emails. There is no runtime gate: the refund worker always runs once deployed.
"""
from dataclasses import replace
from datetime import datetime, timezone
import importlib
import importlib.util
import math
import os
from uuid import uuid4

import pytest
from psycopg2.extras import Json

from test.purchaseProviderFixtures import SyntheticPurchaseProvider
from test.test_purchase_checkout_postgres import (checkoutDb, drafts, controlledPurchase, authorization, supersede,
    recordUngrantedCapture)
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


def startedAfter(moment):
    """A provider payment start (whole seconds) at or after ``moment``."""
    return math.ceil(moment.timestamp())


def closedCapture(drafts, monkeypatch, **selection):
    """A payment started on a checkout that Pay had already replaced: finalization reserves its full refund."""
    repo, original, intent, _ = controlledPurchase(drafts, monkeypatch, **selection)
    provider = SyntheticRefundProvider()
    provider.orders = original.orders
    supersede(repo, provider, intent)  # Pay with a changed selection replaced the empty Order.
    closedAt = query(drafts[3], 'SELECT closed_at FROM payment_slot_controls WHERE attempt_id=%s',
                     (intent.attemptId,), one=True)['closed_at']
    started = startedAfter(closedAt)
    evidence = replace(authorization(repo, provider, intent), financialStatus='captured',
                       paymentCreatedAt=datetime.fromtimestamp(started, timezone.utc))
    provider.payments[evidence.providerPaymentId].update(status='captured', amount_refunded=0, created_at=started)
    result = repo.finalizeCapturedPayment(evidence)
    assert result.state == 'refund_pending' and not result.finalized
    name = 'api.services.billing.paymentRefundService'
    assert importlib.util.find_spec(name), 'Dedicated full refund handler is required'
    service = importlib.import_module(name).PaymentRefundService(repo, provider)
    return repo, provider, intent, evidence, service


def reserved(url, evidence):
    """The one refund operation finalization reserved for the payment."""
    return reservedFor(url, evidence.providerPaymentId)


def reservedFor(url, paymentId):
    rows = query(url, "SELECT * FROM payment_operations WHERE provider_payment_id=%s AND kind='refund'", (paymentId,))
    assert len(rows) == 1
    return dict(rows[0])


def refundsVia(provider):
    """The same synthetic payments and Orders, able to list and send refunds."""
    refunds = SyntheticRefundProvider()
    refunds.orders, refunds.payments = provider.orders, provider.payments
    return refunds


def duplicateCapture(drafts, monkeypatch, purpose, currency='INR', interval='month'):
    """The first payment on ``purpose``'s Order is granted; a second payment on that same Order is a duplicate."""
    from test.test_payment_replacement_postgres import monthlyRenewalInvoice, annualRenewalInvoice, purposePay
    from test.test_capture_finalization_postgres import providerPayment, finalize
    repo, original, initial, _ = controlledPurchase(drafts, monkeypatch, ('banking',), interval, currency)
    provider = refundsVia(original)
    mode = 'monthly_prepaid' if interval == 'month' else 'annual_prepaid'
    intent = initial
    if purpose != 'initial_purchase':
        assert finalize(repo, initial, providerPayment(provider, initial, 'captured')).finalized
        payload = {'renewal': lambda: {'invoiceId': str((monthlyRenewalInvoice if interval == 'month'
                                                         else annualRenewalInvoice)(repo, initial.userId)['id'])},
                   'expert_addition': lambda: {'domains': ['telecom']},
                   'topup': lambda: {'packId': 'medium'}}[purpose]()
        intent = purposePay(repo, provider, initial.userId, purpose, mode, payload)
        assert intent.purpose == purpose and intent.currency == currency
    assert finalize(repo, intent, providerPayment(provider, intent, 'captured')).finalized
    second = providerPayment(provider, intent, 'captured')
    second['amount_refunded'] = provider.payments[second['id']]['amount_refunded'] = 0  # as the provider reports it
    assert finalize(repo, intent, second).state == 'refund_pending'
    return repo, provider, intent, second['id']


def refundIntents(url, operationId):
    """Committed customer email intents of one refund operation, by milestone."""
    rows = query(url, """SELECT idempotency_key,user_id,metadata_json FROM billing_events
        WHERE event_type='email.billing_intent.committed' AND idempotency_key LIKE %s ORDER BY idempotency_key""",
        ('notification:payment_refund:' + str(operationId) + ':%',))
    return {row['idempotency_key'].rsplit(':', 1)[1]: row for row in rows}


@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
@pytest.mark.parametrize('interval', ['month', 'year'])
@pytest.mark.parametrize('currency', ['INR', 'USD'])
def test_refund_matrix(drafts, monkeypatch, currency, interval, purpose):
    from api.services.billing.paymentRefundService import PaymentRefundService
    url = drafts[3]
    repo, provider, intent, paymentId = duplicateCapture(drafts, monkeypatch, purpose, currency, interval)
    op = reservedFor(url, paymentId)
    invoice = query(url, 'SELECT amount_before_tax,tax_amount,total_amount,currency FROM "Invoices" WHERE id=%s',
                    (intent.invoiceId,), one=True)
    # The full captured amount, tax included, in the payment's own currency.
    assert (op['kind'], op['reason'], op['currency'], invoice['currency']) == ('refund', 'duplicate', currency, currency)
    assert op['amount'] == intent.amount == invoice['total_amount'] == invoice['amount_before_tax'] + invoice['tax_amount']
    provider.refundStatus = 'processed'
    service = PaymentRefundService(repo, provider)
    assert service.execute(str(op['id']))['state'] == 'succeeded'
    service.execute(str(op['id']))
    service.recover(str(op['id']))
    assert [(call[0], call[1]['amount']) for call in provider.refundCalls] == [(paymentId, intent.amount)]
    assert [r['provider_payment_id'] for r in query(url, 'SELECT provider_payment_id FROM payment_operations WHERE user_id=%s',
                                                     (intent.userId,))] == [paymentId]
    assert set(refundIntents(url, op['id'])) == {'started', 'completed'}


def test_late_capture_reserves_one_full_refund(drafts, monkeypatch):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    first = reserved(drafts[3], evidence)
    replayed = repo.finalizeCapturedPayment(replace(evidence, provenCaptureAt=evidence.observedAt, timingVerified=True))
    assert replayed.state == 'refund_pending'
    second = reserved(drafts[3], evidence)
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
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


@pytest.mark.parametrize('external,status,expected,calls', [
    ('none','processed','succeeded',1), ('none','pending','submitted',1),
    ('full','processed','succeeded',0), ('full','pending','submitted',0),
    ('partial','processed','succeeded',1), ('partial','pending','submitted',0)])
def test_complete_refund_history_prevents_duplicate_customer_returns(drafts, monkeypatch, external, status, expected, calls):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    if external != 'none':
        provider.externalRefund(evidence.providerPaymentId, intent.amount if external == 'full' else 100, status)
    provider.refundStatus = status
    op = reserved(drafts[3], evidence)
    result = service.execute(str(op['id']))
    assert result['state'] == expected
    assert len(provider.refundCalls) == calls
    if calls:
        assert provider.refundCalls[0][1]['amount'] == intent.amount-(100 if external == 'partial' else 0)
    if expected == 'succeeded':
        frozen = repo._json(repo.attemptById(intent.userId, intent.attemptId)['metadata_json'])['manualBilling']
        # A superseded Order stays in the recovery scan until its capture window passes.
        assert frozen['closedReason'] == 'SUPERSEDED' and not frozen.get('closureReconciledAt')
        assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


@pytest.mark.parametrize('fault', ['before','after'])
def test_unknown_send_lookup_only(drafts, monkeypatch, fault):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    op = reserved(drafts[3], evidence)
    provider.refundFault = fault
    assert service.execute(str(op['id']))['state'] == 'outcome_unknown'
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: False)
    result = service.execute(str(op['id']))
    assert result['state'] == ('submitted' if fault == 'after' else 'outcome_unknown')
    assert len(provider.refundCalls) == 1
    assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_inconsistent_refund_history_holds_liability_and_escalates_once(drafts, monkeypatch):
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    op = reserved(drafts[3], evidence)
    provider.payments[evidence.providerPaymentId]['amount_refunded'] = 100
    service.execute(str(op['id']))
    service.execute(str(op['id']))
    assert not provider.refundCalls
    assert repo.operationById(str(op['id']))['state'] == 'needs_attention'
    events = query(drafts[3], "SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='payment.refund_escalated'", (intent.userId,))
    assert len(events) == 1 and events[0]['metadata_json']['caseReference'] == 'refund:'+str(op['id'])


@pytest.mark.parametrize('mutation', ['granted', 'other_attempt', 'amount', 'currency', 'reason', 'owner', 'order'])
def test_unrelated_or_invalid_capture_has_no_refund_authority(drafts, monkeypatch, mutation):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from api.services.billing.paymentOperationRepository import reserveRefund
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    url = drafts[3]
    def reserve(**changes):
        values = dict(attemptId=intent.attemptId, paymentId=evidence.providerPaymentId, amount=intent.amount,
                      currency=intent.currency, reason='paid_after_close') | changes
        with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            return reserveRefund(cursor, **values)
    assert reserve() == str(reserved(url, evidence)['id'])  # idempotent on the provider payment
    if mutation == 'granted':
        otherRepo, otherProvider, otherIntent, _ = controlledPurchaseForOtherOwner(drafts, monkeypatch)
        payment = otherProvider.authorize(otherIntent)
        otherEvidence = replace(authorization(otherRepo, otherProvider, otherIntent), financialStatus='captured',
            providerPaymentId=payment['id'], paymentCreatedAt=datetime.fromtimestamp(payment['created_at'], timezone.utc))
        assert otherRepo.finalizeCapturedPayment(otherEvidence).finalized
        with pytest.raises(ValueError, match='PAYMENT_REFUND_CAPTURE_NOT_VERIFIED'):
            reserve(attemptId=otherIntent.attemptId, paymentId=payment['id'], amount=otherIntent.amount)
        assert query(url, 'SELECT * FROM payment_operations WHERE provider_payment_id=%s', (payment['id'],)) == []
    elif mutation == 'other_attempt':
        live = repo.readEditor(intent.userId).checkout.attemptId
        amount = query(url, 'SELECT amount FROM billing_events WHERE id=%s', (live,), one=True)['amount']
        with pytest.raises(ValueError, match='PAYMENT_REFUND_CAPTURE_NOT_VERIFIED'):
            reserve(attemptId=live, amount=amount)  # the payment was recorded on another Order
    elif mutation in ('amount', 'currency', 'reason'):
        changes = {'amount': {'amount': intent.amount + 1}, 'currency': {'currency': 'USD'},
                   'reason': {'reason': 'goodwill'}}[mutation]
        with pytest.raises(ValueError):
            reserve(**changes)
    else:
        values = {'owner': {'userId': 'foreign'}, 'order': {'providerOrderId': 'foreign'}}[mutation]
        with pytest.raises(ValueError):
            repo.finalizeCapturedPayment(replace(evidence, **values))
    assert len(query(url, 'SELECT id FROM payment_operations WHERE attempt_id=%s', (intent.attemptId,))) == 1
    assert not provider.refundCalls


def controlledPurchaseForOtherOwner(drafts, monkeypatch):
    repo, billing, _, url = drafts
    user = 'other-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    billing.ensureCanonicalSubscription(user)
    return controlledPurchase((repo,billing,user,url), monkeypatch)


def test_verified_late_capture_transport_reserves_and_worker_completes_after_tabs_close(drafts,monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    repo,provider,intent,evidence,_=closedCapture(drafts,monkeypatch)
    provider.refundStatus='processed'
    ManualPaymentService.forProduction(provider,repo).finalizeCapturedPayment(evidence)
    ops=query(drafts[3],"SELECT id FROM payment_operations WHERE attempt_id=%s AND kind='refund'",(intent.attemptId,))
    assert len(ops)==1
    assert not provider.refundCalls
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady',lambda:False)
    PaymentOperationWorker(repo,provider).execute()
    assert repo.operationById(str(ops[0]['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==1


def test_erased_owner_refund_uses_retained_authority(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    repo,oldProvider,intent,_=controlledPurchase(drafts,monkeypatch)
    provider=SyntheticRefundProvider()
    provider.orders=oldProvider.orders
    supersede(repo,provider,intent,key='before-erasure')
    _,request=runErasure(drafts[3],intent.userId)
    payment=provider.authorize(intent)
    provider.payments[payment['id']].update(status='captured',amount_refunded=0)
    evidence=VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,intent.userId,intent.razorpayOrderId,
        payment['id'],'initial_purchase',intent.currency,'captured','server_observation',intent.amount,_now(),None,None,False,
        paymentCreatedAt=datetime.fromtimestamp(payment['created_at'],timezone.utc))
    assert repo.finalizeCapturedPayment(evidence).state=='refund_pending'
    assert repo.finalizeCapturedPayment(evidence).state=='refund_pending'  # replay
    from api.services.billing.paymentRefundService import PaymentRefundService
    service=PaymentRefundService(repo,provider)
    op=reserved(drafts[3],evidence)
    assert op['user_id'] is None and str(op['erasure_request_id'])==request
    # The account is closed: its money is returned whatever the attempt's state (spec 4.4 row 5).
    assert op['kind']=='refund' and op['reason']=='account_closed' and op['dedupe_key']=='payment:refund:'+payment['id']
    provider.refundStatus='processed'
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert query(drafts[3],'SELECT * FROM "Users" WHERE "userId"=%s',(intent.userId,))==[]
    assert len(provider.refundCalls)==1
    refund=next(iter(provider.refunds.values()))
    payload={'payload':{'refund':{'entity':refund}}}
    linked=query(drafts[3],'SELECT private.erasure_request_for_webhook(NULL,%s) AS request',(Json(payload),),one=True)
    assert str(linked['request'])==request
    assert refundIntents(drafts[3],op['id'])=={}  # an erased account is never emailed


def test_obligation_report_exposes_operation_and_one_actionable_case(drafts,monkeypatch):
    from api.services.billing.manualObligationReport import listObligations
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    repo.escalateOperationOnce(str(op['id']),'PAYMENT_REFUND_OVERDUE')
    report=listObligations(repo,limit=500)
    assert report['available']
    items=[r for r in report['items'] if r['id']==str(op['id'])]
    assert len(items)==1 and items[0]['category']=='payment_refund'
    assert items[0]['caseReference']=='refund:'+str(op['id'])


def test_backoff_and_single_escalation(drafts,monkeypatch):
    from datetime import timedelta
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    key=str(op['id'])
    early=op['created_at']+timedelta(seconds=1799)
    assert not repo.scheduleRefundRetry(key,early)  # unresolved for under 30 minutes: no case yet
    assert repo.operationById(key)['next_retry_at']==early+timedelta(seconds=30)
    when=op['created_at']+timedelta(seconds=1801)
    escalated=[]
    for retry in range(1,10):
        escalated.append(repo.scheduleRefundRetry(key,when))
        updated=repo.operationById(key)
        assert updated['retry_count']==retry+1
        assert updated['next_retry_at']==when+timedelta(seconds=min(30*2**min(retry,7),3600))
    assert escalated==[True]+[False]*8
    events=query(drafts[3],"SELECT metadata_json FROM billing_events WHERE event_type='payment.refund_escalated' AND user_id=%s",
                 (intent.userId,))
    assert [e['metadata_json']['reasonCode'] for e in events]==['PAYMENT_REFUND_OVERDUE']
    updated=repo.operationById(key)
    assert updated['state']=='queued' and updated['escalation_reference']=='refund:'+key
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_fully_refunded_payment_entity_settles_refund_without_new_charge(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    provider.externalRefund(evidence.providerPaymentId,intent.amount,'processed')
    provider.payments[evidence.providerPaymentId]['status']='refunded'
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert not provider.refundCalls


def test_concurrent_refund_executors_send_once_without_holding_owner_lock(drafts,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    entered,release=Event(),Event()
    def delayed():
        entered.set()
        assert release.wait(5)
    provider.onRefund=delayed
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(service.execute,str(op['id']))
        assert entered.wait(5)
        try:
            assert pool.submit(repo.readEditor,intent.userId).result(timeout=5).draft.revision==2
            second=pool.submit(service.execute,str(op['id'])).result(timeout=5)
            assert second['state'] in ('submission_started','outcome_unknown','submitted')
        finally:
            release.set()
        assert first.result(timeout=5)['state']=='submitted'
    assert len(provider.refundCalls)==1


def test_keyset_scan_past_1000_rows(drafts,monkeypatch):
    from uuid import UUID
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    # Inventory-only phantoms: each still needs its ungranted recorded capture (storage guard).
    query(drafts[3],'''INSERT INTO billing_events(id,user_id,invoice_id,event_category,event_type,event_status,
        provider_order_id,provider_payment_id,amount,currency,idempotency_key)
        SELECT gen_random_uuid(),%s,%s,'reconciliation','payment.capture','REFUND_RESERVED',%s,'pay-page-'||n,%s,%s,
        'capture:pay-page-'||n FROM generate_series(1,1001) n''',
        (intent.userId,intent.invoiceId,intent.razorpayOrderId,intent.amount,intent.currency))
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        SELECT lpad(to_hex(n),32,'0')::uuid,%s,%s,%s,'pay-page-'||n,'refund','paid_after_close',%s,%s,'payment:refund:pay-page-'||n
        FROM generate_series(1,1001) n''',(intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    from api.services.billing.manualBillingRepository import _now
    cutoff=_now()
    high=repo.paymentScanHighWater('refund')
    later=str(UUID(int=UUID(str(high)).int+1))
    recordUngrantedCapture(drafts[3],intent,'pay-after-highwater')
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,'pay-after-highwater','refund','paid_after_close',%s,%s,'payment:refund:pay-after-highwater')''',
        (later,intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    after=None
    seen=[]
    while True:
        page=repo.paymentScanPage('refund',high,after,cutoff,limit=100)
        if not page:
            break
        seen.extend(str(r['id']) for r in page if r['user_id']==intent.userId and r['provider_payment_id'].startswith('pay-page-'))
        after=page[-1]['id']
    assert len(seen)==1001 and later not in seen
    # These inventory-only identities have no synthetic provider payments.
    # Do not make later functional sweeps contact 1,002 deliberate phantoms.
    query(drafts[3],"UPDATE payment_operations SET state='cancelled' WHERE user_id=%s AND kind='refund' AND state='queued'",(intent.userId,))


def test_worker_scan_excludes_new_rows_with_uuid_below_highwater(drafts,monkeypatch):
    from api.services.billing.manualBillingRepository import _now
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    cutoff=_now()
    high=repo.paymentScanHighWater('refund')
    newId='00000000-0000-0000-0000-000000002001'
    recordUngrantedCapture(drafts[3],intent,'pay-after-start')
    query(drafts[3],'''INSERT INTO payment_operations
        (id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,'pay-after-start','refund','paid_after_close',%s,%s,'payment:refund:pay-after-start')''',
        (newId,intent.userId,intent.attemptId,intent.invoiceId,intent.amount,intent.currency))
    page=repo.paymentScanPage('refund',high,None,cutoff,limit=100)
    assert newId not in {str(r['id']) for r in page}


@pytest.mark.parametrize('breakage',['trigger','rls','public_write'])
def test_broken_storage_protection_disables_purchase_actions(drafts,monkeypatch,breakage):
    repo,provider,intent,_=controlledPurchase(drafts,monkeypatch)
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft
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
        assert not repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft
    finally:
        query(drafts[3],restore)
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_completed_refund_is_no_longer_reported_as_a_financial_obligation(drafts,monkeypatch):
    from api.services.billing.manualObligationReport import listObligations
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    provider.refundStatus='processed'
    op=reserved(drafts[3], evidence)
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
    # The two refund emails wait for the notification bridge; they are messages, not money owed.
    emails=[row for row in unresolved if row['category'].startswith('notification')]
    assert sorted(row['category'] for row in emails)==['notification_unbridged']*2
    unresolved=[row for row in unresolved if row not in emails]
    # The returned money is settled. Only attempt-level facts remain: the live replacement and the
    # superseded Order, which stays scanned until its capture window passes.
    assert not [row for row in unresolved if row['providerPaymentId']==evidence.providerPaymentId or row['id']==str(op['id'])]
    assert sorted((row['category'],row['id']) for row in unresolved)==sorted([
        ('closed_attempt_unreconciled',intent.attemptId),
        ('capture_pending',repo.readEditor(intent.userId).checkout.attemptId)])


def test_failed_provider_refund_stays_owed_and_creates_one_case(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    provider.refundStatus='failed'
    service.execute(str(op['id']))
    service.recover(str(op['id']))
    row=repo.operationById(str(op['id']))
    assert row['state']=='needs_attention' and row['escalation_reference']=='refund:'+str(op['id'])
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft
    assert len(provider.refundCalls)==1


@pytest.mark.parametrize('purpose',['initial_purchase','expert_addition'])
def test_failed_refund_retries_new_submission_key(drafts,monkeypatch,purpose):
    from datetime import timedelta
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    from api.services.billing.paymentRefundService import PaymentRefundService
    repo,provider,intent,paymentId=duplicateCapture(drafts,monkeypatch,purpose)
    service=PaymentRefundService(repo,provider)
    op=reservedFor(drafts[3],paymentId)
    provider.refundStatus='failed'
    service.execute(str(op['id']))
    worker=PaymentOperationWorker(repo,provider)
    worker.execute(op['created_at']+timedelta(seconds=31))
    assert repo.operationById(str(op['id']))['state']=='queued'
    provider.refundStatus='processed'
    worker.execute(op['created_at']+timedelta(seconds=92))
    assert repo.operationById(str(op['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==2
    keys=[call[2]['headers']['X-Refund-Idempotency'] for call in provider.refundCalls]
    assert keys[0]!=keys[1] and [call[1]['notes']['purchaseSubmissionKey'] for call in provider.refundCalls]==keys
    assert all(call[1]['amount']==intent.amount for call in provider.refundCalls)


def test_installed_sdk_receives_timeout_and_cannot_repost_unknown_refund(drafts,monkeypatch):
    import json
    import razorpay
    from types import SimpleNamespace
    from api.services.billing.paymentRefundService import PaymentRefundService
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
    service=PaymentRefundService(repo,sdk)
    op=reserved(drafts[3], evidence)
    assert service.execute(str(op['id']))['state']=='outcome_unknown'
    assert service.execute(str(op['id']))['state']=='submitted'
    assert len(provider.refundCalls)==1
    assert requests and all(0<r.get('timeout',0)<=30 for r in requests)


def test_provider_internal_retries_cannot_bypass_durable_refund_submission(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    provider.retry_enabled=True
    service.execute(str(op['id']))
    assert not provider.refundCalls
    assert repo.operationById(str(op['id']))['state'] in ('queued','needs_attention')


@pytest.mark.parametrize('phase',['dual','v2'])
@pytest.mark.parametrize('currency',['INR','USD'])
@pytest.mark.parametrize('interval',['month','year'])
def test_full_original_currency_refund_in_each_supported_phase_and_interval(drafts,monkeypatch,phase,currency,interval):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE',phase)
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch,currency=currency,interval=interval)
    provider.refundStatus='processed'
    op=reserved(drafts[3], evidence)
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert op['currency']==currency and provider.refundCalls[0][1]['amount']==intent.amount
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed


def test_corrected_provider_configuration_can_resume_a_never_submitted_refund(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    provider.retry_enabled=True
    service.execute(str(op['id']))
    assert not provider.refundCalls
    provider.retry_enabled=False
    provider.refundStatus='processed'
    service.execute(str(op['id']))
    assert service.execute(str(op['id']))['state']=='succeeded'
    assert len(provider.refundCalls)==1


def test_external_pending_refund_must_finish_failing_before_the_remaining_refund_can_submit(drafts,monkeypatch):
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    pending=provider.externalRefund(evidence.providerPaymentId,100,'pending')
    op=reserved(drafts[3], evidence)
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
    op=reserved(drafts[3], evidence)
    assert service.execute(str(op['id']))['state']=='submitted'
    assert not provider.refundCalls
    assert repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft


def test_concurrent_retry_scheduling_uses_the_locked_retry_count(drafts,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Barrier
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
    original=repo.operationById
    rendezvous=Barrier(2)
    def simultaneousRead(key):
        row=original(key)
        rendezvous.wait(timeout=5)
        return row
    monkeypatch.setattr(repo,'operationById',simultaneousRead)
    now=op['created_at']+timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(repo.scheduleRefundRetry,str(op['id']),now) for _ in range(2)]
        assert all(not f.result(timeout=10) for f in futures)
    row=original(str(op['id']))
    assert row['retry_count']==2
    assert row['next_retry_at']==now+timedelta(seconds=60)


def test_replacement_late_capture_after_erasure_preserves_slot_ownership(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.paymentRefundService import PaymentRefundService
    repo,original,first,_=controlledPurchase(drafts,monkeypatch)
    provider=SyntheticRefundProvider()
    provider.orders=original.orders
    second=supersede(repo,provider,first,('banking',),'replacement-before-erasure')
    third=supersede(repo,provider,second,('telecom',),'second-replacement-before-erasure')
    runErasure(drafts[3],first.userId)
    payment=provider.authorize(second)
    provider.payments[payment['id']].update(status='captured',amount_refunded=0)
    evidence=VerifiedPaymentEvidence(second.attemptId,second.invoiceId,first.userId,second.razorpayOrderId,
        payment['id'],'initial_purchase',second.currency,'captured','server_observation',second.amount,_now(),None,None,False,
        paymentCreatedAt=datetime.fromtimestamp(payment['created_at'],timezone.utc))
    assert repo.finalizeCapturedPayment(evidence).state=='refund_pending'
    service=PaymentRefundService(repo,provider)
    op=reserved(drafts[3],evidence)
    provider.refundStatus='processed'
    failures=[]
    claim=repo.beginRefundSubmission
    def tracedClaim(*args):
        try:
            return claim(*args)
        except Exception as exc:
            failures.append(type(exc).__name__+':'+str(exc))
            raise
    monkeypatch.setattr(repo,'beginRefundSubmission',tracedClaim)
    result=service.execute(str(op['id']))
    assert result['state']=='succeeded', failures
    assert not repo.purchaseControl(second.attemptId)['user_id']
    slots={str(repo.purchaseControl(a)['slot_id']) for a in (first.attemptId,second.attemptId,third.attemptId)}
    assert len(slots)==1
    assert query(drafts[3],'SELECT user_id FROM payment_slots WHERE slot_id=%s',(slots.pop(),),one=True)['user_id'] is None
    assert len(provider.refundCalls)==1


@pytest.mark.parametrize('actual,expected',[('pending','submitted'),('processed','succeeded'),('failed','needs_attention')])
def test_refund_webhook_uses_provider_truth_without_resubmission(drafts,monkeypatch,actual,expected):
    from api.services.webhookService import WebhookService
    from api.services.subscriptions.subscriptionService import subscriptionService
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(drafts[3], evidence)
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


@pytest.mark.parametrize('kind,status,expected', [
    ('created', 'pending', 'submitted'), ('processed', 'processed', 'succeeded'),
    ('failed', 'failed', 'needs_attention')])
def test_refund_webhook_with_operation_note_never_claws_back(drafts, monkeypatch, kind, status, expected):
    from api.services.billing.paymentRefundService import PaymentRefundService
    from api.services.webhookService import WebhookService, EVENT_HANDLERS
    from api.services.subscriptions.subscriptionService import subscriptionService
    repo, provider, intent, paymentId = duplicateCapture(drafts, monkeypatch, 'topup')
    op = reservedFor(drafts[3], paymentId)
    PaymentRefundService(repo, provider).execute(str(op['id']))
    refund = next(iter(provider.refunds.values()))
    refund['status'] = status
    provider.payments[paymentId]['amount_refunded'] = intent.amount if status == 'processed' else 0
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    monkeypatch.setattr(subscriptionService, 'razorpayClient', provider)
    webhook = WebhookService.__new__(WebhookService)
    webhook._auditLog = lambda *args, **kwargs: None
    getattr(webhook, EVENT_HANDLERS['refund.' + kind])({'payload': {'refund': {'entity': dict(refund)}}})
    assert repo.operationById(str(op['id']))['state'] == expected
    balance = query(drafts[3], 'SELECT topup_tokens FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True)
    assert balance['topup_tokens'] == 5000000
    assert query(drafts[3], "SELECT id FROM billing_events WHERE user_id=%s AND event_type='credit.topup_refunded'",
                 (intent.userId,)) == []
    assert len(provider.refundCalls) == 1


def test_refund_failed_restores_nothing_and_schedules_retry(drafts, monkeypatch):
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    from api.services.webhookService import WebhookService, EVENT_HANDLERS
    from api.services.subscriptions.subscriptionService import subscriptionService
    repo, provider, intent, evidence, service = closedCapture(drafts, monkeypatch)
    op = reserved(drafts[3], evidence)
    service.execute(str(op['id']))
    refund = next(iter(provider.refunds.values()))
    oldKey = refund['notes']['purchaseSubmissionKey']
    refund['status'] = 'failed'
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    monkeypatch.setattr(subscriptionService, 'razorpayClient', provider)
    webhook = WebhookService.__new__(WebhookService)
    getattr(webhook, EVENT_HANDLERS['refund.failed'])({'payload': {'refund': {'entity': dict(refund)}}})
    assert repo.operationById(str(op['id']))['error_code'] == 'PAYMENT_REFUND_FAILED'
    worker = PaymentOperationWorker(repo, provider)
    assert worker.execute()['errors'] == 0
    retry = repo.operationById(str(op['id']))
    assert retry['state'] == 'queued' and retry['retry_count'] == 1
    assert retry['next_retry_at'] > datetime.now(timezone.utc)
    assert len(provider.refundCalls) == 1
    provider.refundStatus = 'processed'
    assert worker.execute(now=retry['next_retry_at'])['resolved'] == 1
    assert len(provider.refundCalls) == 2
    assert provider.refundCalls[-1][1]['notes']['purchaseSubmissionKey'] != oldKey
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed


@pytest.mark.parametrize('granted', [False, True])
def test_topup_refund_sql_outcome_and_replay(drafts, monkeypatch, granted):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, provider, intent, duplicateId = duplicateCapture(drafts, monkeypatch, 'topup')
    paymentId = query(drafts[3], 'SELECT "razorpayPaymentId" FROM "Invoices" WHERE id=%s',
                      (intent.invoiceId,), one=True)['razorpayPaymentId'] if granted else duplicateId
    credits = ManualCreditRepository(repo)
    refundId = 'topup-refund-' + str(uuid4())
    first = credits.clawbackTopup(intent.userId, refundId, paymentId, intent.amount)
    assert first == {'clawed': granted, 'tokens': 5000000 if granted else 0,
                     'status': 'clawed_back' if granted else 'not_granted'}
    assert credits.clawbackTopup(intent.userId, refundId, paymentId, intent.amount) == {
        'clawed': False, 'tokens': 0, 'status': 'already_applied'}
    balance = query(drafts[3], 'SELECT topup_tokens FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True)
    assert balance['topup_tokens'] == (0 if granted else 5000000)
    assert len(query(drafts[3], 'SELECT id FROM billing_events WHERE idempotency_key=%s', ('topup-refund:' + refundId,))) == 1


@pytest.mark.parametrize('purpose',['renewal','topup'])
def test_external_partial_refund_refunds_remainder(drafts,monkeypatch,purpose):
    from api.services.billing.paymentRefundService import PaymentRefundService
    repo,provider,intent,paymentId=duplicateCapture(drafts,monkeypatch,purpose)
    provider.externalRefund(paymentId,100,'processed')  # someone returned part of it in the dashboard
    provider.refundStatus='processed'
    op=reservedFor(drafts[3],paymentId)
    service=PaymentRefundService(repo,provider)
    assert service.execute(str(op['id']))['state']=='succeeded'
    service.execute(str(op['id']))
    assert [call[1]['amount'] for call in provider.refundCalls]==[intent.amount-100]


def test_refund_does_not_need_compensation_flag(drafts,monkeypatch):
    from pathlib import Path
    from api.services.billing.purchaseCheckoutService import purchaseActionsReady as actionsReady
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    flag='SUBSCRIPTION_CANCELLATION_'+'COMPENSATION_ENABLED'
    monkeypatch.delenv(flag,raising=False)
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    # The real readiness, not the fixture's stand-in: only the editable checkout flag and a fresh worker.
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady',actionsReady)
    op=reserved(drafts[3],evidence)
    provider.refundStatus='processed'
    PaymentOperationWorker(repo,provider).execute()
    assert repo.operationById(str(op['id']))['state']=='succeeded' and len(provider.refundCalls)==1
    assert actionsReady() and repo.readEditor(intent.userId).capabilities.maySubmitLatestDraft
    monkeypatch.setenv(flag,'false')
    assert actionsReady()
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    assert not actionsReady()
    root=Path(__file__).resolve().parents[1]
    assert not [str(path) for folder in ('api','nubrix') for path in (root/folder).rglob('*.py')
                if flag in path.read_text(encoding='utf-8')]


def presentStatus(repo, userId, now=None):
    from psycopg2.extras import RealDictCursor
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.paymentStatusPresentation import presentPaymentStatus
    def operation(connection):
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            return presentPaymentStatus(cursor, userId, now or _now())
    return repo._run(operation)


@pytest.mark.parametrize('currency', ['INR', 'USD'])
def test_payment_status_binds_refund_to_its_attempt_without_other_ids(drafts, monkeypatch, currency):
    import json
    from api.services.billing.paymentRefundService import PaymentRefundService
    from api.services.billing.paymentStatusPresentation import supportReference
    url = drafts[3]
    repo, provider, intent, paymentId = duplicateCapture(drafts, monkeypatch, 'topup', currency)
    op = reservedFor(url, paymentId)
    status = presentStatus(repo, intent.userId)
    # The granted attempt is completed; its duplicate payment is a refund on its way, not an error.
    assert status['checkout']['state'] == 'completed' and status['checkout']['attemptId'] == intent.attemptId
    assert status['refunds'] == [{'attemptId': intent.attemptId, 'amount': intent.amount, 'currency': currency,
        'reason': 'duplicate', 'state': 'on_its_way', 'startedAt': op['created_at'].isoformat()}]
    assert status['paymentAttention'] is None
    hidden = {intent.invoiceId, intent.razorpayOrderId, paymentId, str(op['id'])}
    assert not [identity for identity in hidden if identity in json.dumps(status['refunds'])]
    assert repo.escalateOperationOnce(str(op['id']), 'PAYMENT_REFUND_OVERDUE')
    assert presentStatus(repo, intent.userId)['paymentAttention'] == {'reference': supportReference(str(op['id']))}
    provider.refundStatus = 'processed'
    service = PaymentRefundService(repo, provider)
    service.execute(str(op['id']))
    provider.payments[paymentId]['amount_refunded'] = intent.amount
    service.recover(str(op['id']))
    assert repo.operationById(str(op['id']))['state'] == 'succeeded'
    done = presentStatus(repo, intent.userId)
    assert [refund['state'] for refund in done['refunds']] == ['returned'] and done['paymentAttention'] is None


def test_refund_notifications_enqueued_once(drafts,monkeypatch):
    import json
    import psycopg2
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    from api.services.subscriptions.paymentValidationService import parseUtc
    url=drafts[3]
    repo,provider,intent,evidence,service=closedCapture(drafts,monkeypatch)
    op=reserved(url,evidence)
    key=str(op['id'])
    repo.finalizeCapturedPayment(evidence)  # a replayed finalization reserves nothing new
    assert set(refundIntents(url,key))=={'started'}
    service.execute(key)  # submitted, not yet processed by the provider
    assert repo.operationById(key)['state']=='submitted' and set(refundIntents(url,key))=={'started'}
    refund=next(iter(provider.refunds.values()))
    refund['status']='processed'
    provider.payments[evidence.providerPaymentId]['amount_refunded']=intent.amount
    for _ in range(2):
        service.recover(key)
        service.execute(key)
    assert repo.operationById(key)['state']=='succeeded'
    intents=refundIntents(url,key)
    assert set(intents)=={'started','completed'}
    hidden={intent.invoiceId,intent.attemptId,intent.razorpayOrderId,evidence.providerPaymentId,key}
    for milestone,row in intents.items():
        committed=row['metadata_json']
        assert row['user_id']==intent.userId and committed['userId']==intent.userId
        assert committed['notificationType']=='payment_refund_'+milestone
        assert committed['dedupeKey']=='payment_refund:'+key+':'+milestone
        facts=committed['metadata']
        assert (facts['amount'],facts['currency'],facts['reason'])==(intent.amount,'INR','paid_after_close')
        assert parseUtc(facts['startedAt'])==op['created_at']
        # Customer-facing facts only: no invoice, attempt, Order, payment or operation identity.
        assert not [identity for identity in hidden if identity in json.dumps(facts)]
    assert 'completedAt' in intents['completed']['metadata_json']['metadata']
    # The durable delivery pipeline accepts both types once.
    deliveries=NotificationDeliveryRepository(lambda: psycopg2.connect(url))
    created=[]
    for _ in range(2):
        for row in intents.values():
            committed=row['metadata_json']
            delivery,isNew=deliveries.enqueueBillingNotification(userId=committed['userId'],
                subscriptionId=committed['subscriptionId'],notificationType=committed['notificationType'],
                dedupeKey=committed['dedupeKey'],periodEnd=committed['periodEnd'],metadata=committed['metadata'])
            created.append(isNew)
    assert created==[True,True,False,False]
    # As the earlier build left them: held for the template, the claim's attempt given back.
    query(url,"""UPDATE notification_deliveries SET status='RETRY_PENDING',attempt_count=0,last_error_code='TEMPLATE_PENDING',
        next_attempt_at=now()-interval '1 second' WHERE user_id=%s""",(intent.userId,))
    sent=dispatchRefundEmails(url,intent.userId,monkeypatch)
    assert sorted(p['notificationType'] for p in sent)==['payment_refund_completed','payment_refund_started']
    for payload in sent:
        assert not [identity for identity in hidden if identity in json.dumps(payload['metadata'])]
    assert dispatchRefundEmails(url,intent.userId,monkeypatch)==[]  # delivered once, never again
    final=query(url,'SELECT status,attempt_count FROM notification_deliveries WHERE user_id=%s',(intent.userId,))
    assert [(row['status'],row['attempt_count']) for row in final]==[('ACCEPTED',1)]*2


def dispatchRefundEmails(url, userId, monkeypatch):
    """One real dispatch pass (claim, eligibility, locked submission fence, accept) with a recording edge."""
    monkeypatch.setenv('DATABASE_URL', url)  # the subscription authority read during eligibility
    import psycopg2
    from api.services.notifications.edgeEmailClient import EdgeSendResult
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    from api.services.notifications.notificationDeliveryService import NotificationDeliveryService
    from test.test_notification_delivery_service import FakeEdgeClient, FakeLedger, FakeSupabase
    subscriptions=[{key:(str(value) if key=='id' else value) for key,value in row.items()} for row in query(url,
        'SELECT id,user_id,erasure_pending,is_canonical,cancellation_requested_at FROM subscriptions WHERE user_id=%s',(userId,))]
    edge=FakeEdgeClient([EdgeSendResult(outcome='ACCEPTED',messageId='refund-message-'+str(uuid4())) for _ in range(10)])
    service=NotificationDeliveryService(repository=NotificationDeliveryRepository(lambda: psycopg2.connect(url)),
        edgeClient=edge,supabaseClient=FakeSupabase(subscriptions=subscriptions,
            users=[{'userId':userId,'email':'refund@example.test','fullName':'Refund Customer'}]),
        eventService=FakeLedger(),brevoClient=object(),now=lambda: datetime.now(timezone.utc))
    summary=service.dispatchBatch('refund-email-test')
    assert summary['errors']==0 and summary['failed']==0 and summary['cancelled']==0
    return edge.payloads
