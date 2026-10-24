"""Pay decides open, reuse, replace or wait for the initial purchase slot; real SQL, synthetic provider."""
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from psycopg2.extras import RealDictCursor

from test.purchaseProviderFixtures import SyntheticPurchaseProvider
from test.test_purchase_checkout_postgres import checkoutDb, drafts, controlledPurchase  # noqa:F401
from test.test_subscription_erasure_postgres import erasureDb  # noqa:F401
from test.subscriptionRuntimeFixtures import postgres, runtimeDb, query  # noqa:F401

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Disposable PostgreSQL opt-in; skip is UNVERIFIED')

BUSY_COPY = 'Payment service is busy. Please try again in a moment.'
LIVE = ('created', 'pending_provider_ack', 'authorized')


def pricing(user, domains, interval='month', currency='INR'):
    return {'userId': user, 'domains': list(domains), 'currency': currency, 'catalogRevision': 1,
            'billingMode': 'monthly_prepaid' if interval == 'month' else 'annual_prepaid'}


def pay(repo, provider, user, revision, domains, interval='month', key=None, currency='INR'):
    from api.services.billing.manualPaymentService import ManualPaymentService
    request = repo.draftCheckoutRequest(user, revision, pricing(user, domains, interval, currency), key or 'pay-' + str(uuid4()))
    return ManualPaymentService.forProduction(provider, repo).createCheckout(request)


def legacyPay(billing, provider, user, domains, key):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    from api.services.billing.manualPaymentService import ManualPaymentService
    return ManualPaymentService.forProduction(provider, billing).createCheckout(CheckoutRequest(
        user, 'initial_purchase', 'monthly_prepaid', {'domains': list(domains)}, key, expectedCatalogRevision=1))


