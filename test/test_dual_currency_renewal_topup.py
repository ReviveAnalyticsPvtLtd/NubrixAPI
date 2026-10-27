"""USD pack amounts come only from the published catalog."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from api.services.billing.pricingCatalogContracts import CatalogSnapshot
from test.test_pricing_catalog_contracts import BASELINE

@pytest.fixture
def configured(monkeypatch):
    from api.services.credits import creditConfig
    prices=deepcopy(BASELINE); prices['USD']['small']=619
    catalog=CatalogSnapshot(1,prices,datetime.now(timezone.utc),None,'fixture','Pack price fixture')
    monkeypatch.setattr('api.services.billing.pricingCatalogRepository.getPricingCatalogRepository',lambda:SimpleNamespace(loadCurrent=lambda **kwargs:catalog))
    monkeypatch.setattr(creditConfig,'_getCreditConfig',lambda:{'topup_packs':{'small':{'tokens':1000000,'amount':999999,'amounts':{'USD':99999},'active':True}}})
    return creditConfig

def test_legacy_config_amount_does_not_override_catalog(configured):
    assert configured.getTopupPack('small','INR')['amount'] == 50000
    assert configured.getTopupPack('small','USD')['amount'] == 619

def test_usd_topup_snapshot_uses_usd_tax_base(configured):
    from api.services.billing.billingEngine import computeTopupSnapshot
    snapshot=computeTopupSnapshot('small','monthly_prepaid',currency='USD')
    assert (snapshot.amount_before_tax,snapshot.tax.tax_amount,snapshot.total_amount,snapshot.currency)==(619,112,731,'USD')

def test_retired_pack_is_unavailable(configured,monkeypatch):
    monkeypatch.setattr(configured,'_getCreditConfig',lambda:{'topup_packs':{'small':{'tokens':1000000,'active':False}}})
    assert configured.getTopupPack('small','USD') is None

@pytest.mark.parametrize('marker,service_status,http_status', [
    ('TOPUP_PRICE_UNAVAILABLE', 422, 422),
    ('TOPUP_PACK_UNKNOWN', 422, 400),
    ('TOPUP_NOT_ELIGIBLE', 403, 403),
])
def test_topup_route_preserves_typed_service_errors(monkeypatch, marker, service_status, http_status):
    from fastapi import HTTPException
    from api.models import CreateTopupOrderRequest
    from api.routers.credits import createTopupOrder
    from api.services.credits.topupService import topupService
    from utils.exceptionHandler import CustomException
    calls = []
    def unavailable(**kwargs):
        calls.append(kwargs)
        raise CustomException(ValueError(marker), statusCode=service_status,
                              uiMessage='Pack cannot be purchased.', errorCode=marker)
    monkeypatch.setattr(topupService, 'createTopupOrder', unavailable)
    with pytest.raises(HTTPException) as result:
        createTopupOrder(CreateTopupOrderRequest(packId='small'), token='local-token', requestKey='local-key')
    assert result.value.status_code == http_status
    assert result.value.detail['errorCode'] == marker
    assert result.value.detail['message'] == 'Pack cannot be purchased.'
    assert len(calls) == 1
