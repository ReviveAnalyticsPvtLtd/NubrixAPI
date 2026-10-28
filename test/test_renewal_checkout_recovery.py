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


def test_authorized_renewal_confirms_even_after_session_deadline(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'attempted'
    # Started 30 minutes after the attempt opened: still inside its 45-minute capture window an hour in.
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_pending', 'order_id': first.razorpayOrderId, 'status': 'authorized',
        'amount': first.amount, 'currency': first.currency,
        'created_at': int((NOW + timedelta(minutes=30)).timestamp())}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        recovered = service.createCheckout(replace(request, requestKey='second-browser'))
        assert recovered.state == 'confirming_earlier_payment'
        assert recovered.attemptId == first.attemptId
        assert recovered.earlierPayment == {'selectionLabel': 'Pro monthly renewal with Banking',
                                            'amount': first.amount, 'currency': first.currency}
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


def test_failed_payment_after_deadline_is_replaced_on_the_same_invoice(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    provider.order.orders[first.razorpayOrderId]['status'] = 'attempted'
    provider.paymentsByOrder[first.razorpayOrderId] = [{
        'id': 'pay_failed', 'order_id': first.razorpayOrderId, 'status': 'failed',
        'amount': first.amount, 'currency': first.currency, 'created_at': int(NOW.timestamp())}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        replacement = service.createCheckout(replace(request, requestKey='second-browser'))
        old = service.repository.attemptById(request.userId, first.attemptId)
    assert replacement.attemptId != first.attemptId and replacement.invoiceId == first.invoiceId
    assert replacement.razorpayOrderId and len(provider.order.createdPayloads) == 3
    frozen = service.repository._json(old['metadata_json'])['manualBilling']
    assert old['payment_status'] == 'cancelled' and frozen['closedReason'] == 'SUPERSEDED'
    assert not frozen.get('closureReconciledAt')
    with sqlTransaction(checkout_database[1]) as connection:
        assert connection.execute("SELECT status FROM \"Invoices\" WHERE id='renewal'").fetchone()[0] == 'UPCOMING'


def test_lapsed_renewal_requires_provider_evidence_before_replacement(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(hours=1)):
        assert service.repository.expireStaleCheckouts()['attemptsExpired'] == 0
        replacement = service.createCheckout(replace(request, requestKey='fresh-review'))
        assert replacement.attemptId != first.attemptId and replacement.invoiceId == first.invoiceId
        # Only the attempt is replaced; it stays in the recovery scan for its capture window.
        assert service.repository.attemptById(request.userId, first.attemptId)['payment_status'] == 'cancelled'
    assert len(service.razorpayClient.order.createdPayloads) == 3


def test_incomplete_payment_listing_does_not_finalize_partial_money(checkout_database):
    service, request = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    first = service.createCheckout(request)
    provider = service.razorpayClient
    page = [{'id': f'pay_{index}', 'order_id': first.razorpayOrderId, 'status': 'captured',
        'amount': first.amount, 'currency': first.currency} for index in range(100)]
    def payments(order, params, **options):
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
    def fetch(order, **options):
        service.repository.setRenewalOptOut(request.userId, True, None, 'cancel')
        return originalFetch(order)
    with patch.object(service.razorpayClient.order, 'fetch', side_effect=fetch):
        # The opt-out landed after inspection: the evidence is stale, so nothing is decided.
        with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
            service.createCheckout(replace(request, requestKey='second-browser'))
    with pytest.raises(ValueError):
        service.createCheckout(replace(request, requestKey='third-browser'))
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_late_capture_on_a_closed_attempt_does_not_hold_the_slot(checkout_database):
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
    # Captured money is not in flight; the recovery scan finalizes it (spec 4.3 step 3, 4.4).
    renewed = service.createCheckout(replace(request, requestKey='second-browser'))
    assert renewed.attemptId != first.attemptId and renewed.invoiceId == first.invoiceId and renewed.razorpayOrderId
    assert len(provider.order.createdPayloads) == 3
    assert service.repository.attemptById(request.userId, first.attemptId)['payment_status'] == 'expired'


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
        with pytest.raises(RuntimeError, match='PAYMENT_SERVICE_BUSY'):
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


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_adopted_key_releases_review_after_confirmed_order_absence(checkout_database, mode):
    from api.services.billing.manualBillingPresentation import serializeCheckoutIntent
    service, request = paid_then_request(checkout_database, mode, 'renewal')
    adopted = replace(request, requestKey='second-browser')
    with patch.object(service.razorpayClient.order, 'create', side_effect=TimeoutError('not submitted')):
        with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
            service.createCheckout(request)
        with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
            service.createCheckout(adopted)
    with patch('api.services.billing.manualBillingRepository._now', return_value=NOW + timedelta(minutes=33)):
        closed = service.createCheckout(adopted)
        assert serializeCheckoutIntent(closed, 'test-public-key')['state'] == 'expired'
        replay = service.createCheckout(adopted)
        assert replay.attemptId == closed.attemptId and replay.state == 'expired'
        with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
            service.createCheckout(replace(adopted, payload={**adopted.payload, 'revision': 99}))
        replacement = service.createCheckout(replace(request, requestKey='fresh-review'))
    assert replacement.attemptId != closed.attemptId and replacement.razorpayOrderId
    assert replacement.invoiceId == closed.invoiceId
    original = service.repository.attemptById(request.userId, closed.attemptId)
    assert original['payment_status'] == 'failed' and not original['provider_order_id']
    assert original['idempotency_key'] == f'renewal:{request.userId}:{request.requestKey}'
    assert service.repository._json(original['metadata_json'])['manualBilling']['closedReason'] == 'ORDER_NOT_CREATED'
    with sqlTransaction(checkout_database[1]) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE event_type='checkout.request.bound' AND invoice_id='renewal'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM billing_events WHERE event_category='payment_attempt' AND invoice_id='renewal'").fetchone()[0] == 2
        assert connection.execute("SELECT status FROM \"Invoices\" WHERE id='renewal'").fetchone()[0] == 'UPCOMING'
    assert len(service.razorpayClient.order.createdPayloads) == 2


# -- one Pro term slot across the renewal boundary (spec 4.1, 5 case 4b) --

def _renewalReservedBeforeTermEnd(checkout_database, mode):
    """A renewal whose Order opened five minutes before the term end E."""
    from api.services.billing.manualBillingRepository import _utc
    from test.test_manual_payment_entrypoints import read_row
    service, request = paid_then_request(checkout_database, mode, 'renewal')
    end = _utc(read_row(checkout_database[1], 'subscriptions')['current_period_end'])
    with patch('api.services.billing.manualBillingRepository._now', return_value=end - timedelta(minutes=5)):
        renewal = service.createCheckout(request)
    assert renewal.razorpayOrderId
    return service, renewal, end


def _repurchase(service, end, key='repurchase'):
    from test.test_manual_checkout_http import request as checkoutRequest
    with patch('api.services.billing.manualBillingRepository._now', return_value=end + timedelta(minutes=1)):
        return service.createCheckout(checkoutRequest(key, ('telecom',), mode='monthly_prepaid'))


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_repurchase_after_term_end_replaces_empty_renewal_reserved_before_it(checkout_database, mode):
    service, renewal, end = _renewalReservedBeforeTermEnd(checkout_database, mode)
    repurchase = _repurchase(service, end)
    assert repurchase.attemptId != renewal.attemptId and repurchase.razorpayOrderId
    old = service.repository.attemptById(renewal.userId, renewal.attemptId)
    frozen = service.repository._json(old['metadata_json'])['manualBilling']
    # The open term's live attempt is replaced (a lapsed monthly one too).
    assert old['payment_status'] == 'cancelled' and frozen['closedReason'] == 'SUPERSEDED'
    with sqlTransaction(checkout_database[1]) as connection:
        renewalInvoice = connection.execute("SELECT status FROM \"Invoices\" WHERE id='renewal'").fetchone()[0]
        metadata = json.loads(connection.execute('SELECT metadata_json FROM "Invoices" WHERE id=?',
                                                 (repurchase.invoiceId,)).fetchone()[0])
    # Replacement never voids the renewal invoice (a legacy monthly term expires it at the boundary),
    # so the new invoice claims to replace nothing.
    assert renewalInvoice == ('EXPIRED' if mode == 'monthly_prepaid' else 'UPCOMING')
    assert 'replacesInvoiceIds' not in metadata['manualBilling']
    assert len(service.razorpayClient.order.createdPayloads) == 3


@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_repurchase_after_term_end_waits_for_renewal_payment_in_flight(checkout_database, mode):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.subscriptions.subscriptionModelBackfill import unresolvedMoneyFacts
    service, renewal, end = _renewalReservedBeforeTermEnd(checkout_database, mode)
    provider = service.razorpayClient
    provider.order.orders[renewal.razorpayOrderId]['status'] = 'attempted'
    provider.paymentsByOrder[renewal.razorpayOrderId] = [{
        'id': 'pay_renewal_in_flight', 'order_id': renewal.razorpayOrderId, 'status': 'authorized',
        'amount': renewal.amount, 'currency': renewal.currency,
        'created_at': int((end - timedelta(minutes=2)).timestamp())}]
    with patch('api.services.billing.manualBillingRepository._now', return_value=end - timedelta(minutes=1)):
        ManualBillingRecoveryService(service.repository, provider).recoverAttempt(
            service.repository.attemptById(renewal.userId, renewal.attemptId))
    before = counts(checkout_database[1])
    # Annual: the renewal is still the open term's live attempt. Legacy monthly: the boundary closed it,
    # but its authorized payment is still money in flight in the same slot.
    held = _repurchase(service, end)
    assert held.state == 'confirming_earlier_payment' and held.attemptId == renewal.attemptId
    interval = 'annual' if mode == 'annual_prepaid' else 'monthly'
    assert held.earlierPayment == {'selectionLabel': f'Pro {interval} renewal with Banking',
                                   'amount': renewal.amount, 'currency': renewal.currency}
    assert counts(checkout_database[1]) == before and len(provider.order.createdPayloads) == 2
    # The snapshot names the same held attempt the reservation waits on.
    attempt = service.repository.attemptById(renewal.userId, renewal.attemptId)
    unresolved, attempts = unresolvedMoneyFacts([], [attempt], end + timedelta(minutes=1))
    assert unresolved and [item.attemptId for item in attempts] == [renewal.attemptId]
