"""Annual service intervals and once-only refill through SQL finalization."""
from dateutil.relativedelta import relativedelta
from test.test_manual_billing_runtime import database, USER, NOW, read_row
from test.test_manual_checkout_http import checkout_database, request
from test.test_manual_payment_entrypoints import evidence, RecoverableProvider
from api.services.billing.manualPaymentService import ManualPaymentService


def test_annual_capture_commits_one_year_and_one_refill(checkout_database):
    repository, path = checkout_database
    intent = repository.reserveCheckout(request('annual', mode='annual_prepaid'))
    intent = repository.bindProviderOrder(intent.attemptId, {'id': 'annual-order'})
    result = repository.finalizeCapturedPayment(evidence(intent))
    assert result.currentPeriod.end == NOW + relativedelta(years=1)
    assert result.currentPeriod.billingMode == 'annual_prepaid'
    assert read_row(path, 'subscriptions')['billing_mode'] == 'annual_prepaid'
    assert result.creditsRefilled
    replay = repository.finalizeCapturedPayment(evidence(intent))
    assert replay.finalized and not replay.creditsRefilled
    assert read_row(path, 'credit_balances')['plan_tier'] == 'annual'
