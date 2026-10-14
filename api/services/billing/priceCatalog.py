"""Validated independent INR/USD reference prices; amounts are minor units."""
from datetime import datetime, timezone
import json
import os

PLAN_ENV = {
    ("INR", "monthly_prepaid"): "RAZORPAY_PRO_PLAN_ID",
    ("INR", "annual_prepaid"): "RAZORPAY_ANNUAL_PLAN_ID",
    ("USD", "monthly_prepaid"): "RAZORPAY_PRO_PLAN_ID_USD",
    ("USD", "annual_prepaid"): "RAZORPAY_ANNUAL_PLAN_ID_USD",
}


class PriceUnavailable(ValueError):
    def __init__(self):
        super().__init__("PRICE_UNAVAILABLE")


def validateReference(reference, currency, planId=None):
    if (not isinstance(reference, dict) or type(reference.get("amount")) is not int
            or not 0 < reference["amount"] <= 9007199254740991
            or reference.get("currency") != currency
            or (planId is not None and reference.get("plan_id") != planId)):
        raise PriceUnavailable()
    return reference


def getPlanPrice(currency: str, billingMode: str, *, provider=None, cache=None, now=None) -> dict:
    mode = "monthly_prepaid" if billingMode == "monthly_recurring" else billingMode
    envName = PLAN_ENV.get((currency, mode))
    planId = os.environ.get(envName, "") if envName else ""
    if not planId:
        raise PriceUnavailable()
    if provider is None:
        import razorpay
        provider = razorpay.Client(auth=(os.environ.get("RAZORPAY_KEY_ID", ""),
                                        os.environ.get("RAZORPAY_KEY_SECRET", "")))
    if cache is None:
        import redis
        cache = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                            port=int(os.environ.get("REDIS_PORT", "6379")),
                            password=os.environ.get("REDIS_PASSWORD"), decode_responses=True,
                            socket_connect_timeout=2, socket_timeout=2)
    key = f"billing:price:v2:{currency}:{planId}"
    try:
        item = provider.plan.fetch(planId)["item"]
        reference = validateReference({"amount": item.get("amount"), "currency": item.get("currency"),
            "plan_id": planId, "source": "razorpay_plan_fetch",
            "fetched_at": (now or datetime.now(timezone.utc)).isoformat()}, currency, planId)
    except Exception:
        try:
            reference = validateReference(json.loads(cache.get(key)), currency, planId)
            if not isinstance(reference.get("fetched_at"), str):
                raise PriceUnavailable()
            return {**reference, "source": "cache_fallback"}
        except Exception:
            raise PriceUnavailable() from None
    try:
        cache.set(key, json.dumps(reference), ex=86400)
    except Exception:
        pass  # A cache outage must not discard a validated provider price.
    return reference
