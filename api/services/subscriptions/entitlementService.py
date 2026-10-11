from __future__ import annotations

from dataclasses import dataclass

from api.services.subscriptions.paymentValidationService import (
    isAccessActive,
    isPeriodExpired,
    utcNow,
    parseUtc,
)
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
    mapBillingModeToPlanType,
)
from utils.logger import logger


_PAID_PLAN_TYPES = {"pro", "annual"}
_VALID_PLAN_TYPES = {"none", "free", "pro", "annual"}
_TOPUP_ELIGIBLE_STATUSES = {"active", "renewal_upcoming", "payment_pending", "cancelled"}


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
    if status in {"active", "renewal_upcoming", "cancelled", "payment_pending"}:
        # Every paid phase — including payment_pending (an unpaid next-period
        # invoice) — requires timestamp-valid current coverage. Status alone
        # is never an access grant.
        activeSubscription = isAccessActive(row) and periodEndValid
    else:
        activeSubscription = False
    if billingMode == "monthly_prepaid":
        start = parseUtc(row.get("current_period_start"))
        activeSubscription = activeSubscription and start is not None and start <= utcNow()
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
        if row and row.get("billing_mode") == "monthly_prepaid":
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            try:
                coverage = getManualBillingRepository().getCoverageSnapshot(userId)
                row = self._resolveCanonicalRow(userId)
            except Exception as exc:
                raise EntitlementUnavailableError("Paid coverage activation unavailable") from exc
            if not row or str(row.get('id')) != coverage.subscriptionId:
                raise EntitlementUnavailableError("Canonical subscription changed during lookup")
            return SubscriptionEntitlement(
                userId=userId,
                status=str(row.get('status') or 'none').lower(),
                planType='pro' if coverage.accessAllowed else 'none',
                currentPeriodEnd=(coverage.currentPeriod.end.isoformat() if coverage.currentPeriod
                                  else row.get('current_period_end')),
                activeSubscription=coverage.accessAllowed,
                trialOrAbove=coverage.accessAllowed,
                paidPlan=coverage.accessAllowed,
                topupEligible=coverage.accessAllowed,
            )
        return evaluateSubscriptionEntitlement(userId, row)

    def _resolveCanonicalRow(self, userId: str) -> dict | None:
        """
        Load the user's authoritative subscription row.

        Selection is by the persisted ``is_canonical`` flag, never by
        ``updated_at`` ordering (a historical row updated later must not
        become canonical). Historical-only accounts require operator-reviewed
        backfill; guessing their current subscription would grant wrong access.
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
            legacyRows = self.client.table("subscriptions").select(CANONICAL_SUBSCRIPTION_SELECT).eq("user_id", userId).limit(1).execute().data
            if legacyRows:
                raise EntitlementUnavailableError("Canonical subscription backfill required")
            return None
        except Exception as exc:
            logger.error(
                "Entitlement lookup failed for userId={}: {}",
                userId,
                type(exc).__name__,
            )
            raise EntitlementUnavailableError(
                f"Entitlement lookup failed for userId={userId}"
            ) from exc