def http(repo, provider, user, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    import api.routers.subscriptions as routes
    monkeypatch.setenv('PRICING_PROXY_SECRET', 'synthetic-test-pricing')
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    monkeypatch.setattr(routes.subscriptionService, 'razorpayClient', provider)
    token = jwt.encode({'userId': user, 'email': 'synthetic@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    return TestClient(app)


def submit(client, user, revision, key, currency='INR'):
    from api.services.billing.pricingContext import issuePricingContext
    return client.post('/purchase-draft/checkout', headers={'Idempotency-Key': key}, json={
        'draftRevision': revision, 'expectedCatalogRevision': 1, 'contact': '+919999999999',
        'pricingContext': issuePricingContext(user, currency, 'hosting', catalogRevision=1)})


def control(url, attemptId):
    return query(url, 'SELECT * FROM payment_slot_controls WHERE attempt_id=%s', (attemptId,), one=True)


def frozen(url, attemptId):
    row = query(url, 'SELECT payment_status,metadata_json FROM billing_events WHERE id=%s', (attemptId,), one=True)
    return row['payment_status'], row['metadata_json']['manualBilling']


def liveInitial(url, user):
    return query(url, '''SELECT id FROM billing_events WHERE user_id=%s AND event_category='payment_attempt'
        AND payment_status IN ('created','pending_provider_ack','authorized')
        AND metadata_json->'manualBilling'->>'purpose'='initial_purchase'
        AND metadata_json->'manualBilling'->>'closedAt' IS NULL''', (user,))


def financialRows(url, user):
    return (query(url, 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE "userId"=%s ORDER BY id', (user,)),
            query(url, '''SELECT id,payment_status,provider_order_id,metadata_json->'manualBilling'->>'closedAt' AS closed
                FROM billing_events WHERE user_id=%s AND event_category='payment_attempt' ORDER BY id''', (user,)),
            query(url, 'SELECT to_jsonb(c) AS row FROM payment_slot_controls c WHERE user_id=%s ORDER BY attempt_id', (user,)))


def payment(provider, intent, status, created=None):
    item = provider.authorize(intent)
    provider.payments[item['id']].update(status=status, created_at=int(created if created is not None else time.time()))
    return provider.payments[item['id']]


@pytest.mark.parametrize('currency', ['INR', 'USD'])
@pytest.mark.parametrize('interval', ['month', 'year'])
@pytest.mark.parametrize('original,changed', [(('banking',), ['banking', 'telecom']), (('banking', 'telecom'), ['banking'])])
def test_changed_selection_replaces_empty_order_without_user_action(drafts, monkeypatch, currency, interval, original, changed):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch, original, interval, currency)
    url = drafts[3]
    oldInvoice = query(url, 'SELECT amount,total_amount,currency FROM "Invoices" WHERE id=%s', (old.invoiceId,), one=True)
    repo.saveDraft(old.userId, 1, changed, interval)
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-changed', currency)
    assert response.status_code == 200, response.text
    checkout = response.json()['checkout']
    assert checkout['attemptId'] != old.attemptId and checkout['domains'] == sorted(changed)
    assert checkout['currency'] == currency and checkout['billingMode'] == old.billingMode
    # The superseded invoice keeps its frozen money facts.
    assert query(url, 'SELECT amount,total_amount,currency FROM "Invoices" WHERE id=%s', (old.invoiceId,), one=True) == oldInvoice
    assert checkout['orderId'] and checkout['orderId'] != old.razorpayOrderId
    assert len(provider.orderCalls) == 2
    assert provider.orderCalls[1]['payment'] == {'capture': 'automatic', 'capture_options': {
        'automatic_expiry_period': 45, 'manual_expiry_period': 45, 'refund_speed': 'normal'}}
    status, billing = frozen(url, old.attemptId)
    assert status == 'cancelled' and billing['closedReason'] == 'SUPERSEDED' and billing['closedAt']
    assert not billing.get('closureReconciledAt')
    invoice = query(url, 'SELECT status,metadata_json FROM "Invoices" WHERE id=%s', (old.invoiceId,), one=True)
    assert invoice['status'] == 'VOID'
    assert invoice['metadata_json']['manualBilling']['supersededByInvoiceId'] == checkout['invoiceId']
    closed, replacement = control(url, old.attemptId), control(url, checkout['attemptId'])
    assert closed['closed_reason'] == 'SUPERSEDED' and str(closed['superseded_by']) == checkout['attemptId']
    # The superseded Checkout could start a payment until its own deadline (spec 4.3).
    assert closed['recover_until'] == datetime.fromisoformat(billing['expiresAt']) + timedelta(minutes=45)
    assert closed['slot_id'] == replacement['slot_id'] and replacement['capture_policy'] == 'automatic'
    assert replacement['closed_at'] is None and replacement['draft_revision'] == 2
    assert [str(row['id']) for row in liveInitial(url, old.userId)] == [checkout['attemptId']]


def test_same_selection_second_device_reuses_order(drafts, monkeypatch):
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    second = pay(repo, provider, first.userId, 1, ['banking', 'manufacturing'], key='second-device')
    assert second.attemptId == first.attemptId and second.razorpayOrderId == first.razorpayOrderId
    again = pay(repo, provider, first.userId, 1, ['banking', 'manufacturing'], key='second-device')
    assert again.attemptId == first.attemptId
    assert len(provider.orderCalls) == 1
    assert query(drafts[3], 'SELECT count(*) AS n FROM "Invoices" WHERE "userId"=%s', (first.userId,), one=True)['n'] == 1


def test_authorized_old_payment_returns_confirming(drafts, monkeypatch):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    payment(provider, old, 'authorized')
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    before = query(drafts[3], 'SELECT count(*) AS n FROM "Invoices" WHERE "userId"=%s', (old.userId,), one=True)['n']
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-while-authorized')
    assert response.status_code == 200, response.text
    checkout = response.json()['checkout']
    # Nothing that could reopen checkout on the held Order is returned.
    assert set(checkout) == {'state', 'attemptId', 'earlierPayment'}
    assert checkout['state'] == 'confirming_earlier_payment' and checkout['attemptId'] == old.attemptId
    assert checkout['earlierPayment'] == {'selectionLabel': 'Pro monthly with Banking and Manufacturing',
                                          'amount': old.amount, 'currency': 'INR'}
    intent = pay(repo, provider, old.userId, 2, ['telecom'])
    assert intent.state == 'confirming_earlier_payment' and intent.earlierPayment['amount'] == old.amount
    assert serializeCheckoutIntent(intent, 'key', {'userContact': '+91'}) == {'state': 'confirming_earlier_payment',
        'attemptId': old.attemptId, 'earlierPayment': intent.earlierPayment}
    assert query(drafts[3], 'SELECT count(*) AS n FROM "Invoices" WHERE "userId"=%s', (old.userId,), one=True)['n'] == before
    assert len(provider.orderCalls) == 1 and not provider.captureCalls
    assert control(drafts[3], old.attemptId)['closed_at'] is None


def test_dead_authorized_or_created_payment_does_not_hold_pay(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    started = time.time() - 46 * 60
    payment(provider, old, 'authorized', created=started)
    payment(provider, old, 'created')
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    new = pay(repo, provider, old.userId, 2, ['telecom'])
    assert new.attemptId != old.attemptId and new.razorpayOrderId
    closed = control(drafts[3], old.attemptId)
    # The later of the newest payment and the old Checkout's deadline bounds the late-authorization scan.
    newest = max(p['created_at'] for p in provider.payments.values())
    deadline = datetime.fromisoformat(frozen(drafts[3], old.attemptId)[1]['expiresAt']).timestamp()
    assert abs((closed['recover_until'] - timedelta(minutes=45)).timestamp() - max(newest, deadline)) < 1


def test_captured_old_payment_is_finalized_first_and_returns_completed(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    payment(provider, old, 'captured')
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    result = pay(repo, provider, old.userId, 2, ['telecom'])
    assert result.attemptId == old.attemptId and result.state == 'captured'
    assert len(provider.orderCalls) == 1
    assert repo.getCoverageSnapshot(old.userId).currentPeriod.domains == ('banking', 'manufacturing')


def test_provider_unreachable_returns_busy_and_changes_nothing(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    before = financialRows(drafts[3], old.userId)
    def unreachable(*args, **kwargs):
        raise TimeoutError('synthetic provider outage')
    provider.order.payments = unreachable
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-outage')
    assert response.status_code == 503
    assert response.json() == {'message': BUSY_COPY, 'errorCode': 'PAYMENT_SERVICE_BUSY'}
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        pay(repo, provider, old.userId, 2, ['telecom'])
    assert financialRows(drafts[3], old.userId) == before
    assert len(provider.orderCalls) == 1


def test_concurrent_pay_serializes_to_one_live_order(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    provider = SyntheticPurchaseProvider()
    first = legacyPay(billing, provider, user, ['banking'], 'first-tab')
    rendezvous = Barrier(2)
    inspect = ManualBillingRecoveryService.inspectAttempt
    def bothInspectFirst(self, attempt):
        snapshot = inspect(self, attempt)
        rendezvous.wait(timeout=10)
        return snapshot
    monkeypatch.setattr(ManualBillingRecoveryService, 'inspectAttempt', bothInspectFirst)
    def attempt(domain):
        try:
            return legacyPay(billing, provider, user, [domain], 'tab-' + domain)
        except RuntimeError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['telecom', 'supplychain']))
    opened = [r for r in results if not isinstance(r, str)]
    assert len(opened) == 1 and opened[0].razorpayOrderId and opened[0].attemptId != first.attemptId
    assert [r for r in results if isinstance(r, str)] == ['PAYMENT_SERVICE_BUSY']
    assert [str(row['id']) for row in liveInitial(url, user)] == [opened[0].attemptId]
    assert len(provider.orderCalls) == 2
    assert str(control(url, first.attemptId)['superseded_by']) == opened[0].attemptId


def test_owed_refund_does_not_block_new_purchase(drafts, monkeypatch):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['banking'], 'month')
    replacement = pay(repo, provider, old.userId, 2, ['banking'])
    # Started on the replaced checkout after it closed: finalization reserves its full refund.
    closedAt = control(drafts[3], old.attemptId)['closed_at']
    late = payment(provider, old, 'captured', created=math.ceil(closedAt.timestamp()))
    evidence = VerifiedPaymentEvidence(old.attemptId, old.invoiceId, old.userId, old.razorpayOrderId, late['id'],
        'initial_purchase', old.currency, 'captured', 'server_observation', old.amount, _now(), None, None, False,
        paymentCreatedAt=datetime.fromtimestamp(late['created_at'], timezone.utc))
    assert ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidence).state == 'refund_pending'
    owed = query(drafts[3], "SELECT * FROM payment_operations WHERE attempt_id=%s", (old.attemptId,))
    assert len(owed) == 1 and owed[0]['state'] == 'queued' and owed[0]['reason'] == 'paid_after_close'
    assert repo.readEditor(old.userId).capabilities.maySubmitLatestDraft
    repo.saveDraft(old.userId, 2, ['telecom'], 'month')
    third = pay(repo, provider, old.userId, 3, ['telecom'])
    assert third.attemptId not in (old.attemptId, replacement.attemptId) and third.razorpayOrderId
    assert str(control(drafts[3], replacement.attemptId)['superseded_by']) == third.attemptId


def test_legacy_create_subscription_follows_same_rules(drafts, monkeypatch):
    repo, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    provider = SyntheticPurchaseProvider()
    first = legacyPay(billing, provider, user, ['banking'], 'legacy-one')
    bound = control(url, first.attemptId)
    assert bound['capture_policy'] == 'automatic' and bound['draft_revision'] is None
    assert bound['provider_order_id'] == first.razorpayOrderId
    reused = legacyPay(billing, provider, user, ['banking'], 'legacy-other-device')
    assert reused.attemptId == first.attemptId and len(provider.orderCalls) == 1
    second = legacyPay(billing, provider, user, ['telecom'], 'legacy-two')
    assert second.attemptId != first.attemptId and len(provider.orderCalls) == 2
    assert str(control(url, first.attemptId)['superseded_by']) == second.attemptId
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (first.invoiceId,), one=True)['status'] == 'VOID'
    payment(provider, second, 'authorized')
    held = legacyPay(billing, provider, user, ['supplychain'], 'legacy-three')
    assert held.state == 'confirming_earlier_payment' and held.attemptId == second.attemptId
    assert len(provider.orderCalls) == 2
    replay = legacyPay(billing, provider, user, ['banking'], 'legacy-one')
    assert replay.attemptId == first.attemptId and replay.state == 'cancelled'
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        legacyPay(billing, provider, user, ['telecom'], 'legacy-other-device')


def test_pre_cutoff_live_attempt_can_be_superseded(drafts, monkeypatch):
    repo, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    provider = SyntheticPurchaseProvider()
    # A pre-rollout Order: no per-Order capture field was recorded and no control row exists.
    with monkeypatch.context() as patched:
        patched.setattr('api.services.billing.purchaseCheckoutRepository.bindPurchaseControlLocked', lambda *args: None)
        old = legacyPay(billing, provider, user, ['banking'], 'pre-cutoff')
    query(url, """UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling}',
        (metadata_json->'manualBilling')-'capturePolicy'-'captureWindowMinutes') WHERE id=%s""", (old.attemptId,))
    assert control(url, old.attemptId) is None
    new = legacyPay(billing, provider, user, ['telecom'], 'post-cutoff')
    assert new.attemptId != old.attemptId and new.razorpayOrderId
    legacy, replacement = control(url, old.attemptId), control(url, new.attemptId)
    assert legacy['capture_policy'] == 'account_default' and legacy['closed_reason'] == 'SUPERSEDED'
    assert str(legacy['superseded_by']) == new.attemptId and legacy['slot_id'] == replacement['slot_id']
    assert legacy['provider_order_id'] == old.razorpayOrderId
    assert query(url, 'SELECT kind,slot_key FROM payment_slots WHERE slot_id=%s', (legacy['slot_id'],), one=True) == {
        'kind': 'pro_term', 'slot_key': 'open'}


def test_cancel_route_removed(drafts, monkeypatch):
    import api.models as models
    from api.services.billing import purchaseCheckoutRepository, purchaseCheckoutService, paymentOperationRepository
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    response = http(repo, provider, old.userId, monkeypatch).post(
        '/purchase-checkouts/' + old.attemptId + '/cancel', json={'expectedOperationRevision': 1},
        headers={'Idempotency-Key': 'cancel'})
    assert response.status_code in (404, 405)
    assert not hasattr(models, 'CancelPurchaseCheckoutRequest')
    assert not hasattr(purchaseCheckoutService.PurchaseCheckoutService, 'cancel')
    for name in ('acceptCancellation', 'cancellationReplay', 'resolvePurchaseCancellation', 'scheduleCancellationRetry'):
        assert not hasattr(purchaseCheckoutRepository.PurchaseCheckoutRepository, name), name
    assert not hasattr(paymentOperationRepository.PaymentOperationRepositoryMixin, 'resolvePurchaseCancellation')
    columns = {r['column_name'] for r in query(drafts[3], '''SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='payment_slot_controls' ''')}
    assert not {'cancellation_requested_at', 'cancellation_accepted_at'} & columns
    assert query(drafts[3], "SELECT count(*) AS n FROM billing_events WHERE idempotency_key LIKE 'purchase-cancel:%%'",
                 one=True)['n'] == 0


def test_pay_requires_fresh_worker_heartbeat(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    query(drafts[3], "UPDATE private.payment_recovery_runtime SET worker_seen_at=clock_timestamp()-interval '10 minutes'")
    before = financialRows(drafts[3], old.userId)
    with pytest.raises(RuntimeError, match='PAYMENT_ACTIONS_UNAVAILABLE'):
        pay(repo, provider, old.userId, 2, ['telecom'])
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-stale-worker')
    assert response.status_code == 503 and response.json()['errorCode'] == 'PAYMENT_ACTIONS_UNAVAILABLE'
    assert financialRows(drafts[3], old.userId) == before and len(provider.orderCalls) == 1
    repo.recordPaymentWorkerHeartbeat()
    assert pay(repo, provider, old.userId, 2, ['telecom']).attemptId != old.attemptId


def test_order_window_is_the_reserved_snapshot_window(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: True)
    repo, _, user, _ = drafts
    repo.saveDraft(user, 0, ['banking'], 'month')
    intent = repo.reserveCheckout(repo.draftCheckoutRequest(user, 1, pricing(user, ['banking']), 'window'))
    assert intent.snapshot['captureWindowMinutes'] == 45
    monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', '60')
    provider = SyntheticPurchaseProvider()
    ManualPaymentService.forProduction(provider, repo).openReservedCheckout(intent)
    options = provider.orderCalls[0]['payment']['capture_options']
    assert options['automatic_expiry_period'] == options['manual_expiry_period'] == 45


def test_supersession_target_must_be_a_newer_open_attempt_in_the_same_slot(drafts, monkeypatch):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from api.services.billing.purchaseCheckoutRepository import supersedeAttemptLocked
    from api.services.billing.manualBillingRepository import _now
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    repo.saveDraft(old.userId, 1, ['banking'], 'month')
    new = pay(repo, provider, old.userId, 2, ['banking'])
    def supersede(attemptId, target):
        with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            supersedeAttemptLocked(cursor, attemptId, target, _now())
    for source, target in ((new.attemptId, old.attemptId), (new.attemptId, new.attemptId), (old.attemptId, new.attemptId)):
        with pytest.raises(ValueError, match='PAYMENT_SUPERSESSION_INVALID'):
            supersede(source, target)
    # SQL refuses a closed target even without the repository check: no cycle can form.
    with pytest.raises(psycopg2.Error, match='payment supersession owner or slot mismatch'):
        query(url, """UPDATE payment_slot_controls SET operation_revision=operation_revision+1,superseded_by=%s,
            closed_at=clock_timestamp(),closed_reason='SUPERSEDED' WHERE attempt_id=%s""", (old.attemptId, new.attemptId))


def test_control_replay_never_opens_an_orphan_slot(drafts, monkeypatch):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from types import SimpleNamespace
    from api.services.billing.purchaseCheckoutRepository import bindPurchaseControlLocked
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    slot = control(url, intent.attemptId)['slot_id']
    granted = payment(provider, intent, 'captured')  # the grant settles the attempt's slot
    assert repo.finalizeCapturedPayment(ManualPaymentService.buildPaymentEvidence(granted, attemptId=intent.attemptId,
        invoiceId=intent.invoiceId, userId=intent.userId, purpose='initial_purchase', observedAt=_now())).finalized
    assert query(url, 'SELECT settled_payment_id FROM payment_slots WHERE slot_id=%s', (slot,), one=True)['settled_payment_id']
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        bindPurchaseControlLocked(cursor, SimpleNamespace(userId=intent.userId, purpose='initial_purchase',
            purchaseDraftRevision=1), intent, 'open')
    assert query(url, 'SELECT count(*) AS n FROM payment_slots WHERE user_id=%s', (intent.userId,), one=True)['n'] == 1


@pytest.mark.parametrize('moment', ['before_observation_is_recorded', 'before_owner_lock'])
def test_authorization_recorded_after_inspection_is_never_replaced(drafts, monkeypatch, moment):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingRepository import _now
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    before = financialRows(drafts[3], old.userId)
    def authorizeElsewhere():
        # A browser verify or payment.authorized webhook lands between inspection and the owner lock.
        item = payment(provider, old, 'authorized')
        evidence = ManualPaymentService.buildPaymentEvidence(item, attemptId=old.attemptId, invoiceId=old.invoiceId,
            userId=old.userId, purpose='initial_purchase', observedAt=_now())
        repo.finalizeCapturedPayment(evidence)
    if moment == 'before_observation_is_recorded':
        inspect = ManualBillingRecoveryService.inspectAttempt
        def inspectThenAuthorize(self, attempt):
            snapshot = inspect(self, attempt)
            authorizeElsewhere()
            return snapshot
        monkeypatch.setattr(ManualBillingRecoveryService, 'inspectAttempt', inspectThenAuthorize)
    else:
        reserve = repo.reserveCheckout
        def authorizeThenReserve(request, **kwargs):
            if kwargs.get('inspection') is not None:
                authorizeElsewhere()
            return reserve(request, **kwargs)
        monkeypatch.setattr(repo, 'reserveCheckout', authorizeThenReserve)
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        pay(repo, provider, old.userId, 2, ['telecom'])
    invoices, attempts, controls = financialRows(drafts[3], old.userId)
    assert invoices == before[0] and len(attempts) == len(before[1]) and len(controls) == len(before[2])
    assert attempts[0]['payment_status'] == 'authorized' and not attempts[0]['closed']
    assert len(provider.orderCalls) == 1


@pytest.mark.parametrize('second,expected', [('authorized', 'confirming_earlier_payment'), ('unparseable', 'PAYMENT_SERVICE_BUSY')])
def test_refunded_order_with_another_live_payment_is_decided_again(drafts, monkeypatch, second, expected):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    payment(provider, old, 'refunded')
    held = payment(provider, old, 'authorized')
    if second == 'unparseable':
        held.pop('created_at')
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    try:
        outcome = pay(repo, provider, old.userId, 2, ['telecom']).state
    except RuntimeError as exc:
        outcome = str(exc)
    assert outcome == expected
    assert query(drafts[3], 'SELECT count(*) AS n FROM "Invoices" WHERE "userId"=%s', (old.userId,), one=True)['n'] == 1
    assert len(provider.orderCalls) == 1 and control(drafts[3], old.attemptId)['closed_at'] is None


def test_reusing_an_unopened_attempt_requires_fresh_worker_heartbeat(drafts, monkeypatch):
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: True)
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['banking'], 'month')
    reserved = repo.reserveCheckout(repo.draftCheckoutRequest(user, 1, pricing(user, ['banking']), 'reserved-only'))
    assert reserved.razorpayOrderId is None
    query(url, "UPDATE private.payment_recovery_runtime SET worker_seen_at=clock_timestamp()-interval '10 minutes'")
    provider = SyntheticPurchaseProvider()
    with pytest.raises(RuntimeError, match='PAYMENT_ACTIONS_UNAVAILABLE'):
        pay(repo, provider, user, 1, ['banking'])
    assert not provider.orderCalls
    repo.recordPaymentWorkerHeartbeat()
    opened = pay(repo, provider, user, 1, ['banking'])
    assert opened.attemptId == reserved.attemptId and opened.razorpayOrderId


def test_resaving_the_same_selection_reuses_the_order(drafts, monkeypatch):
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    assert repo.saveDraft(first.userId, 1, ['manufacturing', 'banking'], 'month').revision == 2
    again = pay(repo, provider, first.userId, 2, ['banking', 'manufacturing'])
    assert again.attemptId == first.attemptId and again.razorpayOrderId == first.razorpayOrderId
    assert len(provider.orderCalls) == 1
    with pytest.raises(ValueError, match='PURCHASE_DRAFT_STALE'):
        pay(repo, provider, first.userId, 1, ['banking', 'manufacturing'])


def test_invalid_supersession_is_busy_and_rolls_back(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    before = financialRows(drafts[3], old.userId)
    def refuse(*args):
        raise ValueError('PAYMENT_SUPERSESSION_INVALID')
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.supersedeAttemptLocked', refuse)
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-invalid-supersession')
    assert response.status_code == 503
    assert response.json() == {'message': BUSY_COPY, 'errorCode': 'PAYMENT_SERVICE_BUSY'}
    assert financialRows(drafts[3], old.userId) == before and len(provider.orderCalls) == 1


def test_hold_uses_the_capture_window_recorded_on_the_attempt(drafts, monkeypatch):
    monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', '60')
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    assert old.snapshot['captureWindowMinutes'] == 60
    monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', '45')
    payment(provider, old, 'authorized', created=time.time() - 50 * 60)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    held = pay(repo, provider, old.userId, 2, ['telecom'])
    assert held.state == 'confirming_earlier_payment' and held.attemptId == old.attemptId
    assert len(provider.orderCalls) == 1


def recordAuthorization(repo, provider, intent):
    """Browser verify or payment.authorized marks the attempt authorized; returns the provider payment."""
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingRepository import _now
    item = payment(provider, intent, 'authorized')
    repo.finalizeCapturedPayment(ManualPaymentService.buildPaymentEvidence(item, attemptId=intent.attemptId,
        invoiceId=intent.invoiceId, userId=intent.userId, purpose='initial_purchase', observedAt=_now()))
    assert repo.attemptById(intent.userId, intent.attemptId)['payment_status'] == 'authorized'
    return item


def test_auto_refunded_authorization_frees_pay(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    held = recordAuthorization(repo, provider, old)
    # Never captured: Razorpay returned it automatically. Nothing moves the attempt off 'authorized'.
    provider.payments[held['id']]['status'] = 'refunded'
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    new = pay(repo, provider, old.userId, 2, ['telecom'])
    assert new.attemptId != old.attemptId and new.razorpayOrderId and len(provider.orderCalls) == 2
    assert str(control(drafts[3], old.attemptId)['superseded_by']) == new.attemptId
    assert [str(row['id']) for row in liveInitial(drafts[3], old.userId)] == [new.attemptId]


def test_refunded_and_fresh_authorization_on_an_authorized_attempt_confirms(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    first = recordAuthorization(repo, provider, old)
    provider.payments[first['id']]['status'] = 'refunded'
    payment(provider, old, 'authorized')
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    held = pay(repo, provider, old.userId, 2, ['telecom'])
    assert held.state == 'confirming_earlier_payment' and held.attemptId == old.attemptId
    assert len(provider.orderCalls) == 1 and control(drafts[3], old.attemptId)['closed_at'] is None


def test_status_and_pay_agree_on_a_superseded_attempt_holding_an_authorization(drafts, monkeypatch):
    """A closed (superseded) attempt still holding an in-window authorized payment confirms in status as in Pay."""
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.paymentStatusPresentation import presentPaymentStatus
    from api.services.billing.purchaseCheckoutStatusService import PurchaseCheckoutStatusService
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    new = pay(repo, provider, old.userId, 2, ['telecom'])
    assert new.attemptId != old.attemptId and frozen(drafts[3], old.attemptId)[1]['closedReason'] == 'SUPERSEDED'
    started = time.time()
    payment(provider, old, 'authorized', created=started)  # a late authorization on the replaced Order
    ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(old.userId, old.attemptId))
    assert frozen(drafts[3], old.attemptId)[0] == 'cancelled'
    def present(now):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                return presentPaymentStatus(cursor, old.userId, now)
        return repo._run(operation)
    shown = present(_now())['checkout']
    assert shown == {'state': 'confirming_earlier_payment', 'attemptId': old.attemptId, 'amount': old.amount,
                     'currency': old.currency, 'selectionLabel': 'Pro monthly with Banking and Manufacturing'}
    assert PurchaseCheckoutStatusService(repo, provider).refresh(old.userId)['checkout'] == shown
    repo.saveDraft(old.userId, 2, ['supplychain'], 'month')
    held = pay(repo, provider, old.userId, 3, ['supplychain'])
    assert held.state == 'confirming_earlier_payment' and held.attemptId == shown['attemptId']
    # Past the attempt's own capture window the authorization is dead: status shows the live attempt as ready.
    window = timedelta(minutes=old.snapshot['captureWindowMinutes'])
    after = present(datetime.fromtimestamp(int(started), timezone.utc) + window + timedelta(seconds=1))['checkout']
    assert after['state'] == 'ready' and after['attemptId'] == new.attemptId


# -- every purpose shares the slot rules (renewal, expert addition, top-up) --

def paidAccount(drafts, monkeypatch, interval='month', domains=('banking',)):
    """A v2 account whose initial purchase was captured and granted."""
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch, domains, interval)
    item = payment(provider, intent, 'captured')
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, intent.userId, intent.razorpayOrderId,
        item['id'], 'initial_purchase', intent.currency, 'captured', 'server_observation', intent.amount, _now(),
        None, None, False)
    assert repo.finalizeCapturedPayment(evidence).finalized
    return repo, provider, intent


def monthlyRenewalInvoice(repo, user):
    from dateutil.relativedelta import relativedelta
    context = repo.getRenewalContext(user)
    start = context['current_period_end']
    return repo.createFrozenRenewalInvoice(dict(userId=user, subscription_id=context['id'], billing_reason='renewal',
        payment_flow='razorpay_order_checkout', requires_customer_auth=True, period_start=start,
        period_end=start + relativedelta(months=1), status='UPCOMING',
        metadata_json={'manualBilling': dict(lifecycleId=context['billing_state']['manualBilling']['lifecycleId'],
            purpose='renewal', billingMode='monthly_prepaid', domains=context['_verifiedCoverage']['nextExperts'],
            coverageState='scheduled', revision=1)}), context['version'], expectedCatalogRevision=1)


def annualRenewalInvoice(repo, user):
    invoice = repo.createAnnualRenewalInvoice(user)
    return repo.prepareAnnualRenewalInvoice(str(invoice['id']))


def purposePay(repo, provider, user, purpose, mode, payload, key=None):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    from api.services.billing.manualPaymentService import ManualPaymentService
    return ManualPaymentService.forProduction(provider, repo).createCheckout(CheckoutRequest(
        user, purpose, mode, payload, key or purpose + '-' + str(uuid4()), expectedCatalogRevision=1))


def liveIn(url, user, purposes):
    return [str(row['id']) for row in query(url, '''SELECT id FROM billing_events WHERE user_id=%s
        AND event_category='payment_attempt' AND payment_status IN ('created','pending_provider_ack','authorized')
        AND metadata_json->'manualBilling'->>'purpose' = ANY(%s)
        AND metadata_json->'manualBilling'->>'closedAt' IS NULL ORDER BY attempted_at''', (user, list(purposes)))]


def slotOf(url, attemptId):
    return query(url, '''SELECT s.kind,s.slot_key FROM payment_slot_controls c JOIN payment_slots s ON s.slot_id=c.slot_id
        WHERE c.attempt_id=%s''', (attemptId,), one=True)


def expireAttempt(url, attemptId):
    query(url, """UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,expiresAt}',
        to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE id=%s""", (attemptId,))


def test_renewal_retry_after_dismiss_reuses_then_replaces_after_ttl(drafts, monkeypatch):
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    invoice = monthlyRenewalInvoice(repo, user)
    first = purposePay(repo, provider, user, 'renewal', 'monthly_prepaid', {'invoiceId': str(invoice['id'])})
    assert first.razorpayOrderId and first.invoiceId == str(invoice['id'])
    issued = query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (first.invoiceId,), one=True)['status']
    # Dismissed checkout, Pay again from another device: the same Order, no new invoice.
    again = purposePay(repo, provider, user, 'renewal', 'monthly_prepaid', {'invoiceId': str(invoice['id'])})
    assert again.attemptId == first.attemptId and again.razorpayOrderId == first.razorpayOrderId
    orders = len(provider.orderCalls)
    term = query(url, 'SELECT period_start FROM "Invoices" WHERE id=%s', (str(invoice['id']),), one=True)['period_start']
    assert slotOf(url, first.attemptId) == {'kind': 'pro_term', 'slot_key': term.isoformat()}
    expireAttempt(url, first.attemptId)
    replaced = purposePay(repo, provider, user, 'renewal', 'monthly_prepaid', {'invoiceId': str(invoice['id'])})
    assert replaced.attemptId != first.attemptId and replaced.razorpayOrderId
    assert replaced.invoiceId == first.invoiceId and len(provider.orderCalls) == orders + 1
    # The renewal invoice persists; only the attempt is replaced.
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (first.invoiceId,), one=True)['status'] == issued != 'VOID'
    status, billing = frozen(url, first.attemptId)
    assert status == 'cancelled' and billing['closedReason'] == 'SUPERSEDED' and not billing.get('closureReconciledAt')
    closed = control(url, first.attemptId)
    assert str(closed['superseded_by']) == replaced.attemptId and closed['slot_id'] == control(url, replaced.attemptId)['slot_id']
    assert liveIn(url, user, ['renewal', 'initial_purchase']) == [replaced.attemptId]


