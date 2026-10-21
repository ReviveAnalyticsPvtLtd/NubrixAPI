"""Pay decides open, reuse, replace or wait for the initial purchase slot; real SQL, synthetic provider."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest

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


def pay(repo, provider, user, revision, domains, interval='month', key=None):
    from api.services.billing.manualPaymentService import ManualPaymentService
    request = repo.draftCheckoutRequest(user, revision, pricing(user, domains, interval), key or 'pay-' + str(uuid4()))
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


def submit(client, user, revision, key):
    from api.services.billing.pricingContext import issuePricingContext
    return client.post('/purchase-draft/checkout', headers={'Idempotency-Key': key}, json={
        'draftRevision': revision, 'expectedCatalogRevision': 1, 'contact': '+919999999999',
        'pricingContext': issuePricingContext(user, 'INR', 'hosting', catalogRevision=1)})


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


def test_changed_selection_replaces_empty_order_without_user_action(drafts, monkeypatch):
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    repo.saveDraft(old.userId, 1, ['banking'], 'month')
    response = submit(http(repo, provider, old.userId, monkeypatch), old.userId, 2, 'ui-pay-changed')
    assert response.status_code == 200, response.text
    checkout = response.json()['checkout']
    assert checkout['attemptId'] != old.attemptId and checkout['domains'] == ['banking']
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
    assert closed['recover_until'] == closed['closed_at'] + timedelta(minutes=45)
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
    assert checkout['state'] == 'confirming_earlier_payment' and checkout['attemptId'] == old.attemptId
    assert checkout['earlierPayment'] == {'selectionLabel': 'Pro monthly with Banking and Manufacturing',
                                          'amount': old.amount, 'currency': 'INR'}
    intent = pay(repo, provider, old.userId, 2, ['telecom'])
    assert intent.state == 'confirming_earlier_payment' and intent.earlierPayment['amount'] == old.amount
    assert serializeCheckoutIntent(intent, 'key')['state'] == 'confirming_earlier_payment'
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
    # The newest payment on the superseded Order bounds the late-authorization scan.
    newest = max(p['created_at'] for p in provider.payments.values())
    assert abs((closed['recover_until'] - timedelta(minutes=45)).timestamp() - newest) < 1


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
    late = payment(provider, old, 'captured')
    evidence = VerifiedPaymentEvidence(old.attemptId, old.invoiceId, old.userId, old.razorpayOrderId, late['id'],
        'initial_purchase', old.currency, 'captured', 'server_observation', old.amount, _now(), None, None, False)
    assert ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidence).state == 'requires_reconciliation'
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
    from api.services.billing import purchaseCheckoutRepository, purchaseCheckoutService, purchaseOperationRepository
    repo, provider, old, _ = controlledPurchase(drafts, monkeypatch)
    response = http(repo, provider, old.userId, monkeypatch).post(
        '/purchase-checkouts/' + old.attemptId + '/cancel', json={'expectedOperationRevision': 1},
        headers={'Idempotency-Key': 'cancel'})
    assert response.status_code in (404, 405)
    assert not hasattr(models, 'CancelPurchaseCheckoutRequest')
    assert not hasattr(purchaseCheckoutService.PurchaseCheckoutService, 'cancel')
    for name in ('acceptCancellation', 'cancellationReplay', 'resolvePurchaseCancellation', 'scheduleCancellationRetry'):
        assert not hasattr(purchaseCheckoutRepository.PurchaseCheckoutRepository, name), name
    assert not hasattr(purchaseOperationRepository.PurchaseOperationRepositoryMixin, 'resolvePurchaseCancellation')
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
    repo.recordPurchaseWorkerHeartbeat()
    assert pay(repo, provider, old.userId, 2, ['telecom']).attemptId != old.attemptId


def test_order_window_is_the_reserved_snapshot_window(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: True)
    repo, _, user, _ = drafts
    repo.saveDraft(user, 0, ['banking'], 'month')
    intent = repo.reserveDraftCheckout(user, 1, pricing(user, ['banking']), 'window')
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
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    slot = control(url, intent.attemptId)['slot_id']
    query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
          ('pay-' + str(uuid4()), slot))
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        bindPurchaseControlLocked(cursor, SimpleNamespace(userId=intent.userId, purchaseDraftRevision=1), intent)
    assert query(url, 'SELECT count(*) AS n FROM payment_slots WHERE user_id=%s', (intent.userId,), one=True)['n'] == 1
