"""Durable billing notification intents for manual monthly billing.

Builds and re-checks the logical email identities from the audited design
section 10: T-7 invoice-ready, T-1 final reminder, one expiry notice for
unpaid+uncancelled cycles, transactional receipts, cancellation confirmation
and the two support-refund confirmations. No due-today, period-start,
failure or win-back email exists in this schedule. Logical identity is
lifecycle + target cycle + milestone — independent of invoice revision or
sweep date — so repricing/resume never duplicate a delivered milestone.
"""

__all__ = [
    "SUPPORTED_BILLING_NOTIFICATION_TYPES",
    "buildBillingNotificationIntent",
    "isBillingNotificationEligible",
    "enqueueBillingIntent",
]


from datetime import datetime, timezone

from api.services.subscriptions.paymentValidationService import parseUtc


SUPPORTED_BILLING_NOTIFICATION_TYPES = {
    "monthly_renewal_ready",
    "monthly_renewal_reminder",
    "monthly_subscription_expired",
    "payment_receipt",
    "monthly_cancellation_confirmation",
    "subscription_refund_initiated",
    "subscription_refund_processed",
}

_SOLICITATION_TYPES = {
    "monthly_renewal_ready",
    "monthly_renewal_reminder",
    "monthly_subscription_expired",
}


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _lifecycleId(snapshot: dict) -> str:
    subscription = snapshot.get("subscription") or {}
    manualBilling = (subscription.get("billing_state") or {}).get("manualBilling") or {}
    return str(manualBilling.get("lifecycleId") or "unknown")


def _periodEnd(snapshot: dict):
    subscription = snapshot.get("subscription") or {}
    return parseUtc(subscription.get("current_period_end"))


def _invoiceUnpaid(snapshot: dict) -> bool:
    invoice = snapshot.get("invoice") or {}
    status = (invoice.get("status") or "").upper()
    if status in ("PAID", "VOID"):
        return False
    if status in ("UPCOMING", "PAYMENT_PENDING"):
        return True
    # Missing invoice metadata means no payable revision exists.
    return False


def _withinWindow(now: datetime, periodEnd, daysBefore: int) -> bool:
    if periodEnd is None:
        return False
    from datetime import timedelta

    milestone = periodEnd - timedelta(days=daysBefore)
    return now >= milestone


def buildBillingNotificationIntent(
    event: dict,
    snapshot: dict,
    now: datetime,
) -> dict | None:
    """Build the durable notification intent for a billing event.

    Returns None when the event is not eligible (opted out, paid, erasure,
    wrong window, suppressed milestone). The dedupe key is the logical
    identity persisted in notification_deliveries; the enqueue repository
    bridge uses the same key idempotently.
    """
    if not isinstance(event, dict):
        return None
    eventType = event.get("type") or event.get("notificationType") or ""
    current = _utc(now)
    subscription = snapshot.get("subscription") or {}
    if subscription.get("erasure_pending"):
        return None
    if (subscription.get("billing_mode") or "").lower() not in (
        "monthly_prepaid",
        "annual_prepaid",
    ):
        # Monthly schedule applies to manual monthly; receipts and refund
        # confirmations apply to every billing mode.
        if eventType not in (
            "payment_receipt",
            "subscription_refund_initiated",
            "subscription_refund_processed",
        ):
            return None
    lifecycleId = _lifecycleId(snapshot)
    cycleId = str(snapshot.get("cycleId") or "")
    periodEnd = _periodEnd(snapshot)
    optOut = bool(subscription.get("renewal_opt_out"))

    if eventType == "monthly_renewal_ready":
        if optOut or not _invoiceUnpaid(snapshot):
            return None
        if not cycleId or periodEnd is None or current >= periodEnd:
            return None
        if not _withinWindow(current, periodEnd, 7):
            return None
        # Catch-up rule: once inside T-1, the reminder replaces the missed
        # ready milestone; never send both as catch-up.
        if _withinWindow(current, periodEnd, 1):
            return None
        return {
            "notificationType": eventType,
            "dedupeKey": f"monthly:{lifecycleId}:{cycleId}:ready",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "lifecycleId": lifecycleId,
                "cycleId": cycleId,
                "invoiceId": (snapshot.get("invoice") or {}).get("id"),
                "milestone": "ready",
            },
        }

    if eventType == "monthly_renewal_reminder":
        if optOut or not _invoiceUnpaid(snapshot):
            return None
        if not cycleId or periodEnd is None or current >= periodEnd:
            return None
        if not _withinWindow(current, periodEnd, 1):
            return None
        return {
            "notificationType": eventType,
            "dedupeKey": f"monthly:{lifecycleId}:{cycleId}:reminder",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "lifecycleId": lifecycleId,
                "cycleId": cycleId,
                "invoiceId": (snapshot.get("invoice") or {}).get("id"),
                "milestone": "reminder",
            },
        }

    if eventType == "monthly_subscription_expired":
        # One expiry notice only if the subscription expired without an
        # explicit cancellation (opted-out users get no expiry/win-back).
        if optOut:
            return None
        if not cycleId or periodEnd is None or current < periodEnd:
            return None
        return {
            "notificationType": eventType,
            "dedupeKey": f"monthly:{lifecycleId}:{cycleId}:expired",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "lifecycleId": lifecycleId,
                "cycleId": cycleId,
                "milestone": "expired",
            },
        }

    if eventType == "payment_receipt":
        providerPaymentId = str(
            event.get("paymentId")
            or snapshot.get("providerPaymentId")
            or ""
        )
        if not providerPaymentId:
            return None
        return {
            "notificationType": eventType,
            "dedupeKey": f"receipt:{providerPaymentId}",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "providerPaymentId": providerPaymentId,
                "purpose": event.get("purpose"),
            },
        }

    if eventType == "monthly_cancellation_confirmation":
        cancellationOperationId = str(event.get("cancellationOperationId") or "")
        if not cancellationOperationId:
            return None
        return {
            "notificationType": eventType,
            "dedupeKey": f"cancel:{lifecycleId}:{cancellationOperationId}",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "lifecycleId": lifecycleId,
                "cancellationOperationId": cancellationOperationId,
            },
        }

    if eventType in ("subscription_refund_initiated", "subscription_refund_processed"):
        refundIntentId = str(event.get("refundIntentId") or "")
        if not refundIntentId:
            return None
        suffix = "initiated" if eventType.endswith("initiated") else "processed"
        return {
            "notificationType": eventType,
            "dedupeKey": f"refund:{refundIntentId}:{suffix}",
            "userId": str(subscription.get("user_id") or ""),
            "subscriptionId": str(subscription.get("id") or ""),
            "periodEnd": (periodEnd.isoformat() if periodEnd else None),
            "metadata": {
                "refundIntentId": refundIntentId,
            },
        }

    return None