def test_renewal_revision_change_replaces_empty_attempt(drafts, monkeypatch):
    repo, provider, initial = paidAccount(drafts, monkeypatch, 'year')
    url, user = drafts[3], initial.userId
    invoice = annualRenewalInvoice(repo, user)
    old = purposePay(repo, provider, user, 'renewal', 'annual_prepaid', {'invoiceId': str(invoice['id'])})
    assert old.revision == 1 and old.razorpayOrderId
    # An expert grant marked the unpaid renewal for repricing; the scheduler re-freezes it in place.
    query(url, '''UPDATE "Invoices" SET status='EXPIRED',metadata_json=metadata_json||'{"repricingRequired":true}'::jsonb
        WHERE id=%s''', (str(invoice['id']),))
    repriced = repo.prepareAnnualRenewalInvoice(str(invoice['id']))
    assert repriced['status'] == 'PAYMENT_PENDING' and repriced['metadata_json']['manualBilling']['revision'] == 2
    new = purposePay(repo, provider, user, 'renewal', 'annual_prepaid', {'invoiceId': str(invoice['id'])})
    assert new.attemptId != old.attemptId and new.revision == 2 and new.invoiceId == old.invoiceId
    assert new.razorpayOrderId and new.razorpayOrderId != old.razorpayOrderId
    assert frozen(url, old.attemptId)[1]['closedReason'] == 'SUPERSEDED'
    assert str(control(url, old.attemptId)['superseded_by']) == new.attemptId
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (old.invoiceId,), one=True)['status'] == 'PAYMENT_PENDING'
    # The same revision again reuses the replacement.
    assert purposePay(repo, provider, user, 'renewal', 'annual_prepaid', {'invoiceId': str(invoice['id'])}).attemptId == new.attemptId


