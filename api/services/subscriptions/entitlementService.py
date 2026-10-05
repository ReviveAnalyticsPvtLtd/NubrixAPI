from __future__ import annotations

from dataclasses import dataclass

from api.services.subscriptions.paymentValidationService import (
    isAccessActive,
    isPeriodExpired,
    parseUtc,
)
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
    mapBillingModeToPlanType,
)
from utils.logger import logger


_PAID_PLAN_TYPES = {"pro", "annual"}
_VALID_PLAN_TYPES = {"none", "free", "pro", "annual"}
_TOPUP_ELIGIBLE_STATUSES = {"active", "renewal_upcoming", "payment_pending"}


class EntitlementUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubscriptionEntitlement:
    userId: str
    status: str
    planType: str
    currentPeriodEnd: str | None
    activeSubscription: bool
    trialOrAbove: bool
    paidPlan: bool
    topupEligible: bool


def evaluateSubscriptionEntitlement(
    userId: str,
    subscription: dict | None,
) -> SubscriptionEntitlement:
    row = subscription or {}
    status = str(row.get("status") or "none").strip().lower()
    billingMode = str(row.get("billing_mode") or "none").strip().lower()
    storedPlanType = str(row.get("plan_type") or "").strip().lower()
    planType = (
        storedPlanType
        if storedPlanType in _VALID_PLAN_TYPES
        else mapBillingModeToPlanType(billingMode, status)
    )
    currentPeriodEnd = row.get("current_period_end")

    periodEndValid = (
        parseUtc(currentPeriodEnd) is not None
        and not isPeriodExpired(row)
    )
    if status in {"active", "renewal_upcoming", "cancelled"}:
        activeSubscription = isAccessActive(row) and periodEndValid
    else:
        activeSubscription = status == "payment_pending"
    trialPeriodValid = (
        status == "trial"
        and periodEndValid
    )
    trialOrAbove = activeSubscription or trialPeriodValid
    paidPlan = activeSubscription and planType in _PAID_PLAN_TYPES
    topupEligible = (
        planType in _PAID_PLAN_TYPES
        and status in _TOPUP_ELIGIBLE_STATUSES
        and activeSubscription
    )

    return SubscriptionEntitlement(
        userId=userId,
        status=status,
        planType=planType,
        currentPeriodEnd=currentPeriodEnd,
        activeSubscription=activeSubscription,
        trialOrAbove=trialOrAbove,
        paidPlan=paidPlan,
        topupEligible=topupEligible,
    )


class SubscriptionEntitlementService:
    def __init__(self, dbClient) -> None:
        self.client = dbClient

    def get(self, userId: str) -> SubscriptionEntitlement:
        row = self._resolveCanonicalRow(userId)
        return evaluateSubscriptionEntitlement(userId, row)

    def _resolveCanonicalRow(self, userId: str) -> dict | None:
        """
        Load the user's authoritative subscription row.

        Selection is by the persisted ``is_canonical`` flag, never by
        ``updated_at`` ordering (a historical row updated later must not
        become canonical). Until operator-reviewed backfill has promoted
        canonical rows, a deterministic latest-row fallback keeps legacy
        accounts readable; the canonical flag wins whenever it exists.
        """
        try:
            canonicalRows = (
                self.client.table("subscriptions")
                .select(CANONICAL_SUBSCRIPTION_SELECT)
                .eq("user_id", userId)
                .eq("is_canonical", True)
                .limit(1)
                .execute()
                .data
            )
            if canonicalRows:
                return canonicalRows[0]
            legacyRows = (
                self.client.table("subscriptions")
                .select(CANONICAL_SUBSCRIPTION_SELECT)
                .eq("user_id", userId)
                .order("updated_at", desc=True)
                .order("id", desc=True)
                .limit(1)
                .execute()
                .data
            )
            return legacyRows[0] if legacyRows else None
        except Exception as exc:
            logger.error(
                "Entitlement lookup failed for userId={}: {}",
                userId,
                type(exc).__name__,
            )
            raise EntitlementUnavailableError(
                f"Entitlement lookup failed for userId={userId}"
            ) from exc