def isBillingNotificationEligible(
    delivery: dict,
    snapshot: dict,
    now: datetime,
) -> bool:
    """Re-check type-specific eligibility immediately before dispatch.

    Solicitations (ready/reminder/expiry) are suppressed after payment,
    cancellation, repricing closure or erasure; committed transactional
    confirmations (receipts, cancellation confirmation, refund messages)
    remain deliverable subject only to erasure/privacy rules.
    """
    notificationType = (delivery or {}).get("notification_type") or ""
    subscription = (snapshot or {}).get("subscription") or {}
    if subscription.get("erasure_pending"):
        return False

    if notificationType not in _SOLICITATION_TYPES:
        # Transactional confirmations describe committed facts: deliverable
        # even when the subscriber is now expired/cancelled.
        return notificationType in SUPPORTED_BILLING_NOTIFICATION_TYPES

    if bool(subscription.get("renewal_opt_out")):
        return False
    current = _utc(now)
    periodEnd = _periodEnd(snapshot or {})
    deliveryEnd = parseUtc(delivery.get("period_end"))
    if periodEnd is None or deliveryEnd != periodEnd:
        return False
    metadata = delivery.get("metadata_json") or {}
    if metadata.get("lifecycleId") and metadata["lifecycleId"] != _lifecycleId(snapshot):
        return False
    if notificationType in ("monthly_renewal_ready", "monthly_renewal_reminder"):
        if current >= periodEnd:
            return False
        if not _withinWindow(current,periodEnd,7 if notificationType=='monthly_renewal_ready' else 1):
            return False
        if notificationType == "monthly_renewal_ready" and _withinWindow(current, periodEnd, 1):
            return False
        return _invoiceUnpaid(snapshot or {})

    # Expiry notice: only unpaid, uncancelled, no valid paid continuation.
    invoice = (snapshot or {}).get("invoice")
    if invoice is not None and (invoice.get("status") or "").upper() == "PAID":
        return False
    return periodEnd is not None and current >= periodEnd


def enqueueBillingIntent(intent: dict) -> tuple[dict, bool]:
    """Idempotently enqueue a committed billing notification intent.

    Bridge from the committed ledger intent to notification_deliveries
    using the original dedupe key; an enqueue failure never loses the
    committed intent (the caller retries with the same key).
    """
    from api.services.notifications.notificationDeliveryRepository import (
        getNotificationDeliveryRepository,
    )

    repository = getNotificationDeliveryRepository()
    return repository.enqueueBillingNotification(
        userId=intent["userId"],
        subscriptionId=intent.get("subscriptionId"),
        notificationType=intent["notificationType"],
        dedupeKey=intent["dedupeKey"],
        periodEnd=intent.get("periodEnd") or "",
        metadata=intent.get("metadata") or {},
    )
