"""Read-only price catalogue. Public amounts never authorize client totals."""
from datetime import datetime, timezone, timedelta
import os
import psycopg2

from jose import jwt

from api.services.billing.priceCatalog import getPlanPrice, PriceUnavailable
from api.services.billing.pricingContext import resolveCurrency, issuePricingContext, verifyPricingContext
from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository
from api.services.billing.pricingCatalogContracts import CatalogUnavailable


class PricingService:
    def __init__(self, priceLoader=None, catalogRepository=None, accountRepository=None):
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        self.priceLoader = priceLoader or getPlanPrice
        self.catalogRepository = catalogRepository or getPricingCatalogRepository()
        self.accountRepository = accountRepository or getManualBillingRepository()

    def accountFacts(self, token):
        # Caller has verified the session through verifyToken before this read.
        identity = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        userId = str(identity["userId"])
        try:
            paidCurrency = self.accountRepository.paidSubscriptionCurrency(userId)
        except (psycopg2.Error, OSError, RuntimeError) as exc:
            raise CatalogUnavailable('Account pricing is temporarily unavailable') from exc
        # Self-reported country is not an input to regional price authorization.
        return userId, None, paidCurrency

    def getCatalog(self, scope, hostCountry, token=None):
        if scope not in {"visitor", "account"}:
            raise ValueError("PRICING_SCOPE_INVALID")
        subject, profileCountry, paidCurrency = None, None, None
        if scope == "account":
            if not token:
                raise ValueError("PRICING_AUTH_REQUIRED")
            subject, profileCountry, paidCurrency = self.accountFacts(token)
        currency, source = resolveCurrency(hostCountry, profileCountry, paidCurrency)
        catalog = self.catalogRepository.loadCurrent()
        result = {"schemaVersion": 1, "currency": currency, "countrySource": source, "subject": subject,
                  'catalogRevision':catalog.revision}
        for cycle, mode in (("monthly", "monthly_prepaid"), ("annual", "annual_prepaid")):
            try:
                reference = self.priceLoader(currency, mode, catalog=catalog)
                result[cycle] = {"available": True, "amountMinor": reference["amount"]}
            except PriceUnavailable:
                result[cycle] = {"available": False, "reason": "PRICE_UNAVAILABLE"}
        now = datetime.now(timezone.utc)
        result["pricingContext"] = issuePricingContext(subject, currency, source, now=now, catalogRevision=catalog.revision) if subject else None
        result["expiresAt"] = (now + timedelta(seconds=600)).isoformat() if subject else None
        return result

    def preview(self, token, domains, billingMode, pricingContext, expectedCatalogRevision=None):
        """Calculate a display total; never reserve an invoice or provider order."""
        from api.services.billing.billingEngine import computeInvoiceSnapshot
        identity = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        userId = str(identity["userId"])
        claims = verifyPricingContext(pricingContext, userId)
        catalog = self.catalogRepository.loadCurrent()
        if claims.get('catalogRevision') != catalog.revision or (expectedCatalogRevision is not None and expectedCatalogRevision != catalog.revision):
            raise ValueError('PRICING_CHANGED')
        now = datetime.now(timezone.utc)
        currency = claims["currency"]
        snapshot = computeInvoiceSnapshot(
            billingMode=billingMode, billingReason="initial_purchase", domainCount=len(domains),
            priceReference=self.priceLoader(currency, billingMode, catalog=catalog), evaluatedAt=now, currency=currency)
        taxes = [{"label": label, "amount": getattr(snapshot.tax, key)}
                 for key, label in (("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST"), ("cess", "Cess"))
                 if getattr(snapshot.tax, key)]
        return {"schemaVersion": 1, "subject": userId, "domains": sorted(domains), "billingMode": billingMode,
                'catalogRevision':catalog.revision,
                "expiresAt": min(datetime.fromtimestamp(claims["exp"], timezone.utc),
                                 now + timedelta(minutes=5)).isoformat(),
                "breakdown": {"currency": currency, "subtotal": snapshot.amount_before_tax,
                              "tax": snapshot.tax.tax_amount, "total": snapshot.total_amount, "taxes": taxes}}