def test_repurchase_and_ended_annual_renewal_share_pro_term_slot(drafts, monkeypatch):
    from datetime import datetime, timezone
    from dateutil.relativedelta import relativedelta
    from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'legacy')
    provider = SyntheticPurchaseProvider()
    service = ManualPaymentService.forProduction(provider, billing)
    paid = service.createCheckout(CheckoutRequest(user, 'initial_purchase', 'annual_prepaid', {'domains': ['banking']},
                                                  'annual', expectedCatalogRevision=1))
    item = payment(provider, paid, 'captured')
    assert billing.finalizeCapturedPayment(VerifiedPaymentEvidence(paid.attemptId, paid.invoiceId, user,
        paid.razorpayOrderId, item['id'], 'initial_purchase', 'INR', 'captured', 'server_observation', paid.amount,
        _now(), None, None, False)).finalized
    # The annual term ended an hour ago; its renewal invoice is still payable.
    end = datetime.now(timezone.utc).replace(microsecond=0) - relativedelta(hours=1)
    query(url, 'UPDATE "Invoices" SET period_start=%s,period_end=%s WHERE id=%s', (end - relativedelta(years=1), end, paid.invoiceId))
    query(url, 'UPDATE subscriptions SET current_period_start=%s,current_period_end=%s,renewal_due_at=%s WHERE user_id=%s',
          (end - relativedelta(years=1), end, end, user))
    renewalInvoice = annualRenewalInvoice(billing, user)
    assert renewalInvoice['period_start'] == end
    renewal = service.createCheckout(CheckoutRequest(user, 'renewal', 'annual_prepaid',
        {'invoiceId': str(renewalInvoice['id'])}, 'renew-ended', expectedCatalogRevision=1))
    assert renewal.razorpayOrderId and slotOf(url, renewal.attemptId) == {'kind': 'pro_term', 'slot_key': 'open'}
    repurchase = service.createCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
        {'domains': ['telecom']}, 'repurchase', expectedCatalogRevision=1))
    assert repurchase.razorpayOrderId and repurchase.attemptId != renewal.attemptId
    assert liveIn(url, user, ['renewal', 'initial_purchase']) == [repurchase.attemptId]
    assert str(control(url, renewal.attemptId)['superseded_by']) == repurchase.attemptId
    # Only the renewal attempt closed; its invoice still names the ended term.
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (str(renewalInvoice['id']),), one=True)['status'] == 'PAYMENT_PENDING'
    again = service.createCheckout(CheckoutRequest(user, 'renewal', 'annual_prepaid',
        {'invoiceId': str(renewalInvoice['id'])}, 'renew-again', expectedCatalogRevision=1))
    assert again.attemptId not in (renewal.attemptId, repurchase.attemptId) and again.razorpayOrderId
    assert liveIn(url, user, ['renewal', 'initial_purchase']) == [again.attemptId]
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (repurchase.invoiceId,), one=True)['status'] == 'VOID'
    slots = {control(url, a)['slot_id'] for a in (renewal.attemptId, repurchase.attemptId, again.attemptId)}
    assert len(slots) == 1


