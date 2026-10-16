"""
invoiceService.py

Service layer for the annual renewal invoice lifecycle.

Provides idempotent helpers for:
    - Issuing frozen upcoming renewal invoices (T-30).
    - Preparing issued invoices for dashboard Checkout payment (T-7).
    - Transitioning invoice and subscription states through the
      payment_pending → paid / past_due → suspended lifecycle.

Annual issuance/preparation uses the same owner-locked SQL repository as
manual payments; catalog locks serialize new snapshots with publication.
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


from api.services.subscriptions.paymentValidationService import normalizeChurnedSubscription
from api.commons import client
from utils.logger import logger
import datetime
from api.services.billing.billingClient import getBillingRedisClient
import os


_RECONCILIATION_BUFFER_MINUTES = 60


def createUpcomingRenewalInvoice(subscription: dict, user: dict) -> dict | None:
    """Issue the annual renewal once; catalog and invoice commit together."""
    from api.services.billing.manualBillingRepository import getManualBillingRepository
    try:
        return getManualBillingRepository().createAnnualRenewalInvoice(
            subscription["user_id"], subscription["id"])
    except Exception as exc:
        logger.error(f"Failed to issue annual renewal for {subscription.get('id')}: {exc}")
        return None


def buildDashboardRenewalUrl(invoiceId: str) -> str:
    """
    Build the app billing URL used as the customer CTA for annual renewals.
    """
    dashboardBaseUrl = os.environ.get("DASHBOARD_BASE_URL", "").rstrip("/")
    renewalPath = f"/settings/billing-details?renewalInvoiceId={invoiceId}"
    return f"{dashboardBaseUrl}{renewalPath}" if dashboardBaseUrl else renewalPath


def prepareDashboardRenewalInvoice(invoice: dict) -> dict | None:
    """Prepare frozen annual pricing; only closed selection revisions reprice."""
    from api.services.billing.manualBillingRepository import getManualBillingRepository
    try:
        return getManualBillingRepository().prepareAnnualRenewalInvoice(invoice["id"])
    except Exception as exc:
        logger.error(f"Failed to prepare annual renewal {invoice.get('id')}: {exc}")
        return None


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
