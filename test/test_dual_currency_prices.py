"""Price references must never reuse another currency's numeric amount."""
import json
from unittest.mock import Mock

import pytest

from api.services.billing.priceCatalog import getPlanPrice, PriceUnavailable


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID", "plan_inr_month")
    monkeypatch.setenv("RAZORPAY_ANNUAL_PLAN_ID", "plan_inr_year")
    monkeypatch.setenv("RAZORPAY_PRO_PLAN_ID_USD", "plan_usd_month")
    monkeypatch.setenv("RAZORPAY_ANNUAL_PLAN_ID_USD", "plan_usd_year")
    provider, cache = Mock(), Mock()
    cache.get.return_value = None
    return provider, cache


@pytest.mark.parametrize("currency,mode,amount,plan", [
    ("INR", "monthly_prepaid", 79900, "plan_inr_month"),
    ("INR", "annual_prepaid", 819900, "plan_inr_year"),
    ("USD", "monthly_prepaid", 1739, "plan_usd_month"),
    ("USD", "annual_prepaid", 18117, "plan_usd_year"),
])
def test_independent_backend_prices(transport, currency, mode, amount, plan):
    provider, cache = transport
    provider.plan.fetch.return_value = {"item": {"amount": amount, "currency": currency}}
    result = getPlanPrice(currency, mode, provider=provider, cache=cache)
    assert (result["amount"], result["currency"], result["plan_id"]) == (amount, currency, plan)
    saved = json.loads(cache.set.call_args.args[1])
    assert (saved["amount"], saved["currency"]) == (amount, currency)


@pytest.mark.parametrize("amount", [True, 17.39, 0, -1, "1739", None])
def test_invalid_provider_amount_is_not_usable(transport, amount):
    provider, cache = transport
    provider.plan.fetch.return_value = {"item": {"amount": amount, "currency": "USD"}}
    with pytest.raises(PriceUnavailable):
        getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)
    cache.set.assert_not_called()


@pytest.mark.parametrize("cached", [
    "79900", "not-json",
    json.dumps({"amount": 79900, "currency": "INR", "plan_id": "plan_inr_month"}),
    json.dumps({"amount": 1739, "currency": "USD", "plan_id": "retired_plan"}),
])
def test_foreign_or_incomplete_cache_is_rejected(transport, cached):
    provider, cache = transport
    provider.plan.fetch.side_effect = RuntimeError("offline")
    cache.get.return_value = cached
    with pytest.raises(PriceUnavailable):
        getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)


def test_complete_cache_survives_provider_outage(transport):
    provider, cache = transport
    provider.plan.fetch.side_effect = RuntimeError("offline")
    cache.get.return_value = json.dumps({"amount": 1739, "currency": "USD", "plan_id": "plan_usd_month",
                                        "fetched_at": "2026-10-14T00:00:00+00:00"})
    result = getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)
    assert (result["amount"], result["currency"], result["source"]) == (1739, "USD", "cache_fallback")


def test_cache_failure_does_not_discard_valid_provider_price(transport):
    provider, cache = transport
    provider.plan.fetch.return_value = {"item": {"amount": 1739, "currency": "USD"}}
    cache.set.side_effect = RuntimeError("cache offline")
    assert getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)["amount"] == 1739


def test_wrong_provider_currency_never_relabelled(transport):
    provider, cache = transport
    provider.plan.fetch.return_value = {"item": {"amount": 79900, "currency": "INR"}}
    with pytest.raises(PriceUnavailable):
        getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)


def test_unconfigured_usd_does_not_fetch_inr(transport, monkeypatch):
    provider, cache = transport
    monkeypatch.delenv("RAZORPAY_PRO_PLAN_ID_USD")
    with pytest.raises(PriceUnavailable):
        getPlanPrice("USD", "monthly_prepaid", provider=provider, cache=cache)
    provider.plan.fetch.assert_not_called()
