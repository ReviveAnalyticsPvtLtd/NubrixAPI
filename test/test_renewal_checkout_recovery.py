"""Renewal retries retain their original financial identity across browsers."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest

from test.test_manual_checkout_http import checkout_database, counts
from test.test_manual_payment_entrypoints import paid_then_request
from test.test_manual_billing_runtime import database, sqlTransaction, NOW


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_fresh_browser_key_resumes_owned_renewal(checkout_database, mode):
    service, request = paid_then_request(checkout_database, mode, 'renewal')
    first = service.createCheckout(request)
    before = counts(checkout_database[1])
    recovered = service.createCheckout(replace(request, requestKey='second-browser'))
    assert recovered.attemptId == first.attemptId
    assert recovered.razorpayOrderId == first.razorpayOrderId
    assert counts(checkout_database[1]) == before
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_new_annual_renewal_has_configured_deadline(checkout_database, monkeypatch):
    monkeypatch.setenv('MANUAL_CHECKOUT_TTL_SECONDS', '600')
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    assert service.createCheckout(request).expiresAt == NOW + timedelta(seconds=600)


def test_authorized_renewal_is_pending_even_after_session_deadline(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'attempted'
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_pending', 'order_id': first.razorpayOrderId, 'status': 'authorized',
        'amount': first.amount, 'currency': first.currency}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        recovered = service.createCheckout(replace(request, requestKey='second-browser'))
        assert recovered.state == 'authorized'
        assert recovered.attemptId == first.attemptId
        assert service.repository.expireStaleCheckouts()['attemptsExpired'] == 0
    assert len(provider.order.createdPayloads) == 2


def test_processing_renewal_can_be_checked_until_capture(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    order = provider.order.orders[first.razorpayOrderId]
    order['status'] = 'attempted'
    payment = {'id': 'pay_processing', 'order_id': first.razorpayOrderId, 'status': 'created',
        'amount': first.amount, 'currency': first.currency}
    provider.paymentsByOrder[first.razorpayOrderId] = [payment]
    pending = service.createCheckout(replace(request, requestKey='second-browser'))
    assert pending.state == 'provider_processing'
    order['status'] = 'paid'
    payment['status'] = 'captured'
    captured = service.createCheckout(replace(request, requestKey='second-browser'))
    assert captured.state == 'captured' and captured.attemptId == first.attemptId
    assert len(provider.order.createdPayloads) == 2


def test_legacy_unbounded_renewal_keeps_original_deadline(checkout_database):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    first = service.createCheckout(request)
    legacyEnd = datetime.max.replace(tzinfo=timezone.utc)
    attempt = service.repository.attemptById(request.userId, first.attemptId)
    metadata = service.repository._json(attempt['metadata_json'])
    metadata['manualBilling']['expiresAt'] = legacyEnd.isoformat()
    with sqlTransaction(checkout_database[1]) as connection:
        connection.execute('UPDATE billing_events SET metadata_json=? WHERE id=?',
            (json.dumps(metadata), first.attemptId))
    recovered = service.createCheckout(replace(request, requestKey='second-browser'))
    assert recovered.attemptId == first.attemptId and recovered.expiresAt == legacyEnd
    assert serializeCheckoutIntent(recovered, 'test-public-key')['expiresAt'] is None
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_failed_payment_history_after_deadline_requires_review(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'attempted'
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_failed', 'order_id': first.razorpayOrderId, 'status': 'failed',
        'amount': first.amount, 'currency': first.currency}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        recovered = service.createCheckout(replace(request, requestKey='second-browser'))
    assert recovered.state == 'requires_reconciliation' and recovered.attemptId == first.attemptId
    assert len(provider.order.createdPayloads) == 2


def test_lapsed_renewal_requires_provider_evidence_before_replacement(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        assert service.repository.expireStaleCheckouts()['attemptsExpired'] == 0
        replacement = service.createCheckout(replace(request, requestKey='fresh-review'))
        assert replacement.attemptId != first.attemptId and replacement.invoiceId == first.invoiceId
        assert service.repository.attemptById(request.userId, first.attemptId)['payment_status'] == 'expired'
    assert len(service.razorpayClient.order.createdPayloads) == 3


def test_incomplete_payment_listing_does_not_finalize_partial_money(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    page = [{'id': f'pay_{index}', 'order_id': first.razorpayOrderId, 'status': 'captured',
        'amount': first.amount, 'currency': first.currency} for index in range(100)]
    def payments(order, params):
        if params['skip']:
            raise TimeoutError('second page unavailable')
        return {'items': page}
    provider.order.payments = payments
    with pytest.raises(RuntimeError):
        service.createCheckout(replace(request, requestKey='second-browser'))
    with sqlTransaction(checkout_database[1]) as connection:
        assert connection.execute('SELECT status FROM "Invoices" WHERE id=\'renewal\'').fetchone()[0] == 'UPCOMING'
    assert len(provider.order.createdPayloads) == 2


def test_cancellation_during_provider_read_cannot_reopen_renewal(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    service.createCheckout(request)
    originalFetch = service.razorpayClient.order.fetch
    def fetch(order):
        service.repository.setRenewalOptOut(request.userId, True, None, 'cancel')
        return originalFetch(order)
    with patch.object(service.razorpayClient.order, 'fetch', side_effect=fetch):
        with pytest.raises(ValueError):
            service.createCheckout(replace(request, requestKey='second-browser'))
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_closed_attempt_with_received_money_cannot_offer_replacement(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    attempt = service.repository.attemptById(request.userId, first.attemptId)
    metadata = service.repository._json(attempt['metadata_json'])
    metadata['manualBilling'].update(expiresAt=(NOW - timedelta(seconds=1)).isoformat(),
        closedAt=NOW.isoformat(), closedReason='CHECKOUT_EXPIRED')
    with sqlTransaction(checkout_database[1]) as connection:
        connection.execute("UPDATE billing_events SET payment_status='expired',metadata_json=? WHERE id=?",
            (json.dumps(metadata), first.attemptId))
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'paid'
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_late', 'order_id': first.razorpayOrderId, 'status': 'captured',
        'amount': first.amount, 'currency': first.currency}]
    recovered = service.createCheckout(replace(request, requestKey='second-browser'))
    assert recovered.state == 'requires_reconciliation' and recovered.attemptId == first.attemptId
    assert len(provider.order.createdPayloads) == 2


def test_provider_read_failure_never_opens_another_renewal(checkout_database):
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    service.createCheckout(request)
    with patch.object(service.razorpayClient.order, 'fetch', side_effect=TimeoutError('unavailable')):
        with pytest.raises(RuntimeError):
            service.createCheckout(replace(request, requestKey='second-browser'))
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_captured_renewal_recovers_without_browser_callback(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'paid'
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_captured', 'order_id': first.razorpayOrderId, 'status': 'captured',
        'amount': first.amount, 'currency': first.currency}]
    recovered = service.createCheckout(replace(request, requestKey='second-browser'))
    assert recovered.state == 'captured' and recovered.attemptId == first.attemptId
    assert len(provider.order.createdPayloads) == 2


def test_adopted_browser_key_preserves_payload_conflict(checkout_database):
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    original = service.createCheckout(request)
    adopted = replace(request, requestKey='second-browser')
    service.createCheckout(adopted)
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        service.createCheckout(replace(adopted, payload={**adopted.payload, 'revision': 99}))
    assert service.createCheckout(adopted).attemptId == original.attemptId


def test_adopted_browser_key_replays_closed_history(checkout_database):
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    original = service.createCheckout(request)
    adopted = replace(request, requestKey='second-browser')
    service.createCheckout(adopted)
    with sqlTransaction(checkout_database[1]) as connection:
        connection.execute("UPDATE billing_events SET payment_status='cancelled' WHERE id=?", (original.attemptId,))
    replay = service.createCheckout(adopted)
    assert replay.attemptId == original.attemptId and replay.state == 'cancelled'
    assert len(service.razorpayClient.order.createdPayloads) == 2


@pytest.mark.parametrize('change', ['opt_out', 'quote', 'lifecycle'])
def test_fresh_key_cannot_reuse_changed_renewal(checkout_database, change):
    service, request = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    service.createCheckout(request)
    with sqlTransaction(checkout_database[1]) as connection:
        if change == 'opt_out':
            connection.execute('UPDATE subscriptions SET renewal_opt_out=1')
        elif change == 'quote':
            connection.execute('UPDATE "Invoices" SET total_amount=999 WHERE id=\'renewal\'')
        else:
            connection.execute("UPDATE subscriptions SET billing_state='{}'")
    with pytest.raises(ValueError):
        service.createCheckout(replace(request, requestKey='second-browser'))
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_stale_provider_observation_cannot_reserve_another_renewal(checkout_database):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    later = NOW + timedelta(hours=1)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        ManualBillingRecoveryService(service.repository, service.razorpayClient).recoverAttempt(
            service.repository.attemptById(request.userId, first.attemptId))
    # Provider evidence can age while reservation waits for the owner lock.
    with patch('api.services.billing.manualBillingRepository._now', return_value=later + timedelta(seconds=61)):
        with pytest.raises(RuntimeError, match='PROVIDER_STATUS_UNKNOWN'):
            service.repository.reserveCheckout(replace(request, requestKey='second-browser'))
    assert service.repository.attemptById(request.userId, first.attemptId)['payment_status'] == 'pending_provider_ack'
    with sqlTransaction(checkout_database[1]) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE event_category='payment_attempt' AND invoice_id='renewal'").fetchone()[0] == 1


def test_authorized_renewal_captured_after_deadline_returns_reconciliation(checkout_database):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'attempted'
    payment = {'id': 'pay_late_authorized', 'order_id': first.razorpayOrderId,
        'status': 'authorized', 'amount': first.amount, 'currency': first.currency}
    provider.paymentsByOrder[first.razorpayOrderId] = [payment]
    assert service.createCheckout(request).state == 'authorized'
    provider.order.orders[first.razorpayOrderId]['status'] = 'paid'
    payment['status'] = 'captured'
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        recovered = service.createCheckout(request)
        assert service.createCheckout(request).state == 'requires_reconciliation'
    assert recovered.state == 'requires_reconciliation' and recovered.attemptId == first.attemptId
    assert serializeCheckoutIntent(recovered, 'test-public-key')['state'] == 'requires_reconciliation'
    with sqlTransaction(checkout_database[1]) as connection:
        capture = connection.execute("SELECT event_status,failure_reason FROM billing_events WHERE event_type='payment.capture' AND provider_payment_id='pay_late_authorized'").fetchone()
        assert tuple(capture) == ('REQUIRES_RECONCILIATION', 'CAPTURE_OUTSIDE_WINDOW')
        assert connection.execute("SELECT status FROM \"Invoices\" WHERE id='renewal'").fetchone()[0] == 'UPCOMING'
    assert len(provider.order.createdPayloads) == 2