def test_expert_addition_single_attempt_replaces_previous_set(drafts, monkeypatch):
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    first = purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['telecom']})
    assert first.razorpayOrderId
    end = repo.getCoverageSnapshot(user).currentPeriod.end
    assert slotOf(url, first.attemptId) == {'kind': 'expert_addition', 'slot_key': 'current:' + end.isoformat()}
    both = purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['telecom', 'supplychain']})
    assert both.attemptId != first.attemptId and both.razorpayOrderId and both.snapshot['domains'] == ['supplychain', 'telecom']
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (first.invoiceId,), one=True)['status'] == 'VOID'
    assert str(control(url, first.attemptId)['superseded_by']) == both.attemptId
    assert liveIn(url, user, ['expert_addition']) == [both.attemptId]
    same = purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['supplychain', 'telecom']})
    assert same.attemptId == both.attemptId and same.razorpayOrderId == both.razorpayOrderId
    # An expert already subscribed is an invalid request, never a replacement: the live set stays.
    with pytest.raises(ValueError, match='INVALID_EXPERT_SELECTION'):
        purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['banking']})
    assert liveIn(url, user, ['expert_addition']) == [both.attemptId]
    payment(provider, both, 'authorized')
    held = purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['manufacturing']})
    assert held.state == 'confirming_earlier_payment' and held.attemptId == both.attemptId
    assert held.earlierPayment == {'selectionLabel': 'adding Supply Chain and Telecom', 'amount': both.amount,
                                   'currency': 'INR'}


