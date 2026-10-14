"""A complete unpaid observation is required before checkout expiry."""
from datetime import timedelta

import pytest

from api.services.billing.checkoutRecoveryPolicy import classifyCheckoutProviderOutcome, mayExpireCheckout
from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
from test.test_manual_billing_runtime import NOW


@pytest.mark.parametrize('order_status,payment_status,complete,expected', [
    ('created', None, True, 'unpaid'),
    ('created', None, False, 'unknown'),
    ('attempted', 'created', True, 'processing'),
    ('attempted', 'authorized', True, 'authorized'),
    ('paid', 'captured', True, 'captured'),
    ('paid', None, True, 'unknown'),
    ('attempted', 'failed', True, 'unpaid'),
    ('attempted', 'refunded', True, 'unknown'),
    ('unsupported', None, True, 'unknown'),
])
def test_provider_outcomes(order_status, payment_status, complete, expected):
    payments = ({'status': payment_status},) if payment_status else ()
    assert classifyCheckoutProviderOutcome({'status': order_status}, payments, complete) == expected


@pytest.mark.parametrize('seconds,complete,payments,expected', [
    (0, True, (), True), (60, True, (), True), (61, True, (), False),
    (-1, True, (), False), (0, False, (), False),
    (0, True, ({'status': 'failed'},), False),
])
def test_expiry_requires_fresh_empty_complete_evidence(seconds, complete, payments, expected):
    snapshot = CheckoutProviderSnapshot('attempt', 'order', 'unpaid', NOW - timedelta(seconds=seconds),
        complete, 100, 'INR', payments)
    assert mayExpireCheckout(snapshot, NOW) is expected
