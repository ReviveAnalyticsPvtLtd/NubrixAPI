"""
Apply pending entitlement removals only after the paid period boundary.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["EntitlementBoundaryTask"]


from api.services.billing.billingEventService import BillingEventService
from api.services.credits.creditService import creditService
from api.services.subscriptions.paymentValidationService import parseUtc, utcNow
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
    subscriptionExperts,
    subscriptionErasurePending,
    subscriptionPendingRemovals,
)
from api.commons import client as supabaseClient
from utils.logger import logger




class EntitlementBoundaryTask:
    """
    Applies pending_removals at the entitlement boundary, separate from
    renewal invoice pricing.
    """

    def __init__(self, client=None, now=None):
        self.client = client if client is not None else supabaseClient
        self._now = now or utcNow

    def execute(self) -> dict:
        logger.info("Entitlement boundary task started")
        subscriptions = (
            self.client.table("subscriptions")
            .select(CANONICAL_SUBSCRIPTION_SELECT)
            .eq("is_canonical", True)
            .in_("status", ["active", "cancelled", "renewal_upcoming", "payment_pending", "past_due", "suspended"])
            .execute()
            .data
        )

        applied = 0
        skipped = 0
        errors = 0
        now = self._now()

        for subscription in subscriptions or []:
            try:
                if subscriptionErasurePending(subscription):
                    skipped += 1
                    continue
                if subscription.get("billing_mode") == "monthly_prepaid":
                    from api.services.billing.manualBillingRepository import getManualBillingRepository
                    result = getManualBillingRepository().activateDueCoverage(subscription["user_id"], now)
                    applied += int(result.state in ("activated", "expired"))
                    skipped += int(result.state not in ("activated", "expired"))
                    continue
                pendingRemovals = subscriptionPendingRemovals(subscription)
                if not pendingRemovals:
                    skipped += 1
                    continue

                effectiveAt = self._resolveEffectiveAt(subscription)
                if not effectiveAt or effectiveAt > now:
                    skipped += 1
                    continue

                currentExperts = subscriptionExperts(subscription)
                updatedExperts = [expert for expert in currentExperts if expert not in set(pendingRemovals)]
                if updatedExperts == currentExperts:
                    self._clearPendingRemovals(subscription)
                    skipped += 1
                    continue

                self.client.table("subscriptions").update({
                    "subscribed_experts": updatedExperts,
                    "domain_count": len(updatedExperts),
                    "pending_removals": [],
                }).eq("id", subscription["id"]).execute()

                try:
                    creditService.applyDomainCountChange(
                        userId=subscription.get("user_id"),
                        domainCount=len(updatedExperts),
                        grantImmediately=False,
                    )
                except Exception as creditErr:
                    logger.warning(
                        f"Credit allowance update failed after removal for "
                        f"subscription {subscription.get('id')}: {creditErr}"
                    )

                BillingEventService(self.client).log_event(
                    user_id=subscription.get("user_id"),
                    subscription_id=subscription.get("id"),
                    event_type="subscription.pending_removals_applied",
                    event_status="APPLIED",
                    metadata={
                        "previousDomains": currentExperts,
                        "removedDomains": pendingRemovals,
                        "currentDomains": updatedExperts,
                        "effectiveAt": effectiveAt.isoformat(),
                    },
                )
                applied += 1
            except Exception as e:
                logger.error(
                    f"Entitlement boundary failed for subscription {subscription.get('id')}: {e}"
                )
                errors += 1

        logger.info(
            f"Entitlement boundary task completed: {applied} applied, {skipped} skipped, {errors} errors"
        )
        return {"applied": applied, "skipped": skipped, "errors": errors}

    def _resolveEffectiveAt(self, subscription: dict):
        invoiceRows = (
            self.client.table("Invoices")
            .select("id, period_start, metadata_json")
            .eq("subscription_id", subscription.get("id"))
            .eq("billing_reason", "renewal")
            .order("period_start", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if invoiceRows:
            metadata = invoiceRows[0].get("metadata_json") or {}
            metadataEffectiveAt = metadata.get("entitlementChangeEffectiveAt")
            parsedMetadataEffectiveAt = parseUtc(metadataEffectiveAt)
            if parsedMetadataEffectiveAt:
                return parsedMetadataEffectiveAt
            parsedPeriodStart = parseUtc(invoiceRows[0].get("period_start"))
            if parsedPeriodStart:
                return parsedPeriodStart
        return parseUtc(subscription.get("current_period_end"))

    def _clearPendingRemovals(self, subscription: dict) -> None:
        self.client.table("subscriptions").update({
            "pending_removals": [],
        }).eq("id", subscription["id"]).execute()
