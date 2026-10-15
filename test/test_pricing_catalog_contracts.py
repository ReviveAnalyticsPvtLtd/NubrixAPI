"""Money validation at the privileged catalog boundary, independent of HTTP."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from api.pricingModels import PublishPricingBody
from api.services.billing.pricingCatalogContracts import MAX_PRICE_MINOR, validatePrices


BASELINE = {
    'INR': {'monthly': 200000, 'annual': 2000000, 'small': 50000, 'medium': 200000, 'large': 350000},
    'USD': {'monthly': 2000, 'annual': 20000, 'small': 500, 'medium': 2000, 'large': 3500},
}


def test_exact_catalog_returns_an_independent_mapping():
    prices = deepcopy(BASELINE)
    result = validatePrices(prices)
    prices['USD']['monthly'] = 1
    assert result == BASELINE


@pytest.mark.parametrize('bad', [True, False, 20.0, '20', 0, -1, MAX_PRICE_MINOR + 1, None])
def test_rejects_non_integral_or_unsafe_amount(bad):
    prices = deepcopy(BASELINE)
    prices['USD']['monthly'] = bad
    with pytest.raises(ValueError):
        validatePrices(prices)


@pytest.mark.parametrize('mutation', ['missing_currency', 'extra_currency', 'missing_sku', 'extra_sku', 'inr_minimum'])
def test_catalog_requires_supported_complete_prices(mutation):
    prices = deepcopy(BASELINE)
    if mutation == 'missing_currency': del prices['USD']
    if mutation == 'extra_currency': prices['EUR'] = prices['USD']
    if mutation == 'missing_sku': del prices['INR']['small']
    if mutation == 'extra_sku': prices['INR']['custom'] = 100
    if mutation == 'inr_minimum': prices['INR']['small'] = 99
    with pytest.raises(ValueError): validatePrices(prices)


def test_publish_model_normalizes_reason_only():
    body = PublishPricingBody(expectedRevision=1, reason='  Regional review  ', prices=BASELINE)
    assert body.reason == 'Regional review'
    assert body.prices.model_dump() == BASELINE


@pytest.mark.parametrize('changes', [
    {'expectedRevision': True}, {'expectedRevision': 1.0}, {'expectedRevision': 0},
    {'reason': '    '}, {'reason': 'abcd'}, {'reason': 'a' * 501},
    {'adminId': 'spoofed'}, {'prices': {**BASELINE, 'EUR': BASELINE['USD']}},
    {'prices': {**BASELINE, 'USD': {**BASELINE['USD'], 'small': True}}},
])
def test_publish_rejects_invalid_input(changes):
    body = {'expectedRevision': 1, 'reason': 'Regional review', 'prices': BASELINE, **changes}
    with pytest.raises(ValidationError): PublishPricingBody.model_validate(body)
