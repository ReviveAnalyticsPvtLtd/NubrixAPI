"""
annualRenewalTask.py

Celery Beat scheduler for the annual prepaid renewal invoice lifecycle.

Runs daily and handles two distinct timeline windows:

    T-30 sweep:
        Finds annual subscriptions whose current_period_end is within
        30 days and creates an estimated upcoming renewal invoice via
        invoiceService.createUpcomingRenewalInvoice. Sends a T-30
        awareness email on first creation.

    T-7 sweep:
        Finds upcoming renewal invoices whose due_date is within 7 days,
        freezes next-cycle pricing for dashboard Checkout via
        invoiceService.prepareDashboardRenewalInvoice. Sends a T-7
        payment ready email with the app billing link.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["AnnualRenewalTask"]


from api.services.billing.invoiceService import (
    buildDashboardRenewalUrl,
    createUpcomingRenewalInvoice,
    prepareDashboardRenewalInvoice,
)
from api.services.billing.billingEventService import BillingEventService
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
    subscriptionErasurePending,
    subscriptionRenewalDomainCount,
)
from api.commons import client
from utils.logger import logger
import datetime
import requests
import json
import os

_EMAIL_LOCK_TTL_SECONDS = 120
_MAX_EMAIL_SEND_ATTEMPTS = int(os.environ.get("RENEWAL_EMAIL_MAX_SEND_ATTEMPTS", "3"))




class AnnualRenewalTask:
    """
    Daily scheduler for annual prepaid renewal invoice lifecycle.

    Executes two sweeps per run:
        1. T-30: Create upcoming renewal invoices + send awareness email.
        2. T-7: Prepare dashboard payment, send app billing link email.
    """

    def __init__(self):
        self.client = client
        import redis
        self.redisClient = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            password=os.environ.get("REDIS_PASSWORD", None),
            decode_responses=True,
        )

    def execute(self) -> dict:
        """
        Run the full annual renewal sweep.

        Returns:
            dict: Counts for t30 and t7 operations.
        """
        logger.info("Annual renewal task started")
        t30Results = self._sweepT30()
        t7Results = self._sweepT7()
        logger.info(
            f"Annual renewal task completed — "
            f"T30: {t30Results['created']} created, {t30Results['skipped']} skipped, "
            f"{t30Results['errors']} errors | "
            f"T7: {t7Results['created']} created, {t7Results['skipped']} skipped, "
            f"{t7Results['errors']} errors"
        )
        return {"t30": t30Results, "t7": t7Results}

    def _sweepT30(self) -> dict:
        """
        Find annual subscriptions due within 30 days and create
        upcoming renewal invoices. Sends T-30 awareness email
        idempotently on first creation.

        Returns:
            dict: Counts of created, skipped, and errored invoices.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        windowEnd = (now + datetime.timedelta(days=30)).isoformat()

        subscriptions = (
            self.client.table("subscriptions")
            .select(CANONICAL_SUBSCRIPTION_SELECT)
            .eq("billing_mode", "annual_prepaid")
            .in_("status", ["active", "renewal_upcoming"])
            .gte("current_period_end", now.isoformat())
            .lte("current_period_end", windowEnd)
            .execute()
            .data
        )

        if not subscriptions:
            logger.info("T-30: No annual subscriptions approaching renewal")
            return {"created": 0, "skipped": 0, "errors": 0}

        created = 0
        skipped = 0
        errors = 0

        for subscription in subscriptions:
            if subscriptionErasurePending(subscription):
                skipped += 1
                continue
            userId = subscription["user_id"]
            try:
                userRows = (
                    self.client.table("Users")
                    .select("userId, email, fullName, phoneNumber")
                    .eq("userId", userId)
                    .limit(1)
                    .execute()
                    .data
                )
                if not userRows:
                    logger.warning(f"T-30: User not found for subscription {subscription['id']}")
                    skipped += 1
                    continue

                result = createUpcomingRenewalInvoice(subscription, userRows[0])
                if result:
                    self._transitionToRenewalUpcoming(subscription)
                    self._sendT30Email(userRows[0], result, subscription)
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    f"T-30: Error processing subscription {subscription['id']}: {e}"
                )
                errors += 1

        return {"created": created, "skipped": skipped, "errors": errors}

    def _sweepT7(self) -> dict:
        """
        Find upcoming renewal invoices due within 7 days and prepare
        dashboard Checkout payment. Sends T-7 payment ready email with
        the app billing link on successful preparation.

        Returns:
            dict: Counts of prepared, skipped, and errored invoices.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        windowEnd = (now + datetime.timedelta(days=7)).isoformat()

        invoices = (
            self.client.table("Invoices")
            .select("id, subscription_id, userId, status, due_date, period_start, period_end, "
                    "total_amount, currency, metadata_json")
            .in_("status", ["upcoming", "payment_pending", "expired"])
            .eq("billing_reason", "renewal")
            .not_.is_("due_date", "null")
            .lte("due_date", windowEnd)
            .execute()
            .data
        )

        if not invoices:
            logger.info("T-7: No renewal invoices approaching due date")
            return {"created": 0, "skipped": 0, "errors": 0}

        created = 0
        skipped = 0
        errors = 0

        for invoice in invoices:
            userId = invoice.get("userId", "")
            try:
                userRows = (
                    self.client.table("Users")
                    .select("userId, email, fullName, phoneNumber")
                    .eq("userId", userId)
                    .limit(1)
                    .execute()
                    .data
                )
                if not userRows:
                    logger.warning(f"T-7: User not found for invoice {invoice['id']}")
                    skipped += 1
                    continue

                subscriptionRows = (
                    self.client.table("subscriptions")
                    .select(CANONICAL_SUBSCRIPTION_SELECT)
                    .eq("id", invoice.get("subscription_id"))
                    .limit(1)
                    .execute()
                    .data
                )
                if not subscriptionRows:
                    logger.warning(f"T-7: Subscription not found for invoice {invoice['id']}")
                    skipped += 1
                    continue
                subscription = subscriptionRows[0]
                if subscriptionErasurePending(subscription):
                    skipped += 1
                    continue

                result = prepareDashboardRenewalInvoice(invoice)
                if result:
                    self._sendT7Email(userRows[0], result, subscription)
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"T-7: Error preparing dashboard payment for invoice {invoice['id']}: {e}")
                errors += 1

        return {"created": created, "skipped": skipped, "errors": errors}

    def _transitionToRenewalUpcoming(self, subscription: dict) -> None:
        """
        Transition a subscription to renewal_upcoming if it is currently active.

        Args:
            subscription: The subscription row.
        """
        if subscription.get("status") == "active":
            self.client.table("subscriptions").update({
                "status": "renewal_upcoming",
            }).eq("id", subscription["id"]).execute()

    def _sendT30Email(self, user: dict, invoice: dict, subscription: dict) -> None:
        """
        Send a T-30 renewal awareness email.

        Uses billing_events as an idempotent send-log keyed by
        invoiceId + template to ensure the email is sent exactly once.

        Args:
            user: User row (email, fullName).
            invoice: The created invoice row (id, total_amount).
            subscription: The subscription row (current_period_end).
        """
        template = "renewal_notice_t30"
        invoiceId = invoice.get("id", "")
        userId = user.get("userId", "")
        sendLogKey = f"{invoiceId}:{template}"
        lockKey = f"renewal-email:{sendLogKey}"
        lockAcquired = self.redisClient.set(
            lockKey, "1", nx=True, ex=_EMAIL_LOCK_TTL_SECONDS
        )
        if not lockAcquired:
            return

        existingLog = (
            self.client.table("billing_events")
            .select("id, metadata_json")
            .eq("user_id", userId)
            .eq("event_type", f"email.{template}")
            .eq("event_status", sendLogKey)
            .execute()
            .data
        )
        if not self._shouldSendEmail(existingLog, maxAttempts=_MAX_EMAIL_SEND_ATTEMPTS):
            return

        emailUrl = os.environ.get("RENEWAL_REMINDER_EMAIL_URL")
        if not emailUrl:
            logger.info("T-30 email skipped: RENEWAL_REMINDER_EMAIL_URL not configured")
            return

        payload = {
            "email": user.get("email", ""),
            "name": user.get("fullName", ""),
            "template": template,
            "templateVersion": "1",
            "amount": invoice.get("total_amount", 0),
            "currency": "INR",
            "domainCount": subscriptionRenewalDomainCount(subscription),
            "renewalDate": subscription.get("current_period_end", ""),
            "estimateNote": True,
        }

        deliveryStatus = "SENT"
        try:
            response = requests.post(
                url=emailUrl,
                json=payload,
                headers={"Authorization": f"Bearer {os.environ.get('SUPABASE_KEY_OLD', '')}"},
                timeout=10,
            )
            if response.status_code >= 400:
                deliveryStatus = "DELIVERY_FAILED"
                logger.warning(
                    f"T-30 email delivery failed for invoice {invoiceId}, "
                    f"status={response.status_code}: {response.text}"
                )
        except Exception as e:
            deliveryStatus = "DELIVERY_FAILED"
            logger.error(f"T-30 email send failed for invoice {invoiceId}: {e}")

        BillingEventService(self.client).log_event(
            user_id=userId,
            event_type=f"email.{template}",
            event_status=sendLogKey,
            category="notification",
            idempotency_key=sendLogKey if deliveryStatus == "SENT" else None,
            metadata={
                "invoiceId": invoiceId,
                "template": template,
                "templateVersion": "1",
                "deliveryStatus": deliveryStatus,
                "sentAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
        logger.info(f"T-30 awareness email dispatched for invoice {invoiceId}, delivery={deliveryStatus}")

    def _sendT7Email(self, user: dict, artifact: dict, subscription: dict) -> None:
        """
        Send a T-7 payment ready email with the app dashboard renewal link.

        Uses billing_events as an idempotent send-log keyed by
        invoiceId + template to ensure the email is sent exactly once.

        Args:
            user: User row (email, fullName).
            artifact: The updated invoice row with total_amount.
        """
        template = "renewal_payment_ready_t7"
        invoiceId = artifact.get("id", "")
        userId = user.get("userId", "")
        sendLogKey = f"{invoiceId}:{template}"
        lockKey = f"renewal-email:{sendLogKey}"
        lockAcquired = self.redisClient.set(
            lockKey, "1", nx=True, ex=_EMAIL_LOCK_TTL_SECONDS
        )
        if not lockAcquired:
            return

        existingLog = (
            self.client.table("billing_events")
            .select("id, metadata_json")
            .eq("user_id", userId)
            .eq("event_type", f"email.{template}")
            .eq("event_status", sendLogKey)
            .execute()
            .data
        )
        if not self._shouldSendEmail(existingLog, maxAttempts=_MAX_EMAIL_SEND_ATTEMPTS):
            return

        emailUrl = os.environ.get("RENEWAL_REMINDER_EMAIL_URL")
        if not emailUrl:
            logger.info("T-7 email skipped: RENEWAL_REMINDER_EMAIL_URL not configured")
            return

        metadata = artifact.get("metadata_json") if isinstance(artifact.get("metadata_json"), dict) else {}
        dashboardRenewalUrl = metadata.get("dashboardRenewalUrl") or buildDashboardRenewalUrl(
            str(invoiceId)
        )

        payload = {
            "email": user.get("email", ""),
            "name": user.get("fullName", ""),
            "template": template,
            "templateVersion": "1",
            "amount": artifact.get("total_amount", 0),
            "currency": artifact.get("currency", "INR"),
            "paymentUrl": dashboardRenewalUrl,
            "dashboardFallbackUrl": dashboardRenewalUrl,
            "dueDate": artifact.get("due_date", ""),
            "domainCount": subscriptionRenewalDomainCount(subscription),
        }

        deliveryStatus = "SENT"
        try:
            response = requests.post(
                url=emailUrl,
                json=payload,
                headers={"Authorization": f"Bearer {os.environ.get('SUPABASE_KEY_OLD', '')}"},
                timeout=10,
            )
            if response.status_code >= 400:
                deliveryStatus = "DELIVERY_FAILED"
                logger.warning(
                    f"T-7 email delivery failed for invoice {invoiceId}, "
                    f"status={response.status_code}: {response.text}"
                )
        except Exception as e:
            deliveryStatus = "DELIVERY_FAILED"
            logger.error(f"T-7 email send failed for invoice {invoiceId}: {e}")

        BillingEventService(self.client).log_event(
            user_id=userId,
            event_type=f"email.{template}",
            event_status=sendLogKey,
            category="notification",
            idempotency_key=sendLogKey if deliveryStatus == "SENT" else None,
            metadata={
                "invoiceId": invoiceId,
                "template": template,
                "templateVersion": "1",
                "deliveryStatus": deliveryStatus,
                "sentAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
        logger.info(f"T-7 payment ready email dispatched for invoice {invoiceId}, delivery={deliveryStatus}")

    @staticmethod
    def _shouldSendEmail(existingLogs: list[dict] | None, maxAttempts: int) -> bool:
        """
        Allow retry only when prior delivery attempts failed and the bounded
        resend limit has not been reached.
        """
        logs = existingLogs or []
        if not logs:
            return True
        for log in logs:
            metadata = log.get("metadata_json") or log.get("metadata") or {}
            if metadata.get("deliveryStatus") == "SENT":
                return False
        return len(logs) < maxAttempts
