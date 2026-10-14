"""USD credit packs require explicit independent backend prices."""
import pytest


def test_missing_usd_pack_does_not_become_inr(monkeypatch):
    from api.services.credits import creditConfig
    monkeypatch.setattr(creditConfig, '_getCreditConfig', lambda: {'topup_packs': {
        'small': {'tokens': 1000000, 'amount': 49900, 'active': True}}})
    assert creditConfig.getTopupPack('small', 'INR')['amount'] == 49900
    assert creditConfig.getTopupPack('small', 'USD') is None


def test_independent_usd_pack_amount(monkeypatch):
    from api.services.credits import creditConfig
    monkeypatch.setattr(creditConfig, '_getCreditConfig', lambda: {'topup_packs': {
        'small': {'tokens': 1000000, 'amount': 49900, 'amounts': {'USD': 619}, 'active': True}}})
    pack = creditConfig.getTopupPack('small', 'USD')
    assert (pack['amount'], pack['currency'], pack['tokens']) == (619, 'USD', 1000000)


def test_usd_topup_snapshot_uses_usd_tax_base(monkeypatch):
    from api.services.credits import creditConfig
    from api.services.billing.billingEngine import computeTopupSnapshot
    monkeypatch.setattr(creditConfig, '_getCreditConfig', lambda: {'topup_packs': {
        'small': {'tokens': 1000000, 'amount': 49900, 'amounts': {'USD': 619}, 'active': True}}})
    snapshot = computeTopupSnapshot('small', 'monthly_prepaid', currency='USD')
    assert (snapshot.amount_before_tax, snapshot.tax.tax_amount, snapshot.total_amount, snapshot.currency) == (619, 112, 731, 'USD')


@pytest.mark.parametrize('amount', [True, 0, -1, '619', 6.19])
def test_bad_usd_pack_unavailable(monkeypatch, amount):
    from api.services.credits import creditConfig
    monkeypatch.setattr(creditConfig, '_getCreditConfig', lambda: {'topup_packs': {
        'small': {'tokens': 1000000, 'amount': 49900, 'amounts': {'USD': amount}, 'active': True}}})
    assert creditConfig.getTopupPack('small', 'USD') is None


@pytest.mark.parametrize('marker,service_status,http_status', [
    ('TOPUP_PRICE_UNAVAILABLE', 422, 422),
    ('TOPUP_PACK_UNKNOWN', 422, 400),
    ('TOPUP_NOT_ELIGIBLE', 403, 403),
])
def test_topup_route_preserves_typed_service_errors(monkeypatch, marker, service_status, http_status):
    import asyncio
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
        asyncio.run(createTopupOrder(CreateTopupOrderRequest(packId='small'), token='local-token', requestKey='local-key'))
    assert result.value.status_code == http_status
    assert result.value.detail['errorCode'] == marker
    assert result.value.detail['message'] == 'Pack cannot be purchased.'
    assert len(calls) == 1
