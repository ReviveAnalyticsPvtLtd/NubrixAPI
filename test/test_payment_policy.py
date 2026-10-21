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
