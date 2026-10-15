"""A display price and its tax breakdown belong to one published revision."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from jose import jwt

from api.services.billing.priceCatalog import getPlanPrice
from api.services.billing.pricingCatalogContracts import CatalogSnapshot, CatalogUnavailable
from api.services.billing.pricingService import PricingService
from api.services.billing.pricingContext import issuePricingContext
from api.services.credits.creditConfig import getTopupPacks
from test.test_pricing_catalog_contracts import BASELINE


@pytest.fixture
def catalog():
    return CatalogSnapshot(7, BASELINE, datetime.now(timezone.utc), None, 'system', 'Initial pricing catalog')


@pytest.mark.parametrize('currency,mode,amount', [('INR','monthly_prepaid',200000),('INR','annual_prepaid',2000000),('USD','monthly_prepaid',2000),('USD','annual_prepaid',20000)])
def test_reference_uses_catalog_never_environment(monkeypatch,catalog,currency,mode,amount):
    monkeypatch.setenv('BILLING_USD_MONTHLY_AMOUNT','999999')
    monkeypatch.setenv('RAZORPAY_PRO_PLAN_ID','obsolete_provider_price')
    reference = getPlanPrice(currency,mode,catalog=catalog)
    assert reference['amount'] == amount
    assert reference['catalogRevision'] == 7
    assert reference['source'] == 'pricing_catalog'
    assert reference['plan_id'].startswith('catalog:7:')


def test_all_credit_prices_share_the_supplied_catalog(catalog):
    packs = getTopupPacks('USD',catalog=catalog)
    assert {key:pack['amount'] for key,pack in packs.items()} == {'small':500,'medium':2000,'large':3500}
    assert {pack['catalogRevision'] for pack in packs.values()} == {7}


def test_catalog_read_happens_once_per_response(monkeypatch,catalog):
    monkeypatch.setenv('PRICING_PROXY_SECRET','catalog-test-secret')
    calls = []
    def load(): calls.append(1); return catalog
    service = PricingService(catalogRepository=SimpleNamespace(loadCurrent=load))
    result = service.getCatalog('visitor','US')
    assert result['catalogRevision'] == 7
    assert result['monthly']['amountMinor'] == 2000 and result['annual']['amountMinor'] == 20000
    assert calls == [1]
    assert not {'publishedBy','reason','history'} & result.keys()


def test_database_failure_cannot_resurrect_env_price(monkeypatch):
    monkeypatch.setenv('BILLING_USD_MONTHLY_AMOUNT','2000')
    def unavailable(): raise CatalogUnavailable()
    with pytest.raises(CatalogUnavailable):
        PricingService(catalogRepository=SimpleNamespace(loadCurrent=unavailable)).getCatalog('visitor','US')


def test_unissued_preview_rejects_stale_revision(monkeypatch,catalog):
    monkeypatch.setenv('PRICING_PROXY_SECRET','catalog-test-secret')
    context = issuePricingContext('user-a','USD','hosting',catalogRevision=6)
    token = jwt.encode({'userId':'user-a'},__import__('os').environ['SECRET_KEY'],algorithm='HS256')
    service = PricingService(catalogRepository=SimpleNamespace(loadCurrent=lambda:catalog))
    with pytest.raises(ValueError,match='PRICING_CHANGED'):
        service.preview(token,['banking'],'monthly_prepaid',context,expectedCatalogRevision=6)


def test_topup_snapshot_freezes_explicit_reference_without_reading_live_catalog(catalog):
    from api.services.billing.billingEngine import computeTopupSnapshot
    pack = {**getTopupPacks('USD',catalog=catalog)['small'], 'packId':'small'}
    snapshot = computeTopupSnapshot('small','monthly_prepaid',currency='USD',priceReference=pack)
    assert snapshot.amount_before_tax == 500
    assert snapshot.total_amount == 590
    assert snapshot.pricing_reference_snapshot_json['catalogRevision'] == 7
    assert snapshot.pricing_reference_snapshot_json['tokens'] == 1000000


def test_unsafe_computed_total_is_rejected_before_invoice_persistence():
    from api.services.billing.billingEngine import computeInvoiceSnapshot
    with pytest.raises(ValueError,match='INVALID_INVOICE_AMOUNT'):
        computeInvoiceSnapshot('monthly_prepaid','initial_purchase',4,currency='USD',
            priceReference={'amount':9007199254740991,'currency':'USD','plan_id':'catalog:7:monthly:USD'})
