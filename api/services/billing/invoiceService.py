"""
invoiceService.py

Service layer for the annual renewal invoice lifecycle.

Provides idempotent helpers for:
    - Creating draft/upcoming renewal invoices (T-30).
    - Freezing final pricing for dashboard Checkout payment (T-7).
    - Transitioning invoice and subscription states through the
      payment_pending → paid / past_due → suspended lifecycle.

All mutations are guarded by DB uniqueness constraints and Redis
advisory locks to ensure idempotent scheduler reruns.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = [
    "createUpcomingRenewalInvoice",
    "prepareDashboardRenewalInvoice",
    "buildDashboardRenewalUrl",
    "transitionToPastDue",
    "transitionToSuspended",
]


from api.services.billing.billingEngine import computeInvoiceSnapshot
from api.services.subscriptions.subscriptionFieldUtils import (
    SUBSCRIPTION_BILLING_FIELDS_SELECT,
    buildRenewalPricingMetadata,
    subscriptionBillingState,
    subscriptionRenewalDomainCount,
)
from api.services.subscriptions.paymentValidationService import normalizeChurnedSubscription
from api.commons import client
from utils.logger import logger
from dateutil.relativedelta import relativedelta
import datetime
from api.services.billing.billingClient import getBillingRedisClient
import redis
import os


_RECONCILIATION_BUFFER_MINUTES = 60






def _acquireAdvisoryLock(redisClient: redis.Redis, lockKey: str,
                         ttlSeconds: int = 300) -> bool:
    """
    Acquire a Redis advisory lock for idempotent scheduler operations.

    Args:
        redisClient: Redis client instance.
        lockKey: Unique lock key.
        ttlSeconds: Lock TTL in seconds.

    Returns:
        bool: True if lock acquired, False if already held.
    """
    return bool(redisClient.set(lockKey, "1", nx=True, ex=ttlSeconds))


def createUpcomingRenewalInvoice(subscription: dict, user: dict) -> dict | None:
    """
    Create or return an existing upcoming renewal invoice for an
    annual subscription approaching its renewal window (T-30).

    Uses the DB unique index idx_invoices_renewal_period_unique to
    prevent duplicate invoices for the same subscription period.

    The pricing snapshot is estimated at this stage and will be
    recomputed and frozen at T-7 before app Checkout payment.

    Args:
        subscription: Row from the subscriptions table.
        user: Row from the Users table (identity/profile fields).

    Returns:
        dict | None: The invoice row if created/found, None on error.
    """
    redisClient = getBillingRedisClient()
    subscriptionId = subscription["id"]
    userId = subscription["user_id"]
    periodEnd = subscription["current_period_end"]
    nextPeriodStart = periodEnd
    nextPeriodEnd = None

    try:
        from dateutil import parser as dtparser
        periodEndDt = dtparser.isoparse(periodEnd)
        if periodEndDt.tzinfo is None:
            periodEndDt = periodEndDt.replace(
                tzinfo=datetime.timezone.utc
            )
        nextPeriodEndDt = periodEndDt + relativedelta(years=1)
        nextPeriodEnd = nextPeriodEndDt.isoformat()
    except Exception:
        nextPeriodEnd = nextPeriodStart

    existing = (
        client.table("Invoices")
        .select("id, status, total_amount")
        .eq("subscription_id", subscriptionId)
        .eq("billing_reason", "renewal")
        .eq("period_start", nextPeriodStart)
        .eq("period_end", nextPeriodEnd)
        .in_("status", ["UPCOMING", "PAYMENT_PENDING", "PAID", "EXPIRED", "upcoming", "payment_pending", "paid", "expired"])
        .limit(1)
        .execute()
        .data
    )
    if existing:
        logger.info(
            f"Renewal invoice already exists for subscription {subscriptionId}"
        )
        return existing[0]

    lockKey = f"invoice:renewal:{subscriptionId}:{nextPeriodStart}:{nextPeriodEnd}"
    if not _acquireAdvisoryLock(redisClient, lockKey, ttlSeconds=120):
        logger.info(
            f"Renewal invoice lock held for subscription {subscriptionId}, skipping"
        )
        return None

    domainCount = subscriptionRenewalDomainCount(subscription)
    if domainCount < 1:
        logger.warning(
            f"Subscription {subscriptionId} has 0 domains, skipping invoice creation"
        )
        return None

    try:
        snapshot = computeInvoiceSnapshot(
            billingMode="annual_prepaid",
            billingReason="renewal",
            domainCount=domainCount,
            customerState=subscriptionBillingState(subscription).get('customerState'),
            periodStart=dtparser.isoparse(nextPeriodStart) if isinstance(nextPeriodStart, str) else nextPeriodStart,
            periodEnd=nextPeriodEndDt,
        )
    except Exception as e:
        logger.error(
            f"Failed to compute renewal snapshot for subscription {subscriptionId}: {e}"
        )
        return None

    dueDate = periodEndDt.isoformat()
    renewalMetadata = buildRenewalPricingMetadata(subscription, dueDate)

    payload = {
        "userId": userId,
        "subscription_id": subscriptionId,
        "billing_reason": "renewal",
        "payment_flow": "razorpay_order_checkout",
        "requires_customer_auth": True,
        "period_start": snapshot.period_start,
        "period_end": snapshot.period_end,
        "amount_before_tax": snapshot.amount_before_tax,
        "tax_amount": snapshot.tax.tax_amount,
        "total_amount": snapshot.total_amount,
        "amount": snapshot.total_amount,
        "currency": snapshot.currency,
        "status": "UPCOMING",
        "due_date": dueDate,
        "tax_breakdown_json": snapshot.tax.to_dict(),
        "tax_rule_version": snapshot.tax.tax_rule_version,
        "place_of_supply_snapshot": snapshot.tax.place_of_supply_snapshot,
        "pricing_version": snapshot.pricing_version,
        "pricing_reference_snapshot_json": snapshot.pricing_reference_snapshot_json,
        "metadata_json": {
            "flow": "annual_renewal_scheduler_t30",
            "billingMode": "annual_prepaid",
            "estimate": True,
            **renewalMetadata,
        },
    }

    try:
        result = client.table("Invoices").insert(payload).execute()
        if result.data:
            logger.info(
                f"Renewal invoice created for subscription {subscriptionId}, "
                f"invoiceId={result.data[0]['id']}"
            )
            return result.data[0]
    except Exception as e:
        errorStr = str(e).lower()
        if "duplicate" in errorStr or "unique" in errorStr or "23505" in errorStr:
            logger.info(
                f"Duplicate renewal invoice blocked by DB for subscription {subscriptionId}"
            )
            return (
                client.table("Invoices")
                .select("id, status, total_amount")
                .eq("subscription_id", subscriptionId)
                .eq("billing_reason", "renewal")
                .eq("period_start", nextPeriodStart)
                .eq("period_end", nextPeriodEnd)
                .in_("status", ["UPCOMING", "PAYMENT_PENDING", "PAID", "EXPIRED", "upcoming", "payment_pending", "paid", "expired"])
                .limit(1)
                .execute()
                .data or [None]
            )[0]
        logger.error(
            f"Failed to insert renewal invoice for subscription {subscriptionId}: {e}"
        )
    return None


def buildDashboardRenewalUrl(invoiceId: str) -> str:
    """
    Build the app billing URL used as the customer CTA for annual renewals.
    """
    dashboardBaseUrl = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    renewalPath = f"/settings/billing-details?renewalInvoiceId={invoiceId}"
    return f"{dashboardBaseUrl}{renewalPath}" if dashboardBaseUrl else renewalPath


def prepareDashboardRenewalInvoice(invoice: dict) -> dict | None:
    """
    Recompute final pricing and freeze a renewal invoice for app-dashboard
    Razorpay Order Checkout. This does not create provider invoices or payment
    links.

    Args:
        invoice: The upcoming/payment_pending invoice row.

    Returns:
        dict | None: Updated invoice row prepared for dashboard payment, or
        None on error.
    """
    redisClient = getBillingRedisClient()
    invoiceId = invoice["id"]

    lockKey = f"invoice:dashboard-renewal:{invoiceId}"
    if not _acquireAdvisoryLock(redisClient, lockKey, ttlSeconds=180):
        logger.info(f"Dashboard renewal prep lock held for invoice {invoiceId}, skipping")
        return None

    subscriptionId = invoice.get("subscription_id")
    subscription = (
        client.table("subscriptions")
        .select(f"id, current_period_end, billing_mode, status, {SUBSCRIPTION_BILLING_FIELDS_SELECT}")
        .eq("id", subscriptionId)
        .limit(1)
        .execute()
        .data
    )
    if not subscription:
        logger.error(f"Subscription {subscriptionId} not found for invoice {invoiceId}")
        return None

    subscription = subscription[0]
    domainCount = subscriptionRenewalDomainCount(subscription)
    if domainCount < 1:
        logger.warning(f"User has 0 domains for invoice {invoiceId}, skipping dashboard prep")
        return None

    try:
        from dateutil import parser as dtparser
        periodStartDt = dtparser.isoparse(invoice["period_start"])
        periodEndDt = dtparser.isoparse(invoice["period_end"])
        snapshot = computeInvoiceSnapshot(
            billingMode="annual_prepaid",
            billingReason="renewal",
            domainCount=domainCount,
            customerState=subscriptionBillingState(subscription).get('customerState'),
            periodStart=periodStartDt,
            periodEnd=periodEndDt,
        )
    except Exception as e:
        logger.error(
            f"Failed to recompute dashboard renewal snapshot for invoice {invoiceId}: {e}"
        )
        return None

    existingMetadata = invoice.get("metadata_json") if isinstance(invoice.get("metadata_json"), dict) else {}
    renewalMetadata = buildRenewalPricingMetadata(subscription, invoice.get("period_start"))
    dashboardRenewalUrl = buildDashboardRenewalUrl(str(invoiceId))
    metadata = {
        **existingMetadata,
        **renewalMetadata,
        "flow": "annual_renewal_scheduler_t7",
        "billingMode": "annual_prepaid",
        "estimate": False,
        "frozen": True,
        "paymentFlow": "razorpay_order_checkout",
        "dashboardRenewalUrl": dashboardRenewalUrl,
    }
    if existingMetadata.get('repricingRequired'):
        manual = dict(existingMetadata.get('manualBilling') or {})
        for key in ('closedAt', 'closedReason'):
            manual.pop(key, None)
        manual.update(domains=renewalMetadata['renewalDomains'], revision=int(manual.get('revision') or 1) + 1)
        metadata.update(manualBilling=manual, repricingRequired=False)
    updatePayload = {
        "payment_flow": "razorpay_order_checkout",
        "requires_customer_auth": True,
        "status": "PAYMENT_PENDING",
        "amount_before_tax": snapshot.amount_before_tax,
        "tax_amount": snapshot.tax.tax_amount,
        "total_amount": snapshot.total_amount,
        "amount": snapshot.total_amount,
        "tax_breakdown_json": snapshot.tax.to_dict(),
        "tax_rule_version": snapshot.tax.tax_rule_version,
        "place_of_supply_snapshot": snapshot.tax.place_of_supply_snapshot,
        "pricing_version": snapshot.pricing_version,
        "pricing_reference_snapshot_json": snapshot.pricing_reference_snapshot_json,
        "metadata_json": metadata,
    }
    client.table("Invoices").update(updatePayload).eq("id", invoiceId).execute()

    logger.info(
        f"Dashboard renewal invoice prepared for invoice {invoiceId}, "
        f"payment_flow=razorpay_order_checkout"
    )
    return {**invoice, **updatePayload}


def transitionToPastDue(subscription: dict, invoice: dict) -> bool:
    """
    Transition a subscription to past_due when its renewal invoice
    remains unpaid after the billing period has ended.

    Checks invoice status before transitioning to guard against
    race conditions with concurrent webhook-paid events.

    Args:
        subscription: Row from the subscriptions table.
        invoice: The unpaid renewal invoice row.

    Returns:
        bool: True if transition occurred, False if skipped/blocked.
    """
    invoiceId = invoice["id"]
    subscriptionId = subscription["id"]

    freshInvoice = (
        client.table("Invoices")
        .select("status")
        .eq("id", invoiceId)
        .limit(1)
        .execute()
        .data
    )
    if freshInvoice and freshInvoice[0].get("status", "").lower() in ("paid", "void"):
        logger.info(
            f"Invoice {invoiceId} already resolved, skipping past_due transition"
        )
        return False

    currentStatus = subscription.get("status", "")
    if currentStatus in ("past_due", "suspended", "cancelled", "expired"):
        logger.info(
            f"Subscription {subscriptionId} already in terminal/past_due state, skipping"
        )
        return False

    client.table("subscriptions").update({
        "status": "past_due",
    }).eq("id", subscriptionId).execute()

    logger.info(
        f"Subscription {subscriptionId} transitioned to past_due"
    )
    return True


def transitionToSuspended(subscription: dict, invoice: dict) -> bool:
    """
    Transition a past_due subscription to suspended after the
    reconciliation buffer window has elapsed.

    Performs a final invoice status check to handle payment arrival
    race conditions.

    Args:
        subscription: Row from the subscriptions table.
        invoice: The unpaid renewal invoice row.

    Returns:
        bool: True if suspension occurred, False if skipped/blocked.
    """
    invoiceId = invoice["id"]
    subscriptionId = subscription["id"]

    freshInvoice = (
        client.table("Invoices")
        .select("status")
        .eq("id", invoiceId)
        .limit(1)
        .execute()
        .data
    )
    if freshInvoice and freshInvoice[0].get("status", "").lower() in ("paid", "void"):
        logger.info(
            f"Invoice {invoiceId} paid during buffer window, cancelling suspension"
        )
        return False

    currentStatus = subscription.get("status", "")
    if currentStatus != "past_due":
        logger.info(
            f"Subscription {subscriptionId} not in past_due state ({currentStatus}), "
            f"skipping suspension"
        )
        return False

    client.table("subscriptions").update({
        "status": "suspended",
    }).eq("id", subscriptionId).execute()

    subscription["status"] = "suspended"
    normalizeChurnedSubscription(
        client, subscription, "annual_renewal_unpaid",
        override_status="expired",
    )

    logger.info(
        f"Subscription {subscriptionId} suspended due to non-payment"
    )
    return True