def test_topup_second_tab_replaces_unpaid_first(drafts, monkeypatch):
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    first = purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'medium'}, 'tab-one')
    second = purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'medium'}, 'tab-two')
    assert second.attemptId == first.attemptId and second.razorpayOrderId == first.razorpayOrderId
    assert slotOf(url, first.attemptId) == {'kind': 'topup', 'slot_key': 'topup'}
    other = purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'small'}, 'tab-three')
    assert other.attemptId != first.attemptId and other.snapshot['packId'] == 'small'
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (first.invoiceId,), one=True)['status'] == 'VOID'
    assert str(control(url, first.attemptId)['superseded_by']) == other.attemptId
    assert liveIn(url, user, ['topup']) == [other.attemptId]
    # A retry of the first tab replays its own (now closed) attempt; nothing new opens.
    assert purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'medium'}, 'tab-one').state == 'cancelled'
    assert len(provider.orderCalls) == 3


def test_unrelated_slot_money_does_not_block_renewal(drafts, monkeypatch):
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    topup = purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'medium'})
    late = payment(provider, topup, 'captured')
    subscription = query(url, 'SELECT id FROM subscriptions WHERE user_id=%s', (user,), one=True)['id']
    # A top-up refund is owed and a top-up capture awaits reconciliation: neither is in the Pro term slot.
    query(url, '''INSERT INTO billing_events(id,user_id,subscription_id,invoice_id,event_category,event_type,event_status,
        provider,provider_order_id,provider_payment_id,amount,currency,idempotency_key,failure_reason,occurred_at)
        VALUES(%s,%s,%s,%s,'reconciliation','payment.capture','REQUIRES_RECONCILIATION','razorpay',%s,%s,%s,'INR',%s,
        'CLOSED_ATTEMPT',clock_timestamp())''', (str(uuid4()), user, subscription, topup.invoiceId, topup.razorpayOrderId,
        late['id'], topup.amount, 'capture:' + late['id']))
    query(url, '''INSERT INTO billing_events(id,user_id,subscription_id,invoice_id,event_category,event_type,event_status,
        idempotency_key,metadata_json,occurred_at) VALUES(%s,%s,%s,%s,'reconciliation','refund.intent','reserved',%s,'{}',clock_timestamp())''',
        (str(uuid4()), user, subscription, topup.invoiceId, 'refund-intent:' + str(uuid4())))
    invoice = monthlyRenewalInvoice(repo, user)
    renewal = purposePay(repo, provider, user, 'renewal', 'monthly_prepaid', {'invoiceId': str(invoice['id'])})
    assert renewal.razorpayOrderId and renewal.state == 'pending_provider_ack'
    assert repo.runtimeSnapshotFactsLoader(user)[0].moneyUnresolved is False


