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
    "annual_renewal_upcoming",
    "monthly_renewal_ready",
    "monthly_renewal_reminder",
    "monthly_subscription_expired",
    "payment_receipt",
    "monthly_cancellation_confirmation",
    "subscription_refund_initiated",
    "subscription_refund_processed",
    "payment_refund_started",
    "payment_refund_completed",
}

_SOLICITATION_TYPES = {
    "annual_renewal_upcoming",
    "monthly_renewal_ready",
    "monthly_renewal_reminder",
    "monthly_subscription_expired",
}


def notificationSnapshot(snapshot: dict, now: datetime) -> dict:
    """Project a private display copy from owned facts, without legacy writes.

    Locked callers supply the typed snapshot from their transaction. Other
    callers load it through the common authority service; caches never grant
    notification eligibility in a unified phase.
    """
    from api.services.subscriptions.subscriptionCompatibility import usesUnifiedSubscription
    if not usesUnifiedSubscription():
        return snapshot
    from api.services.subscriptions.subscriptionSnapshotService import getSubscriptionSnapshotService
    row = dict(snapshot.get('subscription') or {})
    owned = snapshot.get('subscriptionSnapshot') or getSubscriptionSnapshotService().get(row.get('user_id'), now)
    if str(row.get('id')) != owned.subscriptionId:
        raise ValueError('NOTIFICATION_SUBSCRIPTION_OWNER_MISMATCH')
    current = owned.currentPeriod
    deadline = current.end if current else owned.lastPaidEnd
    row.update(status=owned.status, renewal_opt_out=owned.renewalOptOut,
        lifecycle_id=owned.lifecycleId,
        billing_mode={'month':'monthly_prepaid','year':'annual_prepaid'}.get(owned.billingInterval,'none'),
        current_period_start=current.start if current else owned.trial.startedAt if owned.status=='trial' else None,
        current_period_end=deadline if deadline else owned.trial.endsAt if owned.status=='trial' else None,
        erasure_pending=bool(row.get('erasure_pending')) or owned.denialReason=='erasure_pending')
    return {**snapshot, 'subscription':row, 'subscriptionSnapshot':owned}


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _lifecycleId(snapshot: dict) -> str:
    subscription = snapshot.get("subscription") or {}
    if snapshot.get('subscriptionSnapshot') is not None:
        return str(snapshot['subscriptionSnapshot'].lifecycleId or 'unknown')
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
    snapshot = notificationSnapshot(snapshot, current)
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

    owned = snapshot.get('subscriptionSnapshot')
    if owned and eventType in _SOLICITATION_TYPES:
        if (owned.denialReason in ('account_banned','erasure_pending')
                or owned.nextPeriod is not None or owned.purchaseBlockReason=='recovery_required'):
            return None

    if eventType == 'annual_renewal_upcoming':
        if (optOut or not owned or owned.billingInterval != 'year' or not owned.renewalAvailable
                or not _invoiceUnpaid(snapshot) or not cycleId or periodEnd is None
                or current >= periodEnd or not _withinWindow(current,periodEnd,30)
                or _withinWindow(current,periodEnd,7)):
            return None
        return {'notificationType':eventType,'dedupeKey':f'annual:{lifecycleId}:{cycleId}:upcoming',
            'userId':str(subscription['user_id']),'subscriptionId':str(subscription['id']),
            'periodEnd':periodEnd.isoformat(),'metadata':{'lifecycleId':lifecycleId,'cycleId':cycleId,
                'invoiceId':snapshot['invoice']['id'],'milestone':'upcoming'}}

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
                **({'billingInterval':owned.billingInterval} if owned else {}),
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
    *, ignoreMoneyHold: bool = False,
) -> bool:
    """Re-check type-specific eligibility immediately before dispatch.

    Solicitations (ready/reminder/expiry) are suppressed after payment,
    cancellation, repricing closure or erasure; committed transactional
    confirmations (receipts, cancellation confirmation, refund messages)
    remain deliverable subject only to erasure/privacy rules.
    """
    notificationType = (delivery or {}).get("notification_type") or ""
    snapshot = notificationSnapshot(snapshot or {}, _utc(now))
    subscription = (snapshot or {}).get("subscription") or {}
    if subscription.get("erasure_pending"):
        return False

    owned = snapshot.get('subscriptionSnapshot')
    metadata = delivery.get('metadata_json') or {}
    if owned and notificationType == 'monthly_cancellation_confirmation':
        return (owned.renewalOptOut and metadata.get('lifecycleId') == owned.lifecycleId
            and parseUtc(metadata.get('cancellationRequestedAt')) is not None
            and parseUtc(metadata.get('cancellationRequestedAt')) == parseUtc(subscription.get('cancellation_requested_at'))
            and parseUtc(metadata.get('finalPaidEnd')) == (owned.paidUntil or owned.lastPaidEnd)
            and parseUtc(delivery.get('period_end')) == (owned.paidUntil or owned.lastPaidEnd)
            and (ignoreMoneyHold or owned.purchaseBlockReason != 'recovery_required')
            and not snapshot.get('unresolvedOwnedCapture'))
    if notificationType not in _SOLICITATION_TYPES:
        # Transactional confirmations describe committed facts: deliverable
        # even when the subscriber is now expired/cancelled.
        return notificationType in SUPPORTED_BILLING_NOTIFICATION_TYPES

    if bool(subscription.get("renewal_opt_out")):
        return False
    if owned and (owned.nextPeriod is not None or owned.denialReason in ('account_banned','erasure_pending')
            or (not ignoreMoneyHold and owned.purchaseBlockReason == 'recovery_required')):
        return False
    current = _utc(now)
    periodEnd = _periodEnd(snapshot or {})
    deliveryEnd = parseUtc(delivery.get("period_end"))
    if periodEnd is None or deliveryEnd != periodEnd:
        return False
    metadata = delivery.get("metadata_json") or {}
    if owned and metadata.get('lifecycleId') != owned.lifecycleId:
        return False
    if metadata.get("lifecycleId") and metadata["lifecycleId"] != _lifecycleId(snapshot):
        return False
    # Received but unresolved money for this owned cycle holds every
    # solicitation; the caller decides whether a hold reschedules.
    if (snapshot or {}).get('unresolvedOwnedCapture'):
        return False
    if notificationType in ("annual_renewal_upcoming", "monthly_renewal_ready", "monthly_renewal_reminder"):
        invoice = snapshot.get('invoice') or {}
        if owned:
            frozen = (invoice.get('metadata_json') or {}).get('manualBilling') or {}
            if (str(invoice.get('id')) != str(metadata.get('invoiceId'))
                    or invoice.get('userId') != subscription.get('user_id')
                    or str(invoice.get('subscription_id')) != owned.subscriptionId
                    or frozen.get('lifecycleId') != owned.lifecycleId
                    or parseUtc(invoice.get('period_start')) != periodEnd
                    or type(frozen.get('revision')) is not int or frozen['revision'] < 1
                    or metadata.get('invoiceRevision') != frozen.get('revision')
                    or frozen.get('closedReason') or (invoice.get('metadata_json') or {}).get('repricingRequired')):
                return False
        if current >= periodEnd:
            return False
        if notificationType == 'annual_renewal_upcoming':
            return (owned is not None and owned.billingInterval=='year'
                and _withinWindow(current,periodEnd,30) and not _withinWindow(current,periodEnd,7)
                and _invoiceUnpaid(snapshot))
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
