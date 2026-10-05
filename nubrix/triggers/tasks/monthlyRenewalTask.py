"""
monthlyRenewalTask.py

Hourly Celery Beat scheduler for the manual monthly billing milestones.

Runs hourly to:
    - prepare the next calendar-month renewal invoice at T-7 for eligible,
      opted-in subscriptions (idempotent: an existing payable revision or a
      paid future cycle is reused, never duplicated);
    - queue the T-7 invoice-ready and T-1 final-reminder notification
      intents through the durable outbox with catch-up rules;
    - enqueue the one expiry notice from the committed unpaid expiry
      transition for non-cancelled lifecycles.

There is no automatic charging anywhere in this task and no monthly
past-due/suspension path: paid access ends exactly at the stored period end
(request-time gates enforce the instant), schedulers only persist state and
queue notifications.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["MonthlyRenewalTask"]


from api.commons import client
from api.services.billing.billingEventService import BillingEventService
from api.services.billing.monthlyCoverageService import MonthlyCoverageService
from api.services.notifications.billingNotificationService import (
    buildBillingNotificationIntent,
)
from api.services.subscriptions.paymentValidationService import parseUtc, utcNow
from api.services.subscriptions.subscriptionFieldUtils import (
    subscriptionErasurePending,
)
from utils.logger import logger


_T7_DAYS = 7
_T1_DAYS = 1


class MonthlyRenewalTask:
    def __init__(self, supabaseClient=None, now=None):
        self.client = supabaseClient or client
        self.now = now or utcNow
        self._coverageService = MonthlyCoverageService(now=self.now)

    def execute(self, now=None) -> dict:
        current = now or self.now()
        logger.info("Monthly renewal task started")
        results = {
            "prepared": 0,
            "readyQueued": 0,
            "remindersQueued": 0,
            "expiryQueued": 0,
            "skipped": 0,
            "errors": 0,
        }
        subscriptions = (
            self.client.table("subscriptions")
            .select(
                "id, user_id, status, billing_mode, current_period_start, "
                "current_period_end, renewal_opt_out, erasure_pending, "
                "subscribed_experts, pending_removals, pending_additions, "
                "billing_state"
            )
            .eq("billing_mode", "monthly_prepaid")
            .in_("status", ["active", "renewal_upcoming", "payment_pending"])
            .execute()
            .data
        )
        for subscription in subscriptions or []:
            try:
                outcome = self._processOne(subscription, current)
                for key in ("prepared", "readyQueued", "remindersQueued", "expiryQueued"):
                    results[key] += outcome.get(key, 0)
                if outcome.get("skipped"):
                    results["skipped"] += 1
            except Exception as error:
                logger.error(
                    f"Monthly renewal task failed for subscription "
                    f"{subscription.get('id')}: {error}"
                )
                results["errors"] += 1
        logger.info(f"Monthly renewal task completed: {results}")
        return results

    def _processOne(self, subscription: dict, now) -> dict:
        outcome = {
            "prepared": 0,
            "readyQueued": 0,
            "remindersQueued": 0,
            "expiryQueued": 0,
            "skipped": 0,
        }
        if subscriptionErasurePending(subscription):
            outcome["skipped"] = 1
            return outcome
        userId = subscription.get("user_id")
        periodEnd = parseUtc(subscription.get("current_period_end"))
        if periodEnd is None:
            outcome["skipped"] = 1
            return outcome

        if bool(subscription.get("renewal_opt_out")):
            outcome["skipped"] = 1
            return outcome

        if now < periodEnd:
            invoice = self._loadPayableRenewal(userId, periodEnd)
            if invoice is None and self._withinT7(now, periodEnd):
                try:
                    self._coverageService.prepareRenewalInvoice(
                        userId=userId,
                        subscription=subscription,
                        now=now,
                    )
                    outcome["prepared"] = 1
                except Exception:
                    # Not eligible yet (e.g. paid future cycle already exists
                    # or the removal-empty guard): not an error.
                    outcome["skipped"] = 1
                invoice = self._loadPayableRenewal(userId, periodEnd)
            if invoice is not None:
                cycleId = periodEnd.isoformat()
                snapshot = {
                    "subscription": subscription,
                    "invoice": invoice,
                    "cycleId": cycleId,
                }
                if self._withinT7(now, periodEnd) and not self._withinT1(now, periodEnd):
                    intent = buildBillingNotificationIntent(
                        {"type": "monthly_renewal_ready"}, snapshot, now
                    )
                    if intent:
                        self._enqueue(intent)
                        outcome["readyQueued"] = 1
                elif self._withinT1(now, periodEnd):
                    intent = buildBillingNotificationIntent(
                        {"type": "monthly_renewal_reminder"}, snapshot, now
                    )
                    if intent:
                        self._enqueue(intent)
                        outcome["remindersQueued"] = 1
            return outcome

        # At/after end: the unpaid expiry notice (opted-out users suppressed
        # upstream). State transitions themselves belong to the expiry/
        # boundary tasks; this scheduler only queues the notification.
        expiryIntent = buildBillingNotificationIntent(
            {"type": "monthly_subscription_expired"},
            {
                "subscription": subscription,
                "cycleId": periodEnd.isoformat(),
            },
            now,
        )
        if expiryIntent:
            self._enqueue(expiryIntent)
            outcome["expiryQueued"] = 1
        return outcome

    def _loadPayableRenewal(self, userId, periodEnd) -> dict | None:
        rows = (
            self.client.table("Invoices")
            .select(
                "id, userId, status, billing_reason, period_start, period_end, "
                "metadata_json"
            )
            .eq("userId", userId)
            .eq("billing_reason", "renewal")
            .eq("period_start", periodEnd.isoformat())
            .in_("status", ["UPCOMING", "PAYMENT_PENDING"])
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    @staticmethod
    def _withinT7(now, periodEnd) -> bool:
        from datetime import timedelta

        return now >= periodEnd - timedelta(days=_T7_DAYS)

    @staticmethod
    def _withinT1(now, periodEnd) -> bool:
        from datetime import timedelta

        return now >= periodEnd - timedelta(days=_T1_DAYS)

    def _enqueue(self, intent: dict) -> None:
        try:
            from api.services.notifications.billingNotificationService import (
                enqueueBillingIntent,
            )

            enqueueBillingIntent(intent)
            BillingEventService(self.client).log_event(
                user_id=intent.get("userId"),
                event_type="email.billing_intent.committed",
                event_status="QUEUED",
                category="notification",
                metadata={
                    "notificationType": intent.get("notificationType"),
                    "dedupeKey": intent.get("dedupeKey"),
                },
            )
        except Exception as enqueueError:
            # The committed intent is recoverable from the ledger; never
            # create a duplicate logical email from an enqueue failure.
            logger.warning(
                f"Billing notification bridge failed for dedupeKey="
                f"{intent.get('dedupeKey')}: {enqueueError}"
            )