"""Manual USD orders must not require USD subscription-plan support."""
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize('mode,name,amount', [
    ('monthly_prepaid', 'BILLING_USD_MONTHLY_AMOUNT', 2000),
    ('annual_prepaid', 'BILLING_USD_ANNUAL_AMOUNT', 20000),
])
def test_explicit_usd_amount_prices_manual_orders_without_provider_plans(monkeypatch, mode, name, amount):
    from api.services.billing.priceCatalog import getPlanPrice
    from api.services.billing.billingEngine import computeInvoiceSnapshot
    monkeypatch.setenv(name, str(amount))
    monkeypatch.delenv('RAZORPAY_PRO_PLAN_ID_USD', raising=False)
    monkeypatch.delenv('RAZORPAY_ANNUAL_PLAN_ID_USD', raising=False)
    provider, cache = MagicMock(), MagicMock()
    reference = getPlanPrice('USD', mode, provider=provider, cache=cache)
    snapshot = computeInvoiceSnapshot(mode, 'initial_purchase', 2, priceReference=reference, currency='USD')
    assert reference['amount'] == amount and reference['currency'] == 'USD'
    assert reference['source'] == 'backend_configuration'
    assert snapshot.amount_before_tax == amount * 2 and snapshot.currency == 'USD'
    provider.plan.fetch.assert_not_called()


@pytest.mark.parametrize('value', ['', '0', '-1', '20.00', 'not-a-price', '9007199254740992'])
def test_invalid_explicit_usd_price_cannot_fall_back_to_another_price(monkeypatch, value):
    from api.services.billing.priceCatalog import getPlanPrice, PriceUnavailable
    monkeypatch.setenv('BILLING_USD_MONTHLY_AMOUNT', value)
    monkeypatch.setenv('RAZORPAY_PRO_PLAN_ID_USD', 'plan_other_price')
    provider = MagicMock()
    with pytest.raises(PriceUnavailable):
        getPlanPrice('USD', 'monthly_prepaid', provider=provider, cache=MagicMock())
    provider.plan.fetch.assert_not_called()
