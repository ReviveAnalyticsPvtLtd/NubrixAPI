"""Annual renewal admits one paid future period and preserves settled replays."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import Mock, patch

import pytest
from dateutil.relativedelta import relativedelta

from api.services.billing.manualBillingRepository import _payloadHash, _utc
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, checkout_client, request, counts
from test.test_manual_payment_entrypoints import paid_then_request, evidence


@pytest.fixture
def paidFuture(checkout_database):
    service, checkout = paid_then_request(checkout_database, 'annual_prepaid', 'renewal')
    intent = service.createCheckout(checkout)
    result = service.finalizeCapturedPayment(evidence(intent))
    assert result.nextPeriod is not None
    return service, checkout, intent, result.nextPeriod


def extraInvoice(database):
    repository, path = database
    subscription = read_row(path, 'subscriptions')
    start = _utc(subscription['current_period_end'])
    with sqlTransaction(path) as connection:
        connection.execute('''INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,
            total_amount,currency,period_start,period_end,metadata_json)
            VALUES('extra-year',?,?,'UPCOMING','renewal',10000,'INR',?,?,?)''',
            (USER, subscription['id'], start.isoformat(), (start + relativedelta(years=1)).isoformat(),
             json.dumps({'manualBilling': {'domains': ['banking'], 'revision': 1}})))
    return request('extra-key', mode='annual_prepaid', purpose='renewal', invoiceId='extra-year')


def legacyExtraAttempt(database, service, original, checkout):
    repository, path = database
    source = repository.attemptById(USER, original.attemptId)
    metadata = deepcopy(repository._json(source['metadata_json']))
    subscription = read_row(path, 'subscriptions')
    start = _utc(subscription['current_period_end'])
    metadata['manualBilling'].update(invoiceId='extra-year', periodStart=start.isoformat(),
        periodEnd=(start + relativedelta(years=1)).isoformat(),
        expiresAt=(NOW + timedelta(hours=1)).isoformat(),
        payloadHash=_payloadHash({'billingMode': checkout.billingMode, 'purpose': checkout.purpose,
            'invoiceId': 'extra-year', 'domains': ['banking']}))
    with sqlTransaction(path) as connection:
        connection.execute('''INSERT INTO billing_events(id,user_id,subscription_id,invoice_id,
            event_category,event_type,event_status,payment_attempt_type,payment_status,provider,
            provider_order_id,amount,currency,idempotency_key,metadata_json,attempted_at,occurred_at)
            VALUES('extra-attempt',?,?,'extra-year','payment_attempt','payment.attempt','created',
            'authenticated_checkout','pending_provider_ack','razorpay','extra-order',10000,'INR',?,?,?,?)''',
            (USER, subscription['id'], f'renewal:{USER}:{checkout.requestKey}', json.dumps(metadata),
             NOW.isoformat(), NOW.isoformat()))
    service.razorpayClient.order.orders['extra-order'] = {
        'id': 'extra-order', 'amount': 10000, 'currency': 'INR', 'status': 'created'}
    attempt = repository.attemptById(USER, 'extra-attempt')
    return repository._intentFromAttemptRow(attempt, metadata, USER, 'renewal')


def test_annual_prepare_returns_paid_future_without_another_invoice(checkout_database, checkout_client, paidFuture, monkeypatch):
    client, _, path = checkout_client
    prepare = Mock(return_value={'id': 'extra-year'})
    monkeypatch.setattr('api.services.billing.invoiceService.createUpcomingRenewalInvoice', prepare)
    _, _, original, future = paidFuture
    before = counts(path)
    response = client.post('/prepareRenewalInvoice', json={})
    assert response.status_code == 200, response.text
    result = response.json()['data']
    assert result['state'] == 'paid_scheduled'
    assert result['invoiceId'] == original.invoiceId
    assert result['nextPeriod']['start'] == future.start.isoformat()
    assert result['nextPeriod']['end'] == future.end.isoformat()
    assert result['creditsRefilled'] is False
    assert counts(path) == before
    prepare.assert_not_called()


def test_annual_paid_future_blocks_new_checkout(checkout_database, paidFuture):
    service, _, _, _ = paidFuture
    checkout = extraInvoice(checkout_database)
    before = counts(checkout_database[1])
    with pytest.raises(ValueError, match='RENEWAL_ALREADY_PAID'):
        service.createCheckout(checkout)
    assert counts(checkout_database[1]) == before
    assert len(service.razorpayClient.order.createdPayloads) == 2


@pytest.mark.parametrize('fresh_key', [False, True])
def test_old_extra_year_checkout_cannot_resume(checkout_database, paidFuture, fresh_key):
    service, _, original, _ = paidFuture
    checkout = extraInvoice(checkout_database)
    legacyExtraAttempt(checkout_database, service, original, checkout)
    if fresh_key:
        checkout = replace(checkout, requestKey='another-browser')
    with pytest.raises(ValueError, match='RENEWAL_ALREADY_PAID'):
        service.createCheckout(checkout)
    assert len(service.razorpayClient.order.createdPayloads) == 2


def test_capture_on_old_extra_year_order_requires_review(checkout_database, paidFuture):
    service, _, original, _ = paidFuture
    checkout = extraInvoice(checkout_database)
    extra = legacyExtraAttempt(checkout_database, service, original, checkout)
    before = read_row(checkout_database[1], 'credit_balances')
    result = service.finalizeCapturedPayment(evidence(extra, 'extra-payment'))
    assert result.state == 'requires_reconciliation' and not result.finalized
    assert read_row(checkout_database[1], 'credit_balances') == before
    with sqlTransaction(checkout_database[1]) as connection:
        assert connection.execute("SELECT status FROM \"Invoices\" WHERE id='extra-year'").fetchone()[0] == 'UPCOMING'
        assert connection.execute("SELECT failure_reason FROM billing_events WHERE provider_payment_id='extra-payment'").fetchone()[0] == 'FUTURE_PERIOD_ALREADY_PAID'


def test_paid_renewal_replays_without_refilling(checkout_database, paidFuture):
    service, checkout, original, _ = paidFuture
    before = read_row(checkout_database[1], 'credit_balances')
    assert service.createCheckout(checkout).attemptId == original.attemptId
    assert service.createCheckout(replace(checkout, requestKey='paid-browser')).state == 'captured'
    assert service.finalizeCapturedPayment(evidence(original)).state == 'already_finalized'
    assert read_row(checkout_database[1], 'credit_balances') == before


def test_annual_renewal_available_when_paid_future_becomes_current(checkout_database, paidFuture):
    service, _, _, future = paidFuture
    checkout = extraInvoice(checkout_database)
    with patch('api.services.billing.manualBillingRepository._now', return_value=future.start):
        intent = service.createCheckout(checkout)
    assert intent.razorpayOrderId and intent.invoiceId == 'extra-year'
    assert len(service.razorpayClient.order.createdPayloads) == 3
