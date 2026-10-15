"""Validated references from the central, published database price catalog."""
from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository


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


def getPlanPrice(currency: str, billingMode: str, *, catalog=None, connection=None) -> dict:
    mode = "monthly_prepaid" if billingMode == "monthly_recurring" else billingMode
    if currency not in ('INR', 'USD') or mode not in ('monthly_prepaid', 'annual_prepaid'):
        raise PriceUnavailable()
    catalog = catalog or getPricingCatalogRepository().loadCurrent(connection=connection)
    sku = 'monthly' if mode == 'monthly_prepaid' else 'annual'
    return validateReference({'amount':catalog.prices[currency][sku], 'currency':currency,
        'plan_id':f'catalog:{catalog.revision}:{sku}:{currency}', 'source':'pricing_catalog',
        'catalogRevision':catalog.revision, 'sku':sku,
        'fetched_at':catalog.publishedAt.isoformat()}, currency)
