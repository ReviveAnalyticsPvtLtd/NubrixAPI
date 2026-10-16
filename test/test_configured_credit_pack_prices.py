from test.pricingFixtures import catalog_source
"""Configured currency prices must reach the customer catalogue and invoice calculator."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.parametrize('currency,expected', [
    ('USD', [(100, 500, 90, 590), (500, 2000, 360, 2360), (1000, 3500, 630, 4130)]),
    ('INR', [(100, 50000, 9000, 59000), (500, 200000, 36000, 236000), (1000, 350000, 63000, 413000)]),
])
@pytest.mark.parametrize('mode', ['monthly_prepaid', 'annual_prepaid'])
def test_configured_packs_are_available_at_approved_base_prices(monkeypatch, currency, expected, mode):
    from api.services.credits import creditConfig
    from api.services.credits.topupService import TopupService
    monkeypatch.setattr(creditConfig, '_cachedCreditConfig', creditConfig._loadFromFile('config/credits.json'))
    service = TopupService.__new__(TopupService)
    monkeypatch.setattr(service, '_decodeToken', lambda _token: ('pack-customer', 'customer@example.test'))
    monkeypatch.setattr(service, '_subscription', lambda _user: {
        'status': 'active', 'plan_type': 'pro' if mode == 'monthly_prepaid' else 'annual',
        'billing_mode': mode, 'default_currency': currency,
        'current_period_start': datetime.now(timezone.utc).isoformat(),
        'current_period_end': (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    result = service.listPacks('verified-token')
    assert result['eligible'] is True
    assert [(p['credits'], p['amountBeforeTax'], p['taxAmount'], p['totalAmount']) for p in result['packs']] == expected
    assert [p['packId'] for p in result['packs']] == ['small', 'medium', 'large']
    assert all(p['currency'] == currency for p in result['packs'])
