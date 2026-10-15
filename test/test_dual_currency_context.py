"""Location assertions and purchase contexts bind currency to an account."""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac

import pytest

from api.services.billing.pricingContext import (
    resolveCurrency, verifyProxyAssertion, issuePricingContext, verifyPricingContext,
)

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("PRICING_PROXY_SECRET", "local-context-unit-secret")


@pytest.mark.parametrize("host,profile,paid,expected", [
    ("IN", "United States", None, ("INR", "hosting")),
    ("GB", "India", None, ("USD", "hosting")),
    (None, "India", None, ("USD", "default")),
    ("XX", "IN", None, ("USD", "default")),
    (None, None, None, ("USD", "default")),
    ("IN", None, "USD", ("USD", "subscription")),
    (None, "United States", "INR", ("INR", "subscription")),
])
def test_currency_resolution(host, profile, paid, expected):
    assert resolveCurrency(host, profile, paid) == expected


def assertion(country="IN", scope="account", token="unit-token", timestamp=None):
    timestamp = str(int(NOW.timestamp())) if timestamp is None else timestamp
    message = f"v1\n{timestamp}\nGET\n/subscriptions/pricing\n{scope}\n{country}\n{hashlib.sha256(token.encode()).hexdigest()}"
    return {"X-Pricing-Timestamp": timestamp, "X-Pricing-Country": country,
            "X-Pricing-Signature": hmac.new(b"local-context-unit-secret", message.encode(), hashlib.sha256).hexdigest()}


def test_signed_proxy_country():
    assert verifyProxyAssertion(assertion(), "account", "unit-token", now=NOW) == "IN"


@pytest.mark.parametrize("change", ["country", "scope", "token", "expiry"])
def test_proxy_assertion_cannot_be_changed(change):
    headers = assertion()
    scope, token, now = "account", "unit-token", NOW
    if change == "country":
        headers["X-Pricing-Country"] = "US"
    elif change == "scope":
        scope = "visitor"
    elif change == "token":
        token = "another-token"
    else:
        now += timedelta(seconds=61)
    with pytest.raises(ValueError, match="PRICING_PROXY_INVALID"):
        verifyProxyAssertion(headers, scope, token, now=now)


def test_context_carries_currency_and_subject():
    token = issuePricingContext("a", "USD", "hosting", now=NOW)
    result = verifyPricingContext(token, "a", now=NOW)
    assert (result["sub"], result["currency"]) == ("a", "USD")


def test_context_cannot_cross_accounts():
    token = issuePricingContext("a", "USD", "hosting", now=NOW)
    with pytest.raises(ValueError, match="PRICING_CONTEXT_INVALID"):
        verifyPricingContext(token, "b", now=NOW)


def test_expired_context_requires_new_review():
    token = issuePricingContext("a", "USD", "hosting", now=NOW)
    with pytest.raises(ValueError, match="PRICING_CONTEXT_EXPIRED"):
        verifyPricingContext(token, "a", now=NOW + timedelta(seconds=601))


@pytest.mark.parametrize("token", [None, "", "unsigned", "a.b.c"])
def test_invalid_context_never_defaults_to_inr(token):
    with pytest.raises(ValueError, match="PRICING_CONTEXT_"):
        verifyPricingContext(token, "a", now=NOW)
