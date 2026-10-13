"""Renewal retries retain their original financial identity across browsers."""
from dataclasses import replace

import pytest

from test.test_manual_checkout_http import checkout_database, counts
from test.test_manual_payment_entrypoints import paid_then_request
from test.test_manual_billing_runtime import database, sqlTransaction


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
