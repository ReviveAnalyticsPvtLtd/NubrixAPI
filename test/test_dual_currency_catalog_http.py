"""Catalogue assembly uses account facts without locking an unpaid INR default."""
from unittest.mock import Mock

import pytest

from api.services.billing.pricingService import PricingService
from api.services.billing.priceCatalog import PriceUnavailable


def service(monkeypatch, paid=False, currency="INR", missing=False):
    monkeypatch.setenv("PRICING_PROXY_SECRET", "catalog-unit-secret")
    def prices(code, mode):
        if missing:
            raise PriceUnavailable()
        return {"amount": 1739 if mode == "monthly_prepaid" else 18117, "currency": code}
    instance = PricingService(client=Mock(), priceLoader=prices)
    instance.accountFacts = lambda token: ("user-a", "India", currency if paid else None)
    return instance


def test_unpaid_default_inr_does_not_override_host(monkeypatch):
    result = service(monkeypatch).getCatalog("account", "US", token="verified")
    assert (result["currency"], result["subject"]) == ("USD", "user-a")
    assert result["monthly"] == {"available": True, "amountMinor": 1739}
    assert result["pricingContext"]


def test_paid_currency_overrides_travel(monkeypatch):
    result = service(monkeypatch, paid=True).getCatalog("account", "US", token="verified")
    assert (result["currency"], result["countrySource"]) == ("INR", "subscription")


def test_visitors_have_no_account_context(monkeypatch):
    result = service(monkeypatch).getCatalog("visitor", "IN")
    assert result["currency"] == "INR"
    assert result["subject"] is None and result["pricingContext"] is None


def test_missing_price_returns_availability_not_invented_amount(monkeypatch):
    result = service(monkeypatch, missing=True).getCatalog("visitor", "US")
    assert result["monthly"] == {"available": False, "reason": "PRICE_UNAVAILABLE"}
    assert result["annual"] == {"available": False, "reason": "PRICE_UNAVAILABLE"}


def test_account_scope_requires_authentication(monkeypatch):
    with pytest.raises(ValueError, match="PRICING_AUTH_REQUIRED"):
        service(monkeypatch).getCatalog("account", "US")
