"""Trusted country assertions and account-bound currency selection."""
from datetime import datetime, timezone
import hashlib
import hmac
import os

from jose import jwt, JWTError


def _secret():
    value = os.environ.get("PRICING_PROXY_SECRET")
    if not value:
        raise RuntimeError("PRICING_NOT_CONFIGURED")
    return value


def resolveCurrency(hostCountry, profileCountry, paidCurrency=None):
    if paidCurrency is not None:
        if paidCurrency not in {"INR", "USD"}:
            raise ValueError("BILLING_CURRENCY_UNSUPPORTED")
        return paidCurrency, "subscription"
    host = str(hostCountry or "").strip().upper()
    if len(host) == 2 and host.isascii() and host.isalpha() and host not in {"XX", "ZZ"}:
        return ("INR" if host == "IN" else "USD"), "hosting"
    # Self-reported onboarding country cannot authorize regional prices.
    # Keep the argument for callers; only trusted location or paid currency wins.
    return "USD", "default"


def verifyProxyAssertion(headers, scope, accessToken, now=None):
    normalized = {key.lower(): value for key, value in headers.items()}
    timestamp = normalized.get("x-pricing-timestamp", "")
    country = normalized.get("x-pricing-country", "")
    signature = normalized.get("x-pricing-signature", "")
    secret = _secret()
    try:
        epoch = int(timestamp)
        if scope not in {"visitor", "account"} or abs((now or datetime.now(timezone.utc)).timestamp() - epoch) > 60:
            raise ValueError()
        if country and (len(country) != 2 or not country.isascii() or not country.isalpha() or country != country.upper()):
            raise ValueError()
        digest = hashlib.sha256((accessToken or "").encode()).hexdigest()
        message = f"v1\n{timestamp}\nGET\n/subscriptions/pricing\n{scope}\n{country}\n{digest}"
        expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("PRICING_PROXY_INVALID") from None
    return country or None


def issuePricingContext(userId, currency, source, now=None):
    if currency not in {"INR", "USD"} or not userId:
        raise ValueError("PRICING_CONTEXT_INVALID")
    epoch = int((now or datetime.now(timezone.utc)).timestamp())
    return jwt.encode({"iss": "nubrix-pricing", "aud": "initial-purchase", "v": 1,
        "sub": str(userId), "currency": currency, "countrySource": source,
        "iat": epoch, "exp": epoch + 600}, _secret(), algorithm="HS256")


def verifyPricingContext(context, userId, now=None):
    if not context:
        raise ValueError("PRICING_CONTEXT_REQUIRED")
    secret = _secret()
    try:
        claims = jwt.decode(context, secret, algorithms=["HS256"],
                            issuer="nubrix-pricing", audience="initial-purchase",
                            options={"verify_exp": False})
        epoch = (now or datetime.now(timezone.utc)).timestamp()
        if (claims.get("v") != 1 or claims.get("sub") != str(userId)
                or claims.get("currency") not in {"INR", "USD"}
                or type(claims.get("exp")) is not int or type(claims.get("iat")) is not int
                or claims["iat"] > epoch + 60 or claims["exp"] - claims["iat"] != 600):
            raise ValueError("PRICING_CONTEXT_INVALID")
        if claims["exp"] <= epoch:
            raise ValueError("PRICING_CONTEXT_EXPIRED")
        return claims
    except JWTError:
        raise ValueError("PRICING_CONTEXT_INVALID") from None
