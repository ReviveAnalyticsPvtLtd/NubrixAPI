"""
billingEngine.py

Centralized backend price and tax computation for all billing flows.

Reads published plan and credit prices from the central database catalog.
Tax, proration and immutable invoice snapshots use server-owned references.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["computeInvoiceSnapshot", "computeTopupSnapshot"]


from api.services.billing.taxEngine import computeTax, TaxBreakdown
from utils.logger import logger
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta


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
        base_price_per_domain: Per-domain price in the currency's minor units.
        amount_before_tax: Total taxable base (domains * price * factor).
        tax: The full TaxBreakdown object.
        total_amount: Final payable amount in paise.
        currency: ISO currency code.
        pricing_version: Snapshot identifier for audit trail.
        plan_id: Catalog reference ID; historical snapshots may hold provider IDs.
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


def _getMonthlyBasePrice(currency: str = "INR") -> dict:
    """
    Read the monthly per-domain price from the published catalog.

    Returns:
        dict: Monthly plan metadata snapshot.
    """
    from api.services.billing.priceCatalog import getPlanPrice
    return getPlanPrice(currency, "monthly_prepaid")


def _getAnnualBasePrice(currency: str = "INR") -> dict:
    """
    Read the annual per-domain price from the published catalog.

    Returns:
        dict: Annual plan metadata snapshot.
    """
    from api.services.billing.priceCatalog import getPlanPrice
    return getPlanPrice(currency, "annual_prepaid")


def computeInvoiceSnapshot(billingMode: str, billingReason: str,
                           domainCount: int, customerState: str = None,
                           periodStart: datetime = None,
                           periodEnd: datetime = None,
                           prorationAnchorStart: datetime = None,
                           prorationAnchorEnd: datetime = None,
                           priceReference: dict = None,
                           evaluatedAt: datetime = None,
                           currency: str = "INR") -> PricingSnapshot:
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
    now = evaluatedAt or datetime.now(timezone.utc)

    if billingMode in ("monthly_prepaid", "monthly_recurring"):
        priceRef = priceReference or (_getMonthlyBasePrice() if currency == "INR" else _getMonthlyBasePrice(currency))
        basePrice = priceRef["amount"]
        if periodStart is None:
            periodStart = now
        if periodEnd is None:
            periodEnd = periodStart + relativedelta(months=1)
    elif billingMode == "annual_prepaid":
        priceRef = priceReference or (_getAnnualBasePrice() if currency == "INR" else _getAnnualBasePrice(currency))
        basePrice = priceRef["amount"]
        if periodStart is None:
            periodStart = now
        if periodEnd is None:
            periodEnd = periodStart + relativedelta(years=1)
    else:
        raise ValueError(f"Unsupported billing mode: {billingMode}")

    from api.services.billing.priceCatalog import validateReference
    validateReference(priceRef, currency)
    planId = priceRef.get("plan_id") or "legacy_reference"
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

    _validateTotals(amountBeforeTax, taxBreakdown)
    pricingVersion = f"v1_{planId}_{int(now.timestamp())}"

    snapshot = PricingSnapshot(
        billing_mode=billingMode,
        billing_reason=billingReason,
        domain_count=domainCount,
        base_price_per_domain=basePrice,
        amount_before_tax=amountBeforeTax,
        tax=taxBreakdown,
        total_amount=taxBreakdown.total_amount,
        currency=currency,
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
                         customerState: str = None, *, currency: str = "INR", priceReference: dict = None) -> PricingSnapshot:
    """
    Compute an invoice-ready pricing snapshot for a credit top-up pack.

    The published catalog supplies the amount; credit configuration supplies
    the pack quantity. Neither field is read from provider reference plans.
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
    from api.services.credits.creditConfig import getTopupPack

    pack = priceReference or getTopupPack(packId, currency)
    if pack is None:
        raise ValueError(f"Unknown or inactive top-up pack: {packId}")

    now = datetime.now(timezone.utc)
    amountBeforeTax = pack["amount"]
    taxBreakdown = computeTax(
        amountBeforeTax=amountBeforeTax,
        customerState=customerState,
        invoiceTimestamp=now,
    )

    from api.services.billing.priceCatalog import validateReference
    validateReference(pack, currency)
    _validateTotals(amountBeforeTax, taxBreakdown)
    priceRef = {key: pack[key] for key in ('source', 'catalogRevision', 'tokens', 'amount', 'currency') if key in pack}
    priceRef['packId'] = packId
    priceRef['fetched_at'] = now.isoformat()
    pricingVersion = f"topup_catalog_{pack.get('catalogRevision', 'legacy')}_{packId}_{int(now.timestamp())}"

    snapshot = PricingSnapshot(
        billing_mode=billingMode,
        billing_reason="add_on",
        domain_count=1,
        base_price_per_domain=amountBeforeTax,
        amount_before_tax=amountBeforeTax,
        tax=taxBreakdown,
        total_amount=taxBreakdown.total_amount,
        currency=currency,
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


def _validateTotals(subtotal, tax):
    if any(type(value) is not int or not 0 <= value <= 9007199254740991
           for value in (subtotal, tax.tax_amount, tax.total_amount)):
        raise ValueError('INVALID_INVOICE_AMOUNT')
