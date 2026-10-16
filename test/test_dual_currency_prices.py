"""Published references remain independent by currency and fail closed."""
from datetime import datetime, timezone
from copy import deepcopy
import pytest
from api.services.billing.priceCatalog import getPlanPrice, validateReference, PriceUnavailable
from api.services.billing.pricingCatalogContracts import CatalogSnapshot
from test.test_pricing_catalog_contracts import BASELINE

@pytest.mark.parametrize('currency,mode,sku', [('INR','monthly_prepaid','monthly'),('INR','annual_prepaid','annual'),('USD','monthly_prepaid','monthly'),('USD','annual_prepaid','annual')])
def test_independent_backend_prices(currency,mode,sku):
    catalog=CatalogSnapshot(1,BASELINE,datetime.now(timezone.utc),None,'fixture','Price reference fixture')
    reference=getPlanPrice(currency,mode,catalog=catalog)
    assert (reference['amount'],reference['currency']) == (BASELINE[currency][sku],currency)
    assert reference['catalogRevision'] == 1

@pytest.mark.parametrize('amount',[True,17.39,0,-1,'1739',None])
def test_invalid_reference_is_not_usable(amount):
    with pytest.raises(PriceUnavailable): validateReference({'amount':amount,'currency':'USD'},'USD')

def test_foreign_currency_reference_cannot_be_relabelled():
    with pytest.raises(PriceUnavailable): validateReference({'amount':200000,'currency':'INR'},'USD')

def test_changing_inr_does_not_convert_or_change_usd():
    prices=deepcopy(BASELINE); prices['INR']['monthly']=250000
    catalog=CatalogSnapshot(2,prices,datetime.now(timezone.utc),None,'fixture','Regional adjustment')
    assert getPlanPrice('USD','monthly_prepaid',catalog=catalog)['amount'] == 2000
