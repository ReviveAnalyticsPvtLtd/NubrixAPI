"""Eligibility and enqueue policy for free-trial expiry warnings."""

__all__ = [
    "TrialExpiryNotificationService",
    "buildTrialExpiryIntent",
    "getTrialExpiryNotificationService",
]


import datetime

from api.services.notifications.notificationDeliveryRepository import (
    NotificationDeliveryRepository,
    getNotificationDeliveryRepository,
)
from api.services.subscriptions.paymentValidationService import parseUtc


def buildTrialExpiryIntent(
    subscription: dict,
    now: datetime.datetime,
) -> dict | None:
    if subscription.get("erasure_pending"):
        return None
    if (subscription.get("status") or "").lower() != "trial":
        return None
    if (subscription.get("billing_mode") or "none").lower() != "none":
        return None

    subscriptionId = subscription.get("id")
    userId = subscription.get("user_id")
    periodEnd = parseUtc(subscription.get("current_period_end"))
    trialStart = parseUtc(subscription.get("current_period_start"))
    if not subscriptionId or not userId or periodEnd is None or trialStart is None:
        return None

    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = now.astimezone(datetime.timezone.utc)

    daysRemaining = (periodEnd.date() - now.date()).days
    if daysRemaining not in (1, 2):
        return None

    periodEndIso = periodEnd.isoformat()
    return {
        "userId": str(userId),
        "subscriptionId": str(subscriptionId),
        "periodEnd": periodEndIso,
        "dedupeKey": (
            f"trial_expiry_warning:v1:{subscriptionId}:{periodEndIso}"
        ),
        "metadata": {"trialStartDate": trialStart.isoformat()},
    }


class TrialExpiryNotificationService:
    def __init__(
        self,
        repository: NotificationDeliveryRepository | None = None,
        eventService=None,
    ):
        self.repository = repository or getNotificationDeliveryRepository()
        self.eventService = eventService

    def enqueueEligible(
        self,
        subscription: dict,
        now: datetime.datetime,
    ) -> tuple[dict | None, bool]:
        intent = buildTrialExpiryIntent(subscription, now)
        if intent is None:
            return None, False
        row, created = self.repository.enqueueTrialExpiry(**intent)
        if created:
            self._auditQueued(row, intent)
        return row, created

    def _auditQueued(self, row: dict, intent: dict) -> None:
        if self.eventService is None:
            from api.commons import client
            from api.services.billing.billingEventService import BillingEventService

            self.eventService = BillingEventService(client)
        deliveryId = str(row["id"])
        self.eventService.log_event(
            user_id=intent["userId"],
            subscription_id=intent["subscriptionId"],
            event_type="email.expiry_warning.queued",
            event_status="PENDING",
            category="notification",
            metadata={
                "deliveryId": deliveryId,
                "notificationType": "trial_expiry_warning",
                "attemptCount": 0,
            },
            idempotency_key=f"{deliveryId}:PENDING:0",
        )


_trialExpiryNotificationService: TrialExpiryNotificationService | None = None


def getTrialExpiryNotificationService() -> TrialExpiryNotificationService:
    global _trialExpiryNotificationService
    if _trialExpiryNotificationService is None:
        _trialExpiryNotificationService = TrialExpiryNotificationService()
    return _trialExpiryNotificationService
