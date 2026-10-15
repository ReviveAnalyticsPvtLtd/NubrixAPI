"""Old deployment price values cannot override or replace the database catalog."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from api.services.billing.priceCatalog import getPlanPrice
from api.services.billing.pricingCatalogContracts import CatalogSnapshot,CatalogUnavailable
from test.test_pricing_catalog_contracts import BASELINE

@pytest.mark.parametrize('value',['9999','invalid','0'])
def test_env_cannot_override_catalog(monkeypatch,value):
    monkeypatch.setenv('BILLING_USD_MONTHLY_AMOUNT',value)
    catalog=CatalogSnapshot(1,BASELINE,datetime.now(timezone.utc),None,'fixture','Configured price regression')
    assert getPlanPrice('USD','monthly_prepaid',catalog=catalog)['amount']==2000

def test_env_cannot_fallback_during_catalog_outage(monkeypatch):
    monkeypatch.setenv('BILLING_USD_MONTHLY_AMOUNT','2000')
    def unavailable(**kwargs): raise CatalogUnavailable()
    monkeypatch.setattr('api.services.billing.priceCatalog.getPricingCatalogRepository',lambda:SimpleNamespace(loadCurrent=unavailable))
    with pytest.raises(CatalogUnavailable): getPlanPrice('USD','monthly_prepaid')