def test_scheduler_skips_repricing_with_authorized_payment(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, initial = paidAccount(drafts, monkeypatch, 'year')
    url, user = drafts[3], initial.userId
    invoice = annualRenewalInvoice(repo, user)
    attempt = purposePay(repo, provider, user, 'renewal', 'annual_prepaid', {'invoiceId': str(invoice['id'])})
    held = payment(provider, attempt, 'authorized')
    ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(user, attempt.attemptId))
    query(url, '''UPDATE "Invoices" SET status='EXPIRED',metadata_json=metadata_json||'{"repricingRequired":true}'::jsonb
        WHERE id=%s''', (str(invoice['id']),))
    before = query(url, 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE id=%s', (str(invoice['id']),), one=True)
    with pytest.raises(RuntimeError, match='RENEWAL_REPRICING_DEFERRED'):
        repo.prepareAnnualRenewalInvoice(str(invoice['id']))
    assert query(url, 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE id=%s', (str(invoice['id']),), one=True) == before
    # Once the authorization is past its capture window (Razorpay returns it), the scheduler reprices.
    provider.payments[held['id']]['created_at'] = int(time.time()) - 46 * 60
    query(url, """UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,providerObservation,latestPaymentCreatedAt}',
        to_jsonb((clock_timestamp()-interval '46 minutes')::text)) WHERE id=%s""", (attempt.attemptId,))
    repriced = repo.prepareAnnualRenewalInvoice(str(invoice['id']))
    assert repriced['metadata_json']['manualBilling']['revision'] == 2


def test_superseded_attempt_scanned_until_recover_until(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    started = time.time() - 40 * 60
    payment(provider, old, 'failed', created=started)
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    new = pay(repo, provider, old.userId, 2, ['telecom'])
    assert new.attemptId != old.attemptId
    closed = control(url, old.attemptId)
    # Superseded at closed_at, while its Checkout could still start a payment until expiresAt.
    deadline = datetime.fromisoformat(frozen(url, old.attemptId)[1]['expiresAt'])
    assert closed['recover_until'] == deadline + timedelta(minutes=45) > closed['closed_at'] + timedelta(minutes=45)
    # Later on, recover_until has been reached for everything recorded so far.
    query(url, """UPDATE payment_slot_controls SET operation_revision=operation_revision+1,
        recover_until=clock_timestamp()-interval '10 minutes' WHERE attempt_id=%s""", (old.attemptId,))
    query(url, """UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,
        '{manualBilling,providerObservation,latestPaymentCreatedAt}',to_jsonb((clock_timestamp()-interval '55 minutes')::text))
        WHERE id=%s""", (old.attemptId,))

    def scan():
        query(url, "UPDATE billing_events SET updated_at='1990-01-01' WHERE id=%s", (old.attemptId,))
        due = [str(row['id']) for row in repo.pendingRecoveryRows(limit=500)]
        assert old.attemptId in due
        ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(old.userId, old.attemptId))
        return frozen(url, old.attemptId)[1]

    assert not scan().get('closureReconciledAt')
    # A stale tab started another payment on the superseded Order: the scan window follows it.
    later = time.time() - 60
    payment(provider, old, 'failed', created=later)
    assert not scan().get('closureReconciledAt')
    extended = control(url, old.attemptId)['recover_until']
    assert abs(extended.timestamp() - (later + 45 * 60)) < 2
    # Two hours on: past recover_until a complete listing without money ends the scan.
    query(url, """UPDATE payment_slot_controls SET operation_revision=operation_revision+1,
        recover_until=clock_timestamp()-interval '75 minutes' WHERE attempt_id=%s""", (old.attemptId,))
    query(url, """UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,
        '{manualBilling,providerObservation,latestPaymentCreatedAt}',to_jsonb((clock_timestamp()-interval '2 hours')::text))
        WHERE id=%s""", (old.attemptId,))
    for item in provider.payments.values():
        item['created_at'] = int(time.time()) - 2 * 3600
    assert scan().get('closureReconciledAt')
    query(url, "UPDATE billing_events SET updated_at='1990-01-01' WHERE id=%s", (old.attemptId,))
    assert old.attemptId not in [str(row['id']) for row in repo.pendingRecoveryRows(limit=500)]


def test_renewal_expert_and_topup_routes_map_busy_and_confirming(drafts, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    import api.routers.credits as creditRoutes
    import api.routers.subscriptions as routes
    from api.services.credits.topupService import TopupService, topupService
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    invoice = str(monthlyRenewalInvoice(repo, user)['id'])
    held = {'renewal': purposePay(repo, provider, user, 'renewal', 'monthly_prepaid', {'invoiceId': invoice}),
            'expert_addition': purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['telecom']}),
            'topup': purposePay(repo, provider, user, 'topup', 'monthly_prepaid', {'packId': 'medium'})}
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
    monkeypatch.setattr(routes.subscriptionService, 'razorpayClient', provider)
    monkeypatch.setattr(routes.subscriptionService, '_getCanonicalSubscription',
                        lambda userId, required=False: {'billing_mode': 'monthly_prepaid', 'default_currency': 'INR'})
    monkeypatch.setattr(SubscriptionService, '_resolveCheckoutIdentity',
                        lambda self, userId, email: {'email': email, 'name': 'Synthetic', 'contact': ''})
    monkeypatch.setattr(topupService, 'razorpayClient', provider)
    monkeypatch.setattr(topupService, '_subscription', lambda userId: {'billing_mode': 'monthly_prepaid', 'default_currency': 'INR'})
    monkeypatch.setattr(TopupService, '_isTopupEligible', staticmethod(lambda subscription: True))
    token = jwt.encode({'userId': user, 'email': 'synthetic@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(creditRoutes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    client = TestClient(app)
    calls = {'renewal': lambda key: client.post('/createRenewalPaymentSession', json={'invoiceId': invoice},
                                                headers={'Idempotency-Key': key}),
             'expert_addition': lambda key: client.post('/addDomains', json={'domains': ['supplychain'], 'expectedCatalogRevision': 1},
                                                        headers={'Idempotency-Key': key}),
             'topup': lambda key: client.post('/topup/order', json={'packId': 'small', 'expectedCatalogRevision': 1},
                                              headers={'Idempotency-Key': key})}
    orders = len(provider.orderCalls)
    outage = provider.order.payments
    def unreachable(*args, **kwargs):
        raise TimeoutError('synthetic provider outage')
    provider.order.payments = unreachable
    for purpose, call in calls.items():
        response = call('busy-' + purpose)
        assert response.status_code == 503, (purpose, response.text)
        detail = response.json()['detail']
        assert detail['message'] == BUSY_COPY and detail['errorCode'] == 'PAYMENT_SERVICE_BUSY', purpose
    provider.order.payments = outage
    for purpose, intent in held.items():
        payment(provider, intent, 'authorized')
        response = calls[purpose]('confirm-' + purpose)
        assert response.status_code == 200, (purpose, response.text)
        data = response.json()['data']
        assert set(data) == {'state', 'attemptId', 'earlierPayment'}, purpose
        assert data['state'] == 'confirming_earlier_payment' and data['attemptId'] == intent.attemptId
        assert data['earlierPayment']['amount'] == intent.amount
    assert len(provider.orderCalls) == orders


def test_expert_cancel_route_and_unused_slot_retry_columns_removed(drafts, monkeypatch):
    import api.models as models
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    response = http(repo, provider, initial.userId, monkeypatch).post('/cancelPendingAddition', json={'domain': 'telecom'})
    assert response.status_code in (404, 405)
    assert not hasattr(models, 'CancelPendingAdditionRequest')
    assert not hasattr(SubscriptionService, 'cancelPendingAddition')
    assert not hasattr(ManualBillingRepository, 'cancelExpertAddition')
    columns = {r['column_name'] for r in query(drafts[3], '''SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='payment_slot_controls' ''')}
    assert 'recover_until' in columns and not {'retry_count', 'next_retry_at', 'escalation_reference'} & columns


# -- fix round 1: one Pro term slot across the renewal boundary, same-slot money before reuse --

def legacyTermEndingSoon(drafts, monkeypatch, mode, seconds=6):
    """A legacy account whose paid term ends a few real seconds from now, with its renewal invoice issued."""
    from datetime import timezone
    from dateutil.relativedelta import relativedelta
    from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.manualPaymentService import ManualPaymentService
    _, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'legacy')
    provider = SyntheticPurchaseProvider()
    service = ManualPaymentService.forProduction(provider, billing)
    paid = service.createCheckout(CheckoutRequest(user, 'initial_purchase', mode, {'domains': ['banking']},
                                                  'paid-term', expectedCatalogRevision=1))
    item = payment(provider, paid, 'captured')
    assert billing.finalizeCapturedPayment(VerifiedPaymentEvidence(paid.attemptId, paid.invoiceId, user,
        paid.razorpayOrderId, item['id'], 'initial_purchase', 'INR', 'captured', 'server_observation', paid.amount,
        _now(), None, None, False)).finalized
    end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=seconds)
    start = end - (relativedelta(years=1) if mode == 'annual_prepaid' else relativedelta(months=1))
    query(url, 'UPDATE "Invoices" SET period_start=%s,period_end=%s WHERE id=%s', (start, end, paid.invoiceId))
    query(url, 'UPDATE subscriptions SET current_period_start=%s,current_period_end=%s,renewal_due_at=%s WHERE user_id=%s',
          (start, end, end, user))
    if mode == 'annual_prepaid':
        invoice = annualRenewalInvoice(billing, user)
    else:
        row = query(url, 'SELECT id,version,billing_state FROM subscriptions WHERE user_id=%s AND is_canonical', (user,), one=True)
        invoice = billing.createFrozenRenewalInvoice(dict(userId=user, subscription_id=row['id'], billing_reason='renewal',
            payment_flow='razorpay_order_checkout', requires_customer_auth=True, period_start=end,
            period_end=end + relativedelta(months=1), status='UPCOMING', metadata_json={'manualBilling': dict(
                lifecycleId=row['billing_state']['manualBilling']['lifecycleId'], purpose='renewal',
                billingMode='monthly_prepaid', domains=['banking'], coverageState='scheduled', revision=1)}),
            row['version'], expectedCatalogRevision=1)
    assert invoice['period_start'] == end
    renewal = service.createCheckout(CheckoutRequest(user, 'renewal', mode, {'invoiceId': str(invoice['id'])},
                                                     'renew-before-end', expectedCatalogRevision=1))
    assert renewal.razorpayOrderId and slotOf(url, renewal.attemptId) == {'kind': 'pro_term', 'slot_key': end.isoformat()}
    return service, provider, renewal, invoice, end


def afterTermEnd(end):
    from datetime import timezone
    remaining = (end - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 0, 'the renewal must be reserved before its term ends'
    time.sleep(remaining + 0.5)


def repurchase(service, user, key='repurchase-after-end'):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    return service.createCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['telecom']},
                                                  key, expectedCatalogRevision=1))


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_renewal_reserved_before_term_end_is_replaced_by_a_repurchase_after_it(drafts, monkeypatch, mode):
    url = drafts[3]
    service, provider, renewal, invoice, end = legacyTermEndingSoon(drafts, monkeypatch, mode)
    afterTermEnd(end)
    new = repurchase(service, renewal.userId)
    assert new.attemptId != renewal.attemptId and new.razorpayOrderId
    status, billing = frozen(url, renewal.attemptId)
    assert status == 'cancelled' and billing['closedReason'] == 'SUPERSEDED' and not billing.get('closureReconciledAt')
    old, replacement = control(url, renewal.attemptId), control(url, new.attemptId)
    # The renewal's control joined the open term: one slot instance, so only one capture can grant.
    assert str(old['superseded_by']) == new.attemptId and old['slot_id'] == replacement['slot_id']
    assert slotOf(url, renewal.attemptId) == {'kind': 'pro_term', 'slot_key': 'open'}
    assert liveIn(url, renewal.userId, ['renewal', 'initial_purchase']) == [new.attemptId]
    # Replacement never voids the renewal invoice (a legacy monthly term expires it at its boundary),
    # so the new invoice replaces nothing.
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (str(invoice['id']),), one=True)['status'] == (
        'EXPIRED' if mode == 'monthly_prepaid' else 'PAYMENT_PENDING')
    metadata = query(url, 'SELECT metadata_json FROM "Invoices" WHERE id=%s', (new.invoiceId,), one=True)['metadata_json']
    assert 'replacesInvoiceIds' not in metadata['manualBilling']


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_repurchase_after_term_end_confirms_a_renewal_payment_in_flight(drafts, monkeypatch, mode):
    url = drafts[3]
    service, provider, renewal, invoice, end = legacyTermEndingSoon(drafts, monkeypatch, mode)
    payment(provider, renewal, 'authorized')  # started before the term ended
    before = financialRows(url, renewal.userId)
    afterTermEnd(end)
    held = repurchase(service, renewal.userId)
    interval = 'annual' if mode == 'annual_prepaid' else 'monthly'
    assert held.state == 'confirming_earlier_payment' and held.attemptId == renewal.attemptId
    assert held.earlierPayment == {'selectionLabel': f'Pro {interval} renewal with Banking',
                                   'amount': renewal.amount, 'currency': renewal.currency}
    assert len(provider.orderCalls) == 2
    invoices, attempts, controls = financialRows(url, renewal.userId)
    assert len(invoices) == len(before[0]) and len(attempts) == len(before[1]) and len(controls) == len(before[2])


