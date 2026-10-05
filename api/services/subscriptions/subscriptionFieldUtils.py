"""
Shared subscription package helpers for billing and entitlement fields.

The MVP billing cutover keeps identity/profile data on ``Users`` and moves
plan lifecycle, entitlement, pending changes, and Razorpay provider state to
``subscriptions``. These helpers keep the storage shape consistent across API
services, webhooks, and schedulers.
"""

from __future__ import annotations

import datetime

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = [
    "SUBSCRIPTION_BILLING_FIELDS_SELECT",
    "CANONICAL_SUBSCRIPTION_SELECT",
    "normalizeDomainList",
    "subscriptionExperts",
    "subscriptionPendingRemovals",
    "subscriptionPendingAdditions",
    "subscriptionRenewalExperts",
    "subscriptionRenewalDomainCount",
    "buildRenewalPricingMetadata",
    "subscriptionDomainCount",
    "subscriptionBillingState",
    "subscriptionErasurePending",
    "toSubscriptionBillingPayload",
    "toApiPlanFields",
    "mapBillingModeToPlanType",
    "buildChurnResetPayload",
]


SUBSCRIPTION_BILLING_FIELDS_SELECT = (
    "subscribed_experts, domain_count, pending_removals, pending_additions, "
    "billing_state, cancellation_reason"
)

CANONICAL_SUBSCRIPTION_SELECT = (
    "id, user_id, billing_mode, status, plan_type, current_period_start, current_period_end, "
    "renewal_due_at, auto_renew_enabled, payment_collection_mode, "
    "default_currency, version, erasure_pending, "
    "is_canonical, renewal_opt_out, "
    f"{SUBSCRIPTION_BILLING_FIELDS_SELECT}"
)


