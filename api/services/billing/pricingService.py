"""Read-only price catalogue. Public amounts never authorize client totals."""
from datetime import datetime, timezone, timedelta
import os

from jose import jwt

from api.services.billing.priceCatalog import getPlanPrice, PriceUnavailable
from api.services.billing.pricingContext import resolveCurrency, issuePricingContext, verifyPricingContext


class PricingService:
    def __init__(self, client=None, priceLoader=None):
        self._client = client
        self.priceLoader = priceLoader or getPlanPrice

    @property
    def client(self):
        if self._client is None:
            from api.commons import client
            self._client = client
        return self._client

    def accountFacts(self, token):
        # Caller has verified the session through verifyToken before this read.
        identity = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        userId = str(identity["userId"])
        users = self.client.table("Users").select("country").eq("userId", userId).limit(1).execute().data
        subscriptions = self.client.table("subscriptions").select("id, default_currency").eq(
            "user_id", userId).eq("is_canonical", True).limit(1).execute().data
        paidCurrency = None
        if subscriptions:
            paid = self.client.table("Invoices").select("currency").eq("userId", userId).eq(
                "subscription_id", subscriptions[0]["id"]).eq("status", "PAID").execute().data
            currencies = {row.get("currency") or "INR" for row in paid}
            if len(currencies) > 1:
                raise ValueError("BILLING_CURRENCY_CONFLICT")
            if currencies:
                paidCurrency = currencies.pop()
        return userId, users[0].get("country") if users else None, paidCurrency

    def getCatalog(self, scope, hostCountry, token=None):
        if scope not in {"visitor", "account"}:
            raise ValueError("PRICING_SCOPE_INVALID")
        subject, profileCountry, paidCurrency = None, None, None
        if scope == "account":
            if not token:
                raise ValueError("PRICING_AUTH_REQUIRED")
            subject, profileCountry, paidCurrency = self.accountFacts(token)
        currency, source = resolveCurrency(hostCountry, profileCountry, paidCurrency)
        result = {"schemaVersion": 1, "currency": currency, "countrySource": source, "subject": subject}
        for cycle, mode in (("monthly", "monthly_prepaid"), ("annual", "annual_prepaid")):
            try:
                reference = self.priceLoader(currency, mode)
                result[cycle] = {"available": True, "amountMinor": reference["amount"]}
            except PriceUnavailable:
                result[cycle] = {"available": False, "reason": "PRICE_UNAVAILABLE"}
        now = datetime.now(timezone.utc)
        result["pricingContext"] = issuePricingContext(subject, currency, source, now=now) if subject else None
        result["expiresAt"] = (now + timedelta(seconds=600)).isoformat() if subject else None
        return result

    def preview(self, token, domains, billingMode, pricingContext):
        """Calculate a display total; never reserve an invoice or provider order."""
        from api.services.billing.billingEngine import computeInvoiceSnapshot
        identity = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        userId = str(identity["userId"])
        claims = verifyPricingContext(pricingContext, userId)
        now = datetime.now(timezone.utc)
        currency = claims["currency"]
        snapshot = computeInvoiceSnapshot(
            billingMode=billingMode, billingReason="initial_purchase", domainCount=len(domains),
            priceReference=self.priceLoader(currency, billingMode), evaluatedAt=now, currency=currency)
        taxes = [{"label": label, "amount": getattr(snapshot.tax, key)}
                 for key, label in (("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST"), ("cess", "Cess"))
                 if getattr(snapshot.tax, key)]
        return {"schemaVersion": 1, "subject": userId, "domains": sorted(domains), "billingMode": billingMode,
                "expiresAt": min(datetime.fromtimestamp(claims["exp"], timezone.utc),
                                 now + timedelta(minutes=5)).isoformat(),
                "breakdown": {"currency": currency, "subtotal": snapshot.amount_before_tax,
                              "tax": snapshot.tax.tax_amount, "total": snapshot.total_amount, "taxes": taxes}}
