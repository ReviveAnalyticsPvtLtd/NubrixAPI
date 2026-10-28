"""Automatic-capture payment policy and Order payload."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from test.test_manual_billing_runtime import database  # noqa: F401
from test.test_manual_checkout_http import checkout_database, request  # noqa: F401
from test.test_manual_payment_entrypoints import RecoverableProvider, paid_then_request
from api.services.billing.manualPaymentService import ManualPaymentService
from api.services.billing.purchaseProvider import purchaseProviderOptions

AUTOMATIC = {'capture': 'automatic', 'capture_options': {
    'automatic_expiry_period': 45, 'manual_expiry_period': 45, 'refund_speed': 'normal'}}


def test_capture_window_defaults_and_bounds(monkeypatch):
    from api.services.billing.paymentPolicy import captureWindowMinutes
    monkeypatch.delenv('PAYMENT_CAPTURE_WINDOW_MINUTES', raising=False)
    assert captureWindowMinutes() == 45
    for bad in ('11', '7201', 'abc', ''):
        monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', bad)
        with pytest.raises(ValueError, match='PAYMENT_CAPTURE_WINDOW_INVALID'):
            captureWindowMinutes()
    for good in ('12', '7200'):
        monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', good)
        assert captureWindowMinutes() == int(good)


def test_rules_cutoff_required(monkeypatch):
    from api.services.billing.paymentPolicy import paymentRulesEffectiveAt
    monkeypatch.delenv('PAYMENT_RULES_EFFECTIVE_AT', raising=False)
    with pytest.raises(RuntimeError, match='PAYMENT_ACTIONS_UNAVAILABLE'):
        paymentRulesEffectiveAt()
    monkeypatch.setenv('PAYMENT_RULES_EFFECTIVE_AT', 'not-a-date')
    with pytest.raises(RuntimeError, match='PAYMENT_ACTIONS_UNAVAILABLE'):
        paymentRulesEffectiveAt()
    monkeypatch.setenv('PAYMENT_RULES_EFFECTIVE_AT', '2026-10-21T00:00:00Z')
    assert paymentRulesEffectiveAt() == datetime(2026, 10, 21, tzinfo=timezone.utc)


def test_payment_is_dead_at_window_boundary(monkeypatch):
    from api.services.billing.paymentPolicy import paymentIsDead
    monkeypatch.delenv('PAYMENT_CAPTURE_WINDOW_MINUTES', raising=False)
    created = datetime(2026, 10, 21, tzinfo=timezone.utc)
    assert not paymentIsDead(created, created + timedelta(minutes=45, seconds=-1))
    assert paymentIsDead(created, created + timedelta(minutes=45))
    # The attempt's own reserved window wins over the current configuration.
    assert not paymentIsDead(created, created + timedelta(minutes=59), windowMinutes=60)
    assert paymentIsDead(created, created + timedelta(minutes=60), windowMinutes=60)


def test_order_options_use_refund_speed(monkeypatch):
    from api.services.billing.paymentPolicy import orderPaymentOptions
    assert orderPaymentOptions() == AUTOMATIC
    monkeypatch.setenv('PAYMENT_REFUND_SPEED', 'bogus')
    with pytest.raises(ValueError, match='PAYMENT_REFUND_SPEED_INVALID'):
        orderPaymentOptions()


def test_order_options_use_the_reserved_window(monkeypatch):
    from api.services.billing.paymentPolicy import orderPaymentOptions
    monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', '60')
    options = orderPaymentOptions(45)['capture_options']
    assert options['automatic_expiry_period'] == options['manual_expiry_period'] == 45
    for bad in (11, 7201, True, '45'):
        with pytest.raises(ValueError, match='PAYMENT_CAPTURE_WINDOW_INVALID'):
            orderPaymentOptions(bad)


@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
def test_every_purpose_order_is_automatic(checkout_database, purpose):
    repository, path = checkout_database
    service, checkout = paid_then_request(checkout_database, 'monthly_prepaid', purpose)
    provider = service.razorpayClient
    intent = service.createCheckout(checkout)
    assert provider.order.createdPayloads[-1]['payment'] == AUTOMATIC
    assert intent.snapshot['capturePolicy'] == 'automatic'
    assert intent.snapshot['captureWindowMinutes'] == 45
    import json, sqlite3
    metadata = json.loads(sqlite3.connect(path).execute(
        'select metadata_json from billing_events where id=?', (intent.attemptId,)).fetchone()[0])
    assert metadata['manualBilling']['capturePolicy'] == 'automatic'
    assert metadata['manualBilling']['captureWindowMinutes'] == 45


def test_rules_cutoff_unset_blocks_new_order(checkout_database, monkeypatch):
    repository, path = checkout_database
    provider = RecoverableProvider(path)
    monkeypatch.delenv('PAYMENT_RULES_EFFECTIVE_AT')
    service = ManualPaymentService.forProduction(provider, repository)
    with pytest.raises(RuntimeError, match='PAYMENT_ACTIONS_UNAVAILABLE'):
        service.createCheckout(request('no-cutoff'))
    assert provider.order.createdPayloads == []


def test_order_create_uses_bounded_timeout(checkout_database):
    repository, path = checkout_database
    provider = RecoverableProvider(path)
    seen = []
    original = provider.order.create
    provider.order.create = lambda payload, **options: (seen.append(options), original(payload))[1]
    ManualPaymentService.forProduction(provider, repository).createCheckout(request('timeout'))
    assert seen == [purchaseProviderOptions()]


def test_order_rejection_fails_closed(checkout_database):
    from razorpay.errors import BadRequestError
    repository, path = checkout_database
    provider = RecoverableProvider(path)
    def reject(payload, **options):
        raise BadRequestError('The amount must be at least INR 1.00')
    provider.order.create = reject
    service = ManualPaymentService.forProduction(provider, repository)
    with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
        service.createCheckout(request('rejected'))
    import json, sqlite3
    row = sqlite3.connect(path).execute(
        'select payment_status,provider_order_id,metadata_json from billing_events').fetchone()
    assert row[0] == 'failed' and not row[1]
    assert json.loads(row[2])['manualBilling']['closedReason'] == 'ORDER_NOT_CREATED'


@pytest.mark.parametrize('outcome', ['unpaid', 'refunded'])
def test_complete_terminal_listing_retires_stale_authorized_status(outcome):
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    event = {'event_category': 'payment_attempt', 'payment_status': 'authorized',
        'metadata_json': {'manualBilling': {'purpose': 'renewal', 'captureWindowMinutes': 45,
            'providerObservation': {'outcome': outcome, 'complete': True,
                'latestPaymentCreatedAt': now.isoformat()}}}}
    assert not paymentInFlight(event, now)
    event['metadata_json']['manualBilling']['providerObservation']['complete'] = False
    assert paymentInFlight(event, now)


def test_legacy_authorization_deadline_uses_finite_attempt_origin():
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    event = {'event_category': 'payment_attempt', 'payment_status': 'authorized',
        'attempted_at': now - timedelta(minutes=45),
        'metadata_json': {'manualBilling': {'purpose': 'renewal', 'captureWindowMinutes': 45,
            'expiresAt': datetime.max.replace(tzinfo=timezone.utc).isoformat()}}}
    assert not paymentInFlight(event, now)
    assert paymentInFlight(event, now - timedelta(seconds=1))


def test_incomplete_observation_preserves_legacy_closed_authorization(checkout_database):
    import json
    from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    from test.test_manual_billing_runtime import NOW, sqlTransaction
    repository, path = checkout_database
    intent = ManualPaymentService.forProduction(RecoverableProvider(path), repository).createCheckout(request('legacy-auth'))
    row = repository.attemptById(intent.userId, intent.attemptId)
    metadata = repository._json(row['metadata_json'])
    metadata['manualBilling'].update(closedAt=NOW.isoformat(), closedReason='SUPERSEDED',
        providerObservation={'outcome':'authorized', 'hasPayments':True,
            'latestPaymentCreatedAt':NOW.isoformat(), 'checkedAt':NOW.isoformat(), 'complete':True})
    with sqlTransaction(path) as db:
        db.execute("UPDATE billing_events SET payment_status='failed',metadata_json=? WHERE id=?",
                   (json.dumps(metadata),intent.attemptId))
    assert paymentInFlight(repository.attemptById(intent.userId,intent.attemptId),NOW)
    repository.recordProviderObservation(intent.attemptId,CheckoutProviderSnapshot(intent.attemptId,
        intent.razorpayOrderId,'unknown',NOW+timedelta(seconds=1),False,intent.amount,intent.currency,()))
    assert paymentInFlight(repository.attemptById(intent.userId,intent.attemptId),NOW+timedelta(seconds=1))


def test_recovery_defaults_to_bounded_provider_options():
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    assert ManualBillingRecoveryService(None, None).requestOptions == purchaseProviderOptions()
    assert ManualBillingRecoveryService(None, None, {'timeout': 2}).requestOptions == {'timeout': 2}


def test_installed_sdk_bounds_order_pages_and_receipt_lookup():
    from types import SimpleNamespace
    import razorpay
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    seen=[]
    def get(url,**options):
        seen.append((url,options))
        if url.endswith('/payments'):
            data={'items':[{'id':'pay-'+str(n),'order_id':'order-test','amount':100,'currency':'INR','status':'failed'}
                           for n in range(100)] if options['params']['skip']==0 else []}
        elif url.endswith('/order-test'):
            data={'id':'order-test','amount':100,'currency':'INR','status':'created'}
        else:
            data={'items':[]}
        return SimpleNamespace(status_code=200,json=lambda:data)
    provider=razorpay.Client(auth=('synthetic','synthetic'))
    provider.session=SimpleNamespace(get=get)
    repo=SimpleNamespace(closeUncreatedOrder=lambda identity:True)
    recovery=ManualBillingRecoveryService(repo,provider)
    row={'id':'attempt','provider_order_id':'order-test','amount':100,'currency':'INR'}
    assert recovery.inspectAttempt(row).complete
    assert recovery.recoverAttempt({**row,'provider_order_id':None})==1
    assert len(seen)==4 and all(options['timeout']==purchaseProviderOptions()['timeout'] for _,options in seen)
    assert seen[-1][1]['params']['receipt']=='attempt'


def test_timed_out_pay_changes_no_owned_checkout(checkout_database):
    from test.test_manual_checkout_http import counts
    repo,path=checkout_database
    provider=RecoverableProvider(path)
    service=ManualPaymentService.forProduction(provider,repo)
    first=service.createCheckout(request('first'))
    before=counts(path)
    def timeout(*args,**options):
        assert options==purchaseProviderOptions()
        raise TimeoutError('synthetic timeout')
    provider.order.fetch=timeout
    with pytest.raises(RuntimeError,match='PAYMENT_SERVICE_BUSY'):
        service.createCheckout(request('changed',domains=('telecom',)))
    assert counts(path)==before
    assert not repo._json(repo.attemptById(first.userId,first.attemptId)['metadata_json'])['manualBilling'].get('closedAt')