def test_same_slot_money_in_flight_is_checked_before_reuse(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    repo.saveDraft(old.userId, 1, ['telecom'], 'month')
    live = pay(repo, provider, old.userId, 2, ['telecom'], key='replacement')
    assert live.attemptId != old.attemptId and live.razorpayOrderId
    # The stale tab's payment on the superseded Order authorized late (spec 5 case 4a).
    payment(provider, old, 'authorized')
    ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(old.userId, old.attemptId))
    earlier = {'selectionLabel': 'Pro monthly with Banking and Manufacturing', 'amount': old.amount, 'currency': 'INR'}
    # The same selection as the live attempt is not reused, over HTTP too.
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-same-as-live')
    assert response.status_code == 200, response.text
    assert response.json()['checkout'] == {'state': 'confirming_earlier_payment', 'attemptId': old.attemptId,
                                           'earlierPayment': earlier}
    # Neither is it replaced by another selection.
    repo.saveDraft(old.userId, 2, ['supplychain'], 'month')
    held = pay(repo, provider, old.userId, 3, ['supplychain'])
    assert held.state == 'confirming_earlier_payment' and held.attemptId == old.attemptId and held.earlierPayment == earlier
    assert liveIn(url, old.userId, ['initial_purchase']) == [live.attemptId] and control(url, live.attemptId)['closed_at'] is None
    assert len(provider.orderCalls) == 2


@pytest.mark.parametrize('order', ['capture_first', 'replace_first', 'concurrent'])
def test_expert_addition_replace_vs_capture_never_grants_twice_nor_loses_the_payment(drafts, monkeypatch, order):
    from threading import Event
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.billing.manualBillingRepository import _now
    repo, provider, initial = paidAccount(drafts, monkeypatch)
    url, user = drafts[3], initial.userId
    first = purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['telecom']})
    inspected, captured, replaced = Event(), Event(), Event()
    inspect = ManualBillingRecoveryService.inspectAttempt
    def inspectThenWait(self, attempt):
        snapshot = inspect(self, attempt)  # the empty listing Pay decides from
        inspected.set()
        if order == 'capture_first':
            assert captured.wait(timeout=10)
        return snapshot
    monkeypatch.setattr(ManualBillingRecoveryService, 'inspectAttempt', inspectThenWait)
    def replace():
        try:
            return purposePay(repo, provider, user, 'expert_addition', 'monthly_prepaid', {'domains': ['supplychain']})
        except RuntimeError as exc:
            return str(exc)
        finally:
            replaced.set()
    def capture():
        assert inspected.wait(timeout=10)
        if order == 'replace_first':
            assert replaced.wait(timeout=10)
        # Started now (whole provider seconds, rounded up): after the replacement when Pay went first.
        item = payment(provider, first, 'captured', created=math.ceil(time.time()))
        try:
            return repo.finalizeCapturedPayment(VerifiedPaymentEvidence(first.attemptId, first.invoiceId, user,
                first.razorpayOrderId, item['id'], 'expert_addition', first.currency, 'captured', 'server_observation',
                first.amount, _now(), None, None, False,
                paymentCreatedAt=datetime.fromtimestamp(item['created_at'], timezone.utc))), item['id']
        finally:
            captured.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(replace)
        (result, paymentId), outcome = pool.submit(capture).result(), pending.result()
    experts = set(repo.getCoverageSnapshot(user).currentPeriod.domains)
    # Never two grants: the replacement set is never delivered alongside, or instead of, the paid one.
    assert 'supplychain' not in experts and ('telecom' in experts) == bool(result.finalized)
    if result.finalized:
        # The capture won the slot; Pay saw it under the lock and opened nothing.
        assert outcome == 'PAYMENT_SERVICE_BUSY' or (outcome.attemptId == first.attemptId and outcome.state == 'captured')
        assert liveIn(url, user, ['expert_addition']) == []
    else:
        # Pay replaced first; the payment started on the closed checkout is returned in full, never granted.
        assert outcome.attemptId != first.attemptId and frozen(url, first.attemptId)[1]['closedReason'] == 'SUPERSEDED'
        assert liveIn(url, user, ['expert_addition']) == [outcome.attemptId]
        assert result.state == 'refund_pending'
        assert [row['reason'] for row in query(url, 'SELECT reason FROM payment_operations WHERE provider_payment_id=%s',
                                               (paymentId,))] == ['paid_after_close']
    assert result.finalized == (order == 'capture_first') or order == 'concurrent'
    # Never a lost payment record: exactly one capture event for the provider payment.
    assert query(url, '''SELECT count(*) AS n FROM billing_events WHERE provider_payment_id=%s
        AND event_type='payment.capture' ''', (paymentId,), one=True)['n'] == 1
