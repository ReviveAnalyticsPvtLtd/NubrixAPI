"""Cross-path financial regressions using disposable PostgreSQL and synthetic provider evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest

from test.test_capture_finalization_postgres import (pytestmark, postgres, runtimeDb, erasureDb,
    checkoutDb, drafts, query, providerPayment, finalize, evidenceFor, control, stripFrozen,
    paidAccount, purposePay, capturedNow)
from test.test_payment_refunds_postgres import SyntheticRefundProvider


def externalRefundHandler(repo, provider, monkeypatch):
    from api.services.webhookService import WebhookService
    from api.services.subscriptions.subscriptionService import subscriptionService
    from api.services.credits.creditService import creditService
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    monkeypatch.setattr(subscriptionService, 'razorpayClient', provider)
    monkeypatch.setattr(creditService, 'clawbackTopupTokens', ManualCreditRepository(repo).clawbackTopup)
    webhook = WebhookService.__new__(WebhookService)
    webhook._auditLog = lambda *args, **kwargs: None
    return lambda refund: webhook._handleRefundProcessed({'payload': {'refund': {'entity': refund}}})


@pytest.mark.parametrize('order', ['refund_first', 'capture_first', 'concurrent'])
@pytest.mark.parametrize('parts', ['full', 'partial', 'cumulative'])
def test_external_refund_and_capture_order_has_identical_credit_result(drafts, monkeypatch, order, parts):
    repo, original, initial = paidAccount(drafts, monkeypatch)
    provider = SyntheticRefundProvider()
    provider.orders = original.orders
    topup = purposePay(repo, provider, initial.userId, 'topup', 'monthly_prepaid', {'packId': 'medium'})
    payment = dict(providerPayment(provider, topup, 'captured'))  # stale authentic capture entity
    payment['notes'] = {'type': 'credit_topup', 'userId': topup.userId}
    provider.payments[payment['id']] = dict(payment)
    amounts = [topup.amount] if parts == 'full' else [topup.amount // 3] if parts == 'partial' else [1, topup.amount // 3, 1]
    refunds = [provider.externalRefund(payment['id'], amount) for amount in amounts]
    handle = externalRefundHandler(repo, provider, monkeypatch)
    refund = lambda: [handle(item) for item in refunds]
    capture = lambda: finalize(repo, topup, payment)
    if order == 'refund_first':
        refund(); capture()
    elif order == 'capture_first':
        capture(); refund()
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(refund), pool.submit(capture)]
            [future.result() for future in futures]
    tokens = topup.snapshot['tokens']
    expected = tokens - tokens * sum(amounts) // topup.amount
    def balance():
        return query(drafts[3], 'SELECT topup_tokens FROM credit_balances WHERE user_id=%s', (topup.userId,), one=True)['topup_tokens']
    assert balance() == expected  # no refund replay required for correct balance
    refund(); capture(); refund()
    assert balance() == expected
    rows = query(drafts[3], "SELECT event_status FROM billing_events WHERE idempotency_key LIKE 'topup-refund:%%' AND user_id=%s", (topup.userId,))
    assert len(rows) == len(refunds) and all(row['event_status'] == 'SETTLED' for row in rows)


def legacyUncontrolled(drafts, monkeypatch):
    from unittest.mock import patch
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo, _, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED',raising=False)
    with patch('api.services.billing.purchaseCheckoutRepository.bindPurchaseControlLocked', lambda *args: None):
        intent = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
            {'domains': ['banking']}, 'legacy-' + str(uuid4()), expectedCatalogRevision=1))
    provider = SyntheticRefundProvider()
    intent = repo.bindProviderOrder(intent.attemptId, provider.createOrder(
        {'amount': intent.amount, 'currency': intent.currency, 'receipt': intent.attemptId}))
    stripFrozen(url, intent.attemptId, 'capturePolicy')
    return repo, provider, intent


@pytest.mark.parametrize('alreadyDetached', [False, True])
@pytest.mark.parametrize('policy',['account_default','automatic'])
def test_legacy_erasure_preserves_exact_refund_authority(drafts, monkeypatch, alreadyDetached,policy):
    from test.test_subscription_erasure_postgres import runErasure
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    repo, provider, intent = legacyUncontrolled(drafts, monkeypatch)
    if policy=='automatic':
        query(drafts[3],"UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,capturePolicy}','\"automatic\"') WHERE id=%s",(intent.attemptId,))
    if alreadyDetached:
        # Simulate the old erasure implementation before controls were established.
        monkeypatch.setattr('api.services.userErasureRepository.preparePaymentControlsForErasure', lambda *args: None, raising=False)
    _, request = runErasure(drafts[3], intent.userId)
    original = query(drafts[3], 'SELECT source_table,source_id,original_record FROM private.retained_user_records WHERE request_id=%s ORDER BY source_table,source_id', (request,))
    item = providerPayment(provider, intent, 'captured')
    provider.payments[item['id']]['amount_refunded']=0
    provider.refundStatus = 'processed'
    assert finalize(repo, intent, item).state == 'refund_pending'
    assert finalize(repo, intent, item).state == 'refund_pending'
    assert control(drafts[3],intent.attemptId)['capture_policy']==policy
    worker = PaymentOperationWorker(repo, provider)
    worker.execute(); worker.execute()
    ops = query(drafts[3], 'SELECT * FROM payment_operations WHERE provider_payment_id=%s', (item['id'],))
    assert len(ops) == 1 and ops[0]['state'] == 'succeeded' and ops[0]['reason'] == 'account_closed'
    assert len(provider.refundCalls) == 1 and provider.refundCalls[0][1]['amount'] == intent.amount
    assert original == query(drafts[3], 'SELECT source_table,source_id,original_record FROM private.retained_user_records WHERE request_id=%s ORDER BY source_table,source_id', (request,))
    assert query(drafts[3], 'SELECT * FROM subscriptions WHERE user_id=%s', (intent.userId,)) == []


def test_proven_order_rejection_has_distinct_evidence_and_retries(drafts, monkeypatch):
    from razorpay.errors import BadRequestError
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo, _, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    provider = SyntheticRefundProvider()
    create = provider.order.create
    def reject(*args, **kwargs):
        raise BadRequestError('synthetic rejected creation')
    provider.order.create = reject
    service = ManualPaymentService.forProduction(provider, repo)
    request = CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, 'rejected', expectedCatalogRevision=1)
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        service.createCheckout(request)
    row = query(url, "SELECT * FROM billing_events WHERE user_id=%s AND event_category='payment_attempt'", (user,), one=True)
    frozen = row['metadata_json']['manualBilling']
    assert frozen['closedReason'] == 'ORDER_NOT_CREATED' and frozen.get('providerOrderRejectedAt')
    assert 'zeroOrderLookupAt' not in frozen and row['provider_order_id'] is None
    provider.order.create = create
    assert service.createCheckout(replace(request, requestKey='retry')).razorpayOrderId


@pytest.mark.parametrize('lateAuthorization',[False,True])
def test_registered_minute_task_recovers_recent_superseded_capture(drafts, monkeypatch,lateAuthorization):
    from test.test_capture_finalization_postgres import startedPurchaseThenReplaced
    from nubrix.triggers.celery import celeryApp
    from api.services.subscriptions.subscriptionService import subscriptionService
    repo, provider, first, second, payment = startedPurchaseThenReplaced(drafts, monkeypatch)
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    monkeypatch.setattr(subscriptionService, 'razorpayClient', provider)
    entry = celeryApp.conf.beat_schedule['payment-operations-every-minute']
    assert entry['schedule'].minute == set(range(60))
    task = celeryApp.tasks[entry['task']]
    if lateAuthorization:
        provider.payments[payment['id']]['status']='authorized'
        task.run()
        assert not repo.getCoverageSnapshot(first.userId).accessAllowed
        query(drafts[3],"UPDATE billing_events SET metadata_json=metadata_json-'lastRecoveryAt' WHERE id=%s",(first.attemptId,))
    capturedNow(provider, payment['id'])
    task.run()
    assert repo.getCoverageSnapshot(first.userId).accessAllowed
    assert query(drafts[3], 'SELECT payment_status FROM billing_events WHERE id=%s', (first.attemptId,), one=True)['payment_status'] == 'captured'


def test_worker_provider_outage_backs_off_and_escalates_once(drafts, monkeypatch):
    from test.test_payment_refunds_postgres import closedCapture, reserved
    from api.services.billing.paymentOperationWorker import PaymentOperationWorker
    repo, provider, intent, evidence, _ = closedCapture(drafts, monkeypatch)
    op = reserved(drafts[3], evidence)
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise TimeoutError('synthetic provider outage')
    provider.payment.fetch = unavailable
    worker = PaymentOperationWorker(repo, provider)
    now = datetime.now(timezone.utc)
    worker.execute(now=now)
    first = repo.operationById(str(op['id']))
    assert first['retry_count'] == 1 and first['next_retry_at'] > now
    worker.execute(now=now)
    assert len(calls) == 1
    query(drafts[3], "UPDATE payment_operations SET next_retry_at=clock_timestamp()-interval '1 second' WHERE id=%s", (op['id'],))
    later = now + timedelta(minutes=31)
    worker.execute(now=later)
    escalated = repo.operationById(str(op['id']))
    assert escalated['escalation_reference']
    worker.execute(now=escalated['next_retry_at'])
    assert repo.operationById(str(op['id']))['escalation_reference'] == escalated['escalation_reference']
    assert provider.refundCalls == []


@pytest.mark.parametrize('field', ['id','order_id','currency','amount','owner'])
def test_pregrant_refund_rejects_unbound_fetched_payment(drafts, monkeypatch, field):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, provider, initial = paidAccount(drafts,monkeypatch)
    topup=purposePay(repo,provider,initial.userId,'topup','monthly_prepaid',{'packId':'medium'})
    payment=dict(providerPayment(provider,topup,'captured'))
    changed={**payment,field: {'id':'wrong','order_id':'wrong','currency':'USD','amount':topup.amount+1}.get(field)}
    user=topup.userId
    if field=='owner':
        user='foreign-'+str(uuid4())
        query(drafts[3],'INSERT INTO "Users"("userId") VALUES(%s)',(user,))
        repo.ensureCanonicalSubscription(user)
    with pytest.raises(ValueError,match='TOPUP_REFUND_PAYMENT_'):
        ManualCreditRepository(repo).clawbackTopup(user,'refund-'+str(uuid4()),payment['id'],1,paymentEvidence=changed)
    assert query(drafts[3],"SELECT id FROM billing_events WHERE user_id=%s AND event_type='credit.topup_refund_pending'",(user,))==[]


@pytest.mark.parametrize('mutation', ['order','amount','currency','purpose','request','invoice'])
def test_detached_legacy_authority_requires_exact_retained_binding(drafts,monkeypatch,mutation):
    from test.test_subscription_erasure_postgres import runErasure
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    monkeypatch.setattr('api.services.userErasureRepository.preparePaymentControlsForErasure',lambda *args:None)
    _,request=runErasure(drafts[3],intent.userId)
    url=drafts[3]
    if mutation=='request':
        request=str(uuid4())
    elif mutation=='invoice':
        query(url,'UPDATE "Invoices" SET total_amount=total_amount+1 WHERE id=%s',(intent.invoiceId,))
    elif mutation=='purpose':
        query(url,"UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,purpose}','\"topup\"') WHERE id=%s",(intent.attemptId,))
    else:
        assignments={'order':"provider_order_id='wrong-order'",'amount':'amount=amount+1','currency':"currency='USD'"}
        query(url,'UPDATE billing_events SET '+assignments[mutation]+' WHERE id=%s',(intent.attemptId,))
    assert query(url,'SELECT private.exact_retained_payment_attempt(%s,%s,%s) AS valid',
                 (intent.attemptId,intent.invoiceId,request),one=True)['valid'] is False
    import psycopg2
    with pytest.raises(psycopg2.Error,match='retained payment authority'):
        query(url,"INSERT INTO payment_slots(slot_id,kind,slot_key,erasure_request_id) VALUES(%s,'pro_term',%s,%s)",
              (intent.attemptId,'retained:'+intent.attemptId,request))
    assert query(url,'SELECT * FROM payment_operations WHERE attempt_id=%s',(intent.attemptId,))==[]


def test_retained_originals_cannot_be_rewritten_and_refund_slot_cannot_grant(drafts,monkeypatch):
    import psycopg2
    from test.test_subscription_erasure_postgres import runErasure
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    monkeypatch.setattr('api.services.userErasureRepository.preparePaymentControlsForErasure',lambda *args:None)
    _,request=runErasure(drafts[3],intent.userId)
    with pytest.raises(psycopg2.Error):
        query(drafts[3],"UPDATE private.retained_user_records SET original_record='{}' WHERE request_id=%s",(request,))
    assert finalize(repo,intent,providerPayment(provider,intent,'captured')).state=='refund_pending'
    with pytest.raises(psycopg2.Error):
        query(drafts[3],"UPDATE payment_slot_controls SET closed_at=NULL,closed_reason=NULL,operation_revision=operation_revision+1 WHERE attempt_id=%s",(intent.attemptId,))
    with pytest.raises(psycopg2.Error):
        query(drafts[3],"UPDATE payment_slots SET settled_payment_id='invented',settled_at=clock_timestamp() WHERE slot_id=%s",(intent.attemptId,))
    assert query(drafts[3],"SELECT * FROM private.retained_user_records WHERE source_table='payment_slot_controls' AND source_id=%s",(intent.attemptId,))==[]


def test_detached_legacy_pre_cutover_money_remains_historical(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    monkeypatch.setattr('api.services.userErasureRepository.preparePaymentControlsForErasure',lambda *args:None)
    runErasure(drafts[3],intent.userId)
    monkeypatch.setenv('PAYMENT_RULES_EFFECTIVE_AT',(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
    item=providerPayment(provider,intent,'captured')
    assert finalize(repo,intent,item).state=='requires_reconciliation'
    assert control(drafts[3],intent.attemptId) is None
    assert query(drafts[3],'SELECT * FROM payment_operations WHERE attempt_id=%s',(intent.attemptId,))==[]


def test_provider_rejection_cannot_close_a_concurrently_bound_order(drafts,monkeypatch):
    from razorpay.errors import BadRequestError
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo,_,user,url=drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    provider=SyntheticRefundProvider()
    def concurrentBind(payload,**options):
        order=provider.createOrder(payload)
        repo.bindProviderOrder(payload['receipt'],order)
        raise BadRequestError('other call rejected')
    provider.order.create=concurrentBind
    request=CheckoutRequest(user,'initial_purchase','monthly_prepaid',{'domains':['banking']},'race',expectedCatalogRevision=1)
    with pytest.raises(RuntimeError,match='ORDER_ACK_UNKNOWN'):
        ManualPaymentService.forProduction(provider,repo).createCheckout(request)
    row=query(url,"SELECT * FROM billing_events WHERE user_id=%s AND event_category='payment_attempt'",(user,),one=True)
    assert row['provider_order_id'] and row['payment_status']=='pending_provider_ack'
    assert not row['metadata_json']['manualBilling'].get('closedAt')
    assert not row['metadata_json']['manualBilling'].get('providerOrderRejectedAt')


@pytest.mark.parametrize('terminal',['failed','refunded'])
def test_terminal_listing_releases_shared_authorization_consumers(drafts,monkeypatch,terminal):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight,unresolvedMoneyFacts
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    item=providerPayment(provider,intent,'authorized')
    repo.finalizeCapturedPayment(evidenceFor(intent,item))
    row=repo.attemptById(intent.userId,intent.attemptId)
    assert row['metadata_json']['manualBilling']['providerObservation']['latestPaymentCreatedAt']==datetime.fromtimestamp(item['created_at'],timezone.utc).isoformat()
    now=datetime.now(timezone.utc)
    assert paymentInFlight(row,now)
    recovery=ManualBillingRecoveryService(repo,provider)
    provider.payments[item['id']]['status']=terminal
    repo.recordProviderObservation(intent.attemptId,recovery.inspectAttempt(row))
    row=repo.attemptById(intent.userId,intent.attemptId)
    assert not paymentInFlight(row,now)
    assert unresolvedMoneyFacts([], [row], now)==(False,[])
    assert repo._slotMoneyInFlight([(row,row['metadata_json']['manualBilling'])],set(),now) is None
    # An outage after conclusive terminal evidence must not resurrect stale SQL status.
    snapshot=recovery.inspectAttempt(row)
    repo.recordProviderObservation(intent.attemptId,replace(snapshot,complete=False,outcome='unknown',observedAt=now+timedelta(seconds=1)))
    assert not paymentInFlight(repo.attemptById(intent.userId,intent.attemptId),now)


def test_minute_capture_inventory_is_bounded_fair_and_independent_of_status_writes(drafts,monkeypatch):
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    rows=repo.recentCaptureRecoveryRows(limit=1)
    assert len(rows)==1
    first=str(rows[0]['id'])
    repo.recordRecoveryCheck(first,'no_progress')
    assert first not in [str(row['id']) for row in repo.recentCaptureRecoveryRows(limit=100)]
    query(drafts[3],"UPDATE billing_events SET metadata_json=metadata_json-'lastRecoveryAt',updated_at=clock_timestamp() WHERE id=%s",(intent.attemptId,))
    assert intent.attemptId in [str(row['id']) for row in repo.recentCaptureRecoveryRows(limit=100)]


def test_refresh_claim_is_independent_for_another_owner(drafts,monkeypatch):
    from api.services.billing.purchaseCheckoutStatusService import PurchaseCheckoutStatusService
    repo,provider,first=legacyUncontrolled(drafts,monkeypatch)
    other='other-'+str(uuid4())
    query(drafts[3],'INSERT INTO "Users"("userId") VALUES(%s)',(other,))
    repo.ensureCanonicalSubscription(other)
    _,_,second=legacyUncontrolled((repo,drafts[1],other,drafts[3]),monkeypatch)
    status=PurchaseCheckoutStatusService(repo,provider)
    assert status._claimRefresh(first.userId,first.attemptId)
    assert not status._claimRefresh(first.userId,first.attemptId)
    assert not status._claimRefresh(other,first.attemptId)
    assert status._claimRefresh(other,second.attemptId)


def test_detached_refund_refuses_conflicting_deterministic_slot(drafts,monkeypatch):
    from test.test_subscription_erasure_postgres import runErasure
    repo,provider,intent=legacyUncontrolled(drafts,monkeypatch)
    query(drafts[3],"INSERT INTO payment_slots(slot_id,user_id,kind,slot_key) VALUES(%s,%s,'pro_term','different')",(intent.attemptId,intent.userId))
    monkeypatch.setattr('api.services.userErasureRepository.preparePaymentControlsForErasure',lambda *args:None)
    runErasure(drafts[3],intent.userId)
    with pytest.raises(ValueError,match='RETAINED_PAYMENT_SLOT_CONFLICT'):
        finalize(repo,intent,providerPayment(provider,intent,'captured'))
    assert control(drafts[3],intent.attemptId) is None
    assert query(drafts[3],'SELECT * FROM payment_operations WHERE attempt_id=%s',(intent.attemptId,))==[]


@pytest.mark.parametrize('terminal',['failed','refunded'])
def test_terminal_authorization_releases_annual_scheduler_before_window_end(drafts,monkeypatch,terminal):
    from test.test_payment_replacement_postgres import annualRenewalInvoice
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo,provider,initial=paidAccount(drafts,monkeypatch,'year')
    invoice=annualRenewalInvoice(repo,initial.userId)
    attempt=purposePay(repo,provider,initial.userId,'renewal','annual_prepaid',{'invoiceId':str(invoice['id'])})
    payment=providerPayment(provider,attempt,'authorized')
    recovery=ManualBillingRecoveryService(repo,provider)
    repo.recordProviderObservation(attempt.attemptId,recovery.inspectAttempt(repo.attemptById(initial.userId,attempt.attemptId)))
    query(drafts[3],'''UPDATE "Invoices" SET status='EXPIRED',metadata_json=metadata_json||'{"repricingRequired":true}'::jsonb
        WHERE id=%s''',(invoice['id'],))
    with pytest.raises(RuntimeError,match='RENEWAL_REPRICING_DEFERRED'):
        repo.prepareAnnualRenewalInvoice(str(invoice['id']))
    provider.payments[payment['id']]['status']=terminal
    repo.recordProviderObservation(attempt.attemptId,recovery.inspectAttempt(repo.attemptById(initial.userId,attempt.attemptId)))
    assert repo.prepareAnnualRenewalInvoice(str(invoice['id']))['metadata_json']['manualBilling']['revision']==2
