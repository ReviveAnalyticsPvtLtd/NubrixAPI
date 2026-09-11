"""
pastDueSuspensionTask.py

High-frequency Celery Beat scheduler for past-due and suspension
lifecycle transitions on annual prepaid subscriptions.

Runs every 30 minutes to ensure near-real-time detection of:

    Past-Due:
        Subscriptions whose billing period has ended with an unpaid
        renewal invoice are transitioned to past_due immediately.

    Suspension:
        Subscriptions in past_due state where the reconciliation buffer
        window (60 minutes) has elapsed are suspended. A suspension
        notice email is sent on first suspension.

Both transitions re-read invoice status before mutation to guard
against race conditions with concurrent webhook-paid events.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["PastDueSuspensionTask"]


from api.services.billing.invoiceService import transitionToPastDue, transitionToSuspended
from api.services.billing.billingEventService import BillingEventService
from api.services.subscriptions.subscriptionFieldUtils import subscriptionErasurePending
from api.commons import client
from utils.logger import logger
import datetime
import requests
import json
import os


_RECONCILIATION_BUFFER_MINUTES = 60
_NOTIFICATION_LOCK_TTL_SECONDS = 120
_MAX_EMAIL_SEND_ATTEMPTS = int(os.environ.get("RENEWAL_EMAIL_MAX_SEND_ATTEMPTS", "3"))




class PastDueSuspensionTask:
    """
    High-frequency scheduler for past-due and suspension lifecycle
    transitions on annual prepaid renewal invoices.
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
        Run past-due and suspension sweeps.

        Returns:
            dict: Counts for past_due and suspension operations.
        """
        logger.info("Past-due/suspension task started")
        pastDueResults = self._sweepPastDue()
        suspensionResults = self._sweepSuspension()
        logger.info(
            f"Past-due/suspension task completed — "
            f"past_due: {pastDueResults['transitioned']}, "
            f"suspensions: {suspensionResults['suspended']}"
        )
        return {
            "past_due": pastDueResults,
            "suspension": suspensionResults,
        }

    def _sweepPastDue(self) -> dict:
        """
        Find subscriptions where the billing period has ended and the
        renewal invoice is still unpaid, then transition to past_due.

        Returns:
            dict: Counts of transitioned, skipped, and errored operations.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        transitioned = 0
        skipped = 0
        errors = 0

        subscriptions = (
            self.client.table("subscriptions")
            .select("id, user_id, current_period_end, status, billing_mode, erasure_pending")
            .eq("billing_mode", "annual_prepaid")
            .in_("status", ["active", "renewal_upcoming", "payment_pending"])
            .lte("current_period_end", now.isoformat())
            .execute()
            .data
        )

        for subscription in subscriptions:
            if subscriptionErasurePending(subscription):
                skipped += 1
                continue
            subscriptionId = subscription["id"]
            try:
                invoices = (
                    self.client.table("Invoices")
                    .select("id, status")
                    .eq("subscription_id", subscriptionId)
                    .eq("billing_reason", "renewal")
                    .in_("status", ["upcoming", "payment_pending", "expired"])
                    .limit(1)
                    .execute()
                    .data
                )
                if not invoices:
                    skipped += 1
                    continue

                if transitionToPastDue(subscription, invoices[0]):
                    transitioned += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    f"Past-due transition failed for subscription {subscriptionId}: {e}"
                )
                errors += 1

        return {"transitioned": transitioned, "skipped": skipped, "errors": errors}

    def _sweepSuspension(self) -> dict:
        """
        Find past_due subscriptions where the reconciliation buffer has
        elapsed, then transition to suspended. Sends a suspension notice
        email on successful transition.

        Returns:
            dict: Counts of suspended, skipped, and errored operations.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        bufferCutoff = (
            now - datetime.timedelta(minutes=_RECONCILIATION_BUFFER_MINUTES)
        ).isoformat()
        suspended = 0
        skipped = 0
        errors = 0

        subscriptions = (
            self.client.table("subscriptions")
            .select("id, user_id, current_period_end, status, billing_mode, erasure_pending")
            .eq("billing_mode", "annual_prepaid")
            .eq("status", "past_due")
            .lte("current_period_end", bufferCutoff)
            .execute()
            .data
        )

        for subscription in subscriptions:
            if subscriptionErasurePending(subscription):
                skipped += 1
                continue
            subscriptionId = subscription["id"]
            userId = subscription.get("user_id", "")
            try:
                invoices = (
                    self.client.table("Invoices")
                    .select("id, status")
                    .eq("subscription_id", subscriptionId)
                    .eq("billing_reason", "renewal")
                    .in_("status", ["upcoming", "payment_pending", "expired"])
                    .limit(1)
                    .execute()
                    .data
                )
                if not invoices:
                    skipped += 1
                    continue

                if transitionToSuspended(subscription, invoices[0]):
                    self._sendSuspensionNotice(userId, invoices[0].get("id", ""))
                    suspended += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(
                    f"Suspension transition failed for subscription {subscriptionId}: {e}"
                )
                errors += 1

        return {"suspended": suspended, "skipped": skipped, "errors": errors}

    def _sendSuspensionNotice(self, userId: str, invoiceId: str) -> None:
        """
        Send a suspension notice email via the configured edge function.

        Uses billing_events as a send-log with dedupe key
        (invoiceId, template) to enforce exactly-once delivery.

        Args:
            userId: Internal user ID.
            invoiceId: The unpaid renewal invoice ID.
        """
        template = "suspension_notice"
        sendLogKey = f"{invoiceId}:{template}"
        lockKey = f"renewal-notification:{sendLogKey}"
        lockAcquired = self.redisClient.set(
            lockKey, "1", nx=True, ex=_NOTIFICATION_LOCK_TTL_SECONDS
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

        userRows = (
            self.client.table("Users")
            .select("email, fullName")
            .eq("userId", userId)
            .limit(1)
            .execute()
            .data
        )
        if not userRows:
            logger.warning(f"Cannot send suspension notice: user {userId} not found")
            return

        emailUrl = os.environ.get("RENEWAL_REMINDER_EMAIL_URL")
        if not emailUrl:
            logger.info("Suspension notice skipped: RENEWAL_REMINDER_EMAIL_URL not configured")
            return

        payload = {
            "email": userRows[0].get("email", ""),
            "name": userRows[0].get("fullName", ""),
            "template": template,
            "templateVersion": "1",
            "invoiceId": invoiceId,
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
                    f"Suspension notice delivery failed for user {userId}, "
                    f"status={response.status_code}: {response.text}"
                )
        except Exception as e:
            deliveryStatus = "DELIVERY_FAILED"
            logger.error(f"Suspension notice send failed for user {userId}: {e}")

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
        logger.info(f"Suspension notice dispatched for user {userId}, delivery={deliveryStatus}")

    @staticmethod
    def _shouldSendEmail(existingLogs: list[dict] | None, maxAttempts: int) -> bool:
        logs = existingLogs or []
        if not logs:
            return True
        for log in logs:
            metadata = log.get("metadata_json") or log.get("metadata") or {}
            if metadata.get("deliveryStatus") == "SENT":
                return False
        return len(logs) < maxAttempts