def normalizeDomainList(value) -> list[str]:
    """
    Normalize DB/API domain list shapes into a clean list of strings.

    Historical data may be comma-separated text; new storage is JSONB array.
    This function is used during migration/cutover logic, not as a fallback to
    the old Users columns.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def subscriptionExperts(subscription: dict | None) -> list[str]:
    return normalizeDomainList((subscription or {}).get("subscribed_experts"))


def subscriptionPendingRemovals(subscription: dict | None) -> list[str]:
    return normalizeDomainList((subscription or {}).get("pending_removals"))


def subscriptionPendingAdditions(subscription: dict | None) -> list[dict]:
    value = (subscription or {}).get("pending_additions")
    return value if isinstance(value, list) else []


def subscriptionRenewalExperts(subscription: dict | None) -> list[str]:
    currentExperts = subscriptionExperts(subscription)
    pendingRemovals = set(subscriptionPendingRemovals(subscription))
    return [expert for expert in currentExperts if expert not in pendingRemovals]


def subscriptionRenewalDomainCount(subscription: dict | None) -> int:
    return len(subscriptionRenewalExperts(subscription))


def buildRenewalPricingMetadata(subscription: dict | None, effectiveAt) -> dict:
    currentDomains = subscriptionExperts(subscription)
    renewalDomains = subscriptionRenewalExperts(subscription)
    pendingApplied = [domain for domain in subscriptionPendingRemovals(subscription) if domain in currentDomains]
    return {
        "currentDomains": currentDomains,
        "renewalDomains": renewalDomains,
        "pendingRemovalsAppliedForPricing": pendingApplied,
        "entitlementChangeEffectiveAt": str(effectiveAt) if effectiveAt is not None else None,
        "renewalDomainCount": len(renewalDomains),
    }


def subscriptionDomainCount(subscription: dict | None) -> int:
    subscription = subscription or {}
    count = subscription.get("domain_count")
    if count is None:
        return len(subscriptionExperts(subscription))
    try:
        return int(count)
    except (TypeError, ValueError):
        return len(subscriptionExperts(subscription))


def subscriptionBillingState(subscription: dict | None):
    return (subscription or {}).get("billing_state") or {}


def subscriptionErasurePending(subscription: dict | None) -> bool:
    value = (subscription or {}).get("erasure_pending", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def toSubscriptionBillingPayload(
    *,
    subscribedExperts=None,
    domainCount=None,
    pendingRemovals=None,
    pendingAdditions=None,
    billingState=None,
    cancellationReason=None,
) -> dict:
    """
    Convert camelCase service arguments into subscription table column names.
    Values left as ``None`` are omitted so callers can do partial updates.
    The contracted recurring-provider columns (customer/token/anchor/failures)
    are no longer writable through this helper.
    """
    payload = {}
    if subscribedExperts is not None:
        payload["subscribed_experts"] = normalizeDomainList(subscribedExperts)
    if domainCount is not None:
        payload["domain_count"] = int(domainCount)
    if pendingRemovals is not None:
        payload["pending_removals"] = normalizeDomainList(pendingRemovals)
    if pendingAdditions is not None:
        payload["pending_additions"] = pendingAdditions if isinstance(pendingAdditions, list) else []
    if billingState is not None:
        payload["billing_state"] = billingState or {}
    if cancellationReason is not None:
        payload["cancellation_reason"] = cancellationReason
    return payload


def toApiPlanFields(subscription: dict | None) -> dict:
    """
    Preserve frontend response shape while sourcing values from subscriptions.
    """
    return {
        "subscribedExperts": subscriptionExperts(subscription),
        "domainCount": subscriptionDomainCount(subscription),
        "pendingRemovals": subscriptionPendingRemovals(subscription),
        "planType": (subscription or {}).get("plan_type") or mapBillingModeToPlanType(
            (subscription or {}).get("billing_mode"),
            (subscription or {}).get("status"),
        ),
    }


def mapBillingModeToPlanType(billingMode: str | None, status: str | None = None) -> str:
    """
    Derive API plan tier labels from canonical subscription billing_mode and status.

    Paid tiers are resolved from billing_mode first so lifecycle status alone cannot
    collapse a paid row to ``none`` — EXCEPT the terminal expired/reset statuses,
    where a historical billing mode must not force an active pro tier. No-plan rows
    use status to distinguish trial, expired trial, and brand-new users.
    """
    normalizedStatus = (status or "").strip().lower()
    normalizedBillingMode = (billingMode or "").strip().lower()

    isPaidMode = normalizedBillingMode in (
        "monthly_prepaid",
        "monthly_recurring",
        "annual_prepaid",
    )
    if isPaidMode and normalizedStatus == "expired":
        # A reset monthly/annual row is none regardless of its billing-mode
        # history; historical mode must not force an active paid tier.
        return "none"
    if normalizedBillingMode == "monthly_prepaid":
        return "pro"
    if normalizedBillingMode == "monthly_recurring":
        return "pro"
    if normalizedBillingMode == "annual_prepaid":
        return "annual"
    if normalizedStatus == "trial":
        return "free"
    if normalizedStatus == "expired":
        return "free"
    return "none"


def buildChurnResetPayload(
    subscription: dict,
    churn_reason: str,
    now: datetime.datetime | None = None,
    override_status: str | None = None,
) -> dict | None:
    """
    Build a partial-reset payload for a churned subscription.

    Preserves ``billing_mode``, ``razorpay_customer_id``, ``subscribed_experts``,
    ``domain_count``, and identity fields while clearing period dates, pending
    changes, and stale payment tokens.  Domain fields are intentionally kept so
    expired users retain access to their project data in the frontend.
    Archives previous values in ``billing_state.churn_snapshot`` so support can
    see what was active before churn.

    Returns ``None`` when the row is already in a reset state (idempotent guard).
    """
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)

    current_status = (subscription.get("status") or "").lower()
    experts = subscriptionExperts(subscription)
    period_end = subscription.get("current_period_end")

    already_reset = (
        period_end is None
        and current_status == "expired"
        and override_status in (None, "expired")
    )
    if already_reset:
        return None

    existing_billing_state = dict(subscription.get("billing_state") or {})
    existing_billing_state["churn_snapshot"] = {
        "churned_at": current.isoformat(),
        "churn_reason": churn_reason,
        "previous_period_start": subscription.get("current_period_start"),
        "previous_period_end": period_end,
        "previous_subscribed_experts": experts,
        "previous_renewal_due_at": subscription.get("renewal_due_at"),
    }
    existing_billing_state["lifecycle_snapshot"] = {
        "subscription_days_left": 0,
        "calculated_at": current.isoformat(),
        "current_period_end": None,
        "status": "expired",
    }

    payload: dict = {
        "current_period_start": None,
        "current_period_end": None,
        "renewal_due_at": None,
        "pending_removals": [],
        "pending_additions": [],
        "auto_renew_enabled": False,
        "payment_collection_mode": "authenticated_checkout",
        "cancellation_reason": None,
        "billing_state": existing_billing_state,
    }

    if override_status:
        payload["status"] = override_status
        payload["plan_type"] = mapBillingModeToPlanType(
            subscription.get("billing_mode"), override_status,
        )

    return payload
