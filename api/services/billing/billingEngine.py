"""
billingEngine.py

Centralized backend price and tax computation for all billing flows.

Sources base prices from Razorpay reference plans (monthly via
RAZORPAY_PRO_PLAN_ID, annual via RAZORPAY_ANNUAL_PLAN_ID) with Redis
fallback cache. Computes final invoice-ready amounts by combining
domain count, billing mode, tax engine output, and proration windows.

Credit top-up packs are the one exception: they are not Razorpay plans, so
their prices come from config/credits.json instead.

All outputs are immutable value objects (PricingSnapshot) suitable for
direct persistence to invoice columns. Clients cannot override any
computed amount, tax, or currency field.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["computeInvoiceSnapshot", "computeTopupSnapshot"]


from api.services.billing.billingConfig import RAZORPAY_ANNUAL_PLAN_ID
from api.services.billing.billingClient import getBillingRedisClient
from api.services.billing.taxEngine import computeTax, TaxBreakdown
from utils.logger import logger
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
import razorpay
import redis
import time
import os


_PRICE_CACHE_TTL = 24 * 60 * 60
_PRICE_FETCH_RETRIES = 3


def _getRazorpayClient() -> razorpay.Client:
    """
    Create a Razorpay client using environment credentials.

    Returns:
        razorpay.Client: Authenticated Razorpay client.
    """
    return razorpay.Client(
        auth=(
            os.environ.get("RAZORPAY_KEY_ID", ""),
            os.environ.get("RAZORPAY_KEY_SECRET", ""),
        )
    )




@dataclass(frozen=True)
class PricingSnapshot:
    """
    Immutable invoice-ready pricing computation result.

    Once created, this snapshot is frozen and suitable for direct
    persistence to invoice columns. No field may be modified after
    computation.

    Attributes:
        billing_mode: 'monthly_recurring' or 'annual_prepaid'.
        billing_reason: 'initial_purchase', 'renewal', or 'proration'.
        domain_count: Number of active/chargeable domains.
        base_price_per_domain: Per-domain price in paise from the plan.
        amount_before_tax: Total taxable base (domains * price * factor).
        tax: The full TaxBreakdown object.
        total_amount: Final payable amount in paise.
        currency: ISO currency code.
        pricing_version: Snapshot identifier for audit trail.
        plan_id: Razorpay plan ID used as price source.
        pricing_reference_snapshot_json: Price-source metadata snapshot.
        period_start: Billing period start (ISO string).
        period_end: Billing period end (ISO string).
    """
    billing_mode: str
    billing_reason: str
    domain_count: int
    base_price_per_domain: int
    amount_before_tax: int
    tax: TaxBreakdown
    total_amount: int
    currency: str
    pricing_version: str
    plan_id: str
    pricing_reference_snapshot_json: dict
    period_start: str
    period_end: str

    def to_dict(self) -> dict:
        """Return a plain dict suitable for JSON serialization and invoice persistence."""
        result = asdict(self)
        result["tax_amount"] = self.tax.tax_amount
        result["tax_rule_version"] = self.tax.tax_rule_version
        result["place_of_supply_snapshot"] = self.tax.place_of_supply_snapshot
        result["tax_breakdown_json"] = self.tax.to_dict()
        return result


def _fetchPlanAmount(planId: str, cacheKey: str) -> dict:
    """
    Fetch the per-domain price in paise from a Razorpay reference plan.

    Retries with exponential backoff and falls back to the Redis-cached
    value if the Razorpay API is unreachable.

    Args:
        planId: Razorpay plan ID to fetch.
        cacheKey: Redis key for caching the amount.

    Returns:
        dict: Snapshot containing amount/currency/fetched_at/source.

    Raises:
        RuntimeError: If the API fails and no cached value exists.
    """
    razorpayClient = _getRazorpayClient()
    redisClient = getBillingRedisClient()
    lastError = None

    for attempt in range(1, _PRICE_FETCH_RETRIES + 1):
        try:
            plan = razorpayClient.plan.fetch(planId)
            amount = plan["item"]["amount"]
            redisClient.set(cacheKey, str(amount), ex=_PRICE_CACHE_TTL)
            fetchedAt = datetime.now(timezone.utc).isoformat()
            currency = plan["item"].get("currency", "INR")
            logger.info(
                f"Plan fetched — planId={planId}, amount={amount}, "
                f"currency={currency}"
            )
            return {
                "plan_id": planId,
                "amount": amount,
                "currency": currency,
                "fetched_at": fetchedAt,
                "source": "razorpay_plan_fetch",
            }
        except Exception as e:
            lastError = e
            logger.warning(
                f"Plan fetch attempt {attempt}/{_PRICE_FETCH_RETRIES} "
                f"failed for {planId}: {e}"
            )
            if attempt < _PRICE_FETCH_RETRIES:
                time.sleep(2 ** attempt)

    cached = redisClient.get(cacheKey)
    if cached:
        fetchedAt = datetime.now(timezone.utc).isoformat()
        logger.warning(f"Using cached plan amount for {planId}: {cached}")
        return {
            "plan_id": planId,
            "amount": int(cached),
            "currency": "INR",
            "fetched_at": fetchedAt,
            "source": "cache_fallback",
        }

    raise RuntimeError(
        f"Cannot fetch plan {planId} and no cached value available: {lastError}"
    )


def _getMonthlyBasePrice() -> dict:
    """
    Fetch the monthly per-domain price from the Razorpay reference plan.

    Returns:
        dict: Monthly plan metadata snapshot.
    """
    planId = os.environ.get("RAZORPAY_PRO_PLAN_ID", "")
    return _fetchPlanAmount(planId, "billing:monthly_base_price")


def _getAnnualBasePrice() -> dict:
    """
    Fetch the annual per-domain price from the Razorpay reference plan.

    Returns:
        dict: Annual plan metadata snapshot.
    """
    return _fetchPlanAmount(RAZORPAY_ANNUAL_PLAN_ID, "billing:annual_base_price")


def computeInvoiceSnapshot(billingMode: str, billingReason: str,
                           domainCount: int, customerState: str = None,
                           periodStart: datetime = None,
                           periodEnd: datetime = None,
                           prorationAnchorStart: datetime = None,
                           prorationAnchorEnd: datetime = None) -> PricingSnapshot:
    """
    Compute an immutable invoice-ready pricing snapshot.

    Supports all billing scenarios:
        - monthly_recurring initial_purchase: domainCount * monthlyBase + tax
        - monthly_recurring renewal: domainCount * monthlyBase + tax
        - annual_prepaid initial_purchase: domainCount * annualBase + tax
        - annual_prepaid renewal: domainCount * annualBase + tax
        - proration (add-domain mid-cycle): domainCount * dailyRate * remainingDays + tax

    Args:
        billingMode: 'monthly_recurring' or 'annual_prepaid'.
        billingReason: 'initial_purchase', 'renewal', or 'proration'.
        domainCount: Number of domains to charge for.
        customerState: Customer billing state code for tax determination.
        periodStart: Billing period start (required for renewal/initial).
        periodEnd: Billing period end (required for renewal/initial).
        prorationAnchorStart: Start of remaining period (proration only).
        prorationAnchorEnd: End of remaining period (proration only).

    Returns:
        PricingSnapshot: Frozen, immutable pricing result.

    Raises:
        ValueError: If billing mode/reason combination is unsupported or
            required period arguments are missing.
    """
    now = datetime.now(timezone.utc)

    if billingMode in ("monthly_prepaid", "monthly_recurring"):
        priceRef = _getMonthlyBasePrice()
        basePrice = priceRef["amount"]
        planId = os.environ.get("RAZORPAY_PRO_PLAN_ID", "")
        if periodStart is None:
            periodStart = now
        if periodEnd is None:
            periodEnd = periodStart + relativedelta(months=1)
    elif billingMode == "annual_prepaid":
        priceRef = _getAnnualBasePrice()
        basePrice = priceRef["amount"]
        planId = RAZORPAY_ANNUAL_PLAN_ID
        if periodStart is None:
            periodStart = now
        if periodEnd is None:
            periodEnd = periodStart + relativedelta(years=1)
    else:
        raise ValueError(f"Unsupported billing mode: {billingMode}")

    if billingReason in ("initial_purchase", "renewal"):
        amountBeforeTax = domainCount * basePrice
    elif billingReason == "proration":
        if not prorationAnchorStart or not prorationAnchorEnd:
            raise ValueError(
                "prorationAnchorStart and prorationAnchorEnd are required "
                "for proration billing reason"
            )
        totalDays = (prorationAnchorEnd - prorationAnchorStart).days
        if totalDays <= 0:
            totalDays = 1
        remainingDays = (prorationAnchorEnd - now).days
        if remainingDays <= 0:
            remainingDays = 1
        dailyRate = basePrice / totalDays
        amountBeforeTax = int(round(domainCount * dailyRate * remainingDays))
        periodStart = now
        periodEnd = prorationAnchorEnd
    else:
        raise ValueError(f"Unsupported billing reason: {billingReason}")

    taxBreakdown = computeTax(
        amountBeforeTax=amountBeforeTax,
        customerState=customerState,
        invoiceTimestamp=now,
    )

    pricingVersion = f"v1_{planId}_{int(now.timestamp())}"

    snapshot = PricingSnapshot(
        billing_mode=billingMode,
        billing_reason=billingReason,
        domain_count=domainCount,
        base_price_per_domain=basePrice,
        amount_before_tax=amountBeforeTax,
        tax=taxBreakdown,
        total_amount=taxBreakdown.total_amount,
        currency="INR",
        pricing_version=pricingVersion,
        plan_id=planId,
        pricing_reference_snapshot_json=priceRef,
        period_start=periodStart.isoformat() if isinstance(periodStart, datetime) else str(periodStart),
        period_end=periodEnd.isoformat() if isinstance(periodEnd, datetime) else str(periodEnd),
    )

    logger.info(
        f"Invoice snapshot computed — mode={billingMode}, reason={billingReason}, "
        f"domains={domainCount}, base={amountBeforeTax}, tax={taxBreakdown.tax_amount}, "
        f"total={snapshot.total_amount}, version={pricingVersion}"
    )

    return snapshot


def computeTopupSnapshot(packId: str, billingMode: str,
                         customerState: str = None) -> PricingSnapshot:
    """
    Compute an invoice-ready pricing snapshot for a credit top-up pack.

    Unlike the subscription snapshots, the price comes from config/credits.json
    rather than a Razorpay reference plan — top-up packs are not Razorpay plans.
    PricingSnapshot is domain-shaped, so domain_count is 1 and
    base_price_per_domain is the pack price, read as "one pack".

    The pack's token count is frozen into pricing_reference_snapshot_json here,
    and grant_topup_tokens reads it back off the invoice at payment time. A
    later config change therefore cannot alter what an in-flight order grants.

    Args:
        packId: Pack key from config, e.g. 'medium'.
        billingMode: The subscriber's billing mode, recorded for reporting.
        customerState: Customer billing state code for tax determination.

    Returns:
        PricingSnapshot: Frozen, immutable pricing result.

    Raises:
        ValueError: If the pack is unknown or retired.
    """
    from api.services.credits.creditConfig import getTopupPack, CREDIT_CONFIG

    pack = getTopupPack(packId)
    if pack is None:
        raise ValueError(f"Unknown or inactive top-up pack: {packId}")

    now = datetime.now(timezone.utc)
    amountBeforeTax = pack["amount"]
    taxBreakdown = computeTax(
        amountBeforeTax=amountBeforeTax,
        customerState=customerState,
        invoiceTimestamp=now,
    )

    configVersion = CREDIT_CONFIG.get("version", "unknown")
    priceRef = {
        "source": "config_credits_json",
        "configVersion": configVersion,
        "packId": packId,
        "tokens": pack["tokens"],
        "amount": amountBeforeTax,
        "fetched_at": now.isoformat(),
    }
    pricingVersion = f"topup_v{configVersion}_{packId}_{int(now.timestamp())}"

    snapshot = PricingSnapshot(
        billing_mode=billingMode,
        billing_reason="add_on",
        domain_count=1,
        base_price_per_domain=amountBeforeTax,
        amount_before_tax=amountBeforeTax,
        tax=taxBreakdown,
        total_amount=taxBreakdown.total_amount,
        currency="INR",
        pricing_version=pricingVersion,
        plan_id=f"topup_{packId}",
        pricing_reference_snapshot_json=priceRef,
        period_start=now.isoformat(),
        period_end=now.isoformat(),
    )

    logger.info(
        f"Top-up snapshot computed — pack={packId}, tokens={pack['tokens']}, "
        f"base={amountBeforeTax}, tax={taxBreakdown.tax_amount}, "
        f"total={snapshot.total_amount}, version={pricingVersion}"
    )

    return snapshot
