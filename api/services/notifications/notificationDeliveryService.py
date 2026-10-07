"""Dispatch orchestration for durable notification deliveries."""

__all__ = ["NotificationDeliveryService", "getNotificationDeliveryService"]


import datetime

from api.services.billing.billingEventService import BillingEventService
from api.services.notifications.brevoEventClient import (
    BrevoEventClient,
    ProviderEvent,
)
from api.services.notifications.edgeEmailClient import EdgeEmailClient, EdgeSendResult
from api.services.notifications.notificationDeliveryRepository import (
    NotificationDeliveryRepository,
    getNotificationDeliveryRepository,
)
from api.services.notifications.trialExpiryNotificationService import (
    buildTrialExpiryIntent,
)
from api.services.subscriptions.paymentValidationService import parseUtc
from utils.logger import logger


_RETRY_DELAYS_SECONDS = (300, 1800, 7200, 21600, 43200)
_BLOCKING_ERROR_CODES = {
    "INVALID_RECIPIENT",
    "INVALID_RECIPIENT_NAME",
    "INVALID_EMAIL",
    "PROVIDER_BLOCKED",
}


class NotificationDeliveryService:
    def __init__(
        self,
        repository: NotificationDeliveryRepository | None = None,
        edgeClient: EdgeEmailClient | None = None,
        supabaseClient=None,
        eventService=None,
        brevoClient: BrevoEventClient | None = None,
        now=None,
    ):
        self.repository = repository or getNotificationDeliveryRepository()
        self.edgeClient = edgeClient or EdgeEmailClient()
        if supabaseClient is None:
            from api.commons import client

            supabaseClient = client
        self.supabaseClient = supabaseClient
        self.eventService = eventService or BillingEventService(supabaseClient)
        self.brevoClient = brevoClient or BrevoEventClient()
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def dispatchBatch(self, workerId: str, limit: int = 50) -> dict:
        self.edgeClient.validate()
        summary = {
            "claimed": 0,
            "accepted": 0,
            "recovered": 0,
            "retryScheduled": 0,
            "ambiguous": 0,
            "cancelled": 0,
            "failed": 0,
            "errors": 0,
        }

        for _index in range(limit):
            deliveries = self.repository.claimDue(
                workerId,
                limit=1,
                leaseSeconds=300,
            )
            if not deliveries:
                break
            delivery = deliveries[0]
            summary["claimed"] += 1
            try:
                self._audit(delivery, "SENDING")
                self._dispatchOne(delivery, workerId, summary)
            except Exception:
                summary["errors"] += 1
                logger.error(
                    "Notification dispatch row failed "
                    f"deliveryId={delivery.get('id', 'unknown')}"
                )
                self._scheduleUnexpectedFailure(delivery, workerId, summary)
        return summary

    def reconcileBatch(self, limit: int = 100) -> dict:
        accepted = self.repository.listForReconciliation(limit=limit)
        ambiguous = self.repository.listAmbiguous(limit=limit)
        summary = {
            "checked": len(accepted) + len(ambiguous),
            "delivered": 0,
            "bounced": 0,
            "blocked": 0,
            "failed": 0,
            "pending": 0,
            "recovered": 0,
            "errors": 0,
        }

        for delivery in accepted:
            try:
                self._reconcileAccepted(delivery, summary)
            except Exception:
                summary["errors"] += 1
                logger.error(
                    "Notification reconciliation failed "
                    f"deliveryId={delivery.get('id', 'unknown')}"
                )

        for delivery in ambiguous:
            try:
                self._reconcileAmbiguous(delivery, summary)
            except Exception:
                summary["errors"] += 1
                logger.error(
                    "Ambiguous notification reconciliation failed "
                    f"deliveryId={delivery.get('id', 'unknown')}"
                )
        return summary

    def _dispatchOne(self, delivery: dict, workerId: str, summary: dict) -> None:
        now = self.now()
        if delivery.get("last_error_code") == "AMBIGUOUS_SEND":
            tag = f"nubrix_delivery:{delivery['id']}"
            event = self.brevoClient.findByTag(tag)
            if event is not None:
                if not self.repository.recoverClaimedAmbiguous(
                    str(delivery["id"]),
                    workerId,
                    event.messageId,
                    now.isoformat(),
                ):
                    raise RuntimeError("DELIVERY_LEASE_LOST")
                summary["accepted"] += 1
                summary["recovered"] += 1
                self._audit(delivery, "ACCEPTED", "AMBIGUOUS_SEND_RECOVERED")
                return
        if int(delivery.get("attempt_count") or 0) > 6:
            self._terminal(
                delivery,
                workerId,
                "FAILED",
                "RETRY_ATTEMPTS_EXHAUSTED",
            )
            summary["failed"] += 1
            return
        if (delivery.get('notification_type') != 'trial_expiry_warning'
                and delivery.get('last_error_code') == 'AMBIGUOUS_SEND'):
            self._scheduleRetry(delivery,workerId,'AMBIGUOUS_SEND',
                now + datetime.timedelta(minutes=30),now + datetime.timedelta(minutes=5))
            summary['ambiguous'] += 1
            return
        subscription = self._findOne(
            "subscriptions",
            "id",
            delivery.get("subscription_id"),
            (
                "id, user_id, current_period_start, current_period_end, "
                "status, billing_mode, erasure_pending, renewal_opt_out, billing_state, is_canonical"
            ),
        )
        if subscription is None:
            self._cancel(delivery, workerId, "SUBSCRIPTION_NOT_FOUND", summary)
            return

        billingDelivery = delivery.get("notification_type") != "trial_expiry_warning"
        if billingDelivery:
            from api.services.notifications.billingNotificationService import isBillingNotificationEligible
            metadata = dict(delivery.get("metadata_json") or {})
            invoice = self._findOne("Invoices", "id", metadata.get("invoiceId"), "id, status, period_start, period_end, metadata_json") if metadata.get("invoiceId") else None
            if invoice and delivery.get('notification_type') == 'payment_receipt':
                metadata['serviceRevoked'] = bool((invoice.get('metadata_json') or {}).get('manualBilling',{}).get('revokedAt'))
            snapshot = {"subscription":subscription, "invoice":invoice}
            if not isBillingNotificationEligible(delivery, snapshot, now):
                self._cancel(delivery, workerId, "SUBSCRIPTION_NOT_ELIGIBLE", summary)
                return
        else:
            intent = buildTrialExpiryIntent(subscription, now)
            if intent is None or not self._samePeriod(intent["periodEnd"], delivery.get("period_end")):
                self._cancel(delivery, workerId, "SUBSCRIPTION_NOT_ELIGIBLE", summary)
                return

        user = self._findOne(
            "Users",
            "userId",
            delivery.get("user_id"),
            "userId, email, fullName",
        )
        if user is None:
            self._cancel(delivery, workerId, "USER_NOT_FOUND", summary)
            return
        email = str(user.get("email") or "").strip()
        name = str(user.get("fullName") or "").strip()
        if not email or not name:
            self._cancel(delivery, workerId, "USER_PROFILE_INCOMPLETE", summary)
            return

        if billingDelivery:
            authorization = self.repository.authorizeBillingSubmissionResult(
                str(delivery['id']),workerId,delivery['payload_version'])
            if authorization == 'HELD':
                # Unresolved received money for this cycle: keep the original
                # milestone and re-check after the hold, never discard it.
                self._scheduleRetry(delivery,workerId,'PAYMENT_HOLD',now + datetime.timedelta(hours=1),None)
                summary['retryScheduled'] += 1
                return
            if authorization != 'AUTHORIZED':
                self._cancel(delivery,workerId,'SUBSCRIPTION_NOT_ELIGIBLE',summary)
                return
            delivery['submission_started_at'] = now.isoformat()
            payload = {"mode":"send", "deliveryId":str(delivery["id"]),
                "notificationType":delivery["notification_type"], "templateVersion":delivery["template_version"],
                "email":email, "name":name, "periodEnd":str(delivery.get("period_end") or ""),
                "metadata":metadata, "trackingTag":f"nubrix_delivery:{delivery['id']}"}
            result = self.edgeClient.sendBilling(payload)
            self._applySendResult(delivery,workerId,result,now,summary)
            return

        payload = {
            "deliveryId": str(delivery["id"]),
            "notificationType": "trial_expiry_warning",
            "templateVersion": "1",
            "email": email,
            "name": name,
            "trialStartDate": intent["metadata"]["trialStartDate"],
            "trialEndDate": intent["periodEnd"],
            "trackingTag": f"nubrix_delivery:{delivery['id']}",
        }
        result = self.edgeClient.sendTrialExpiry(payload)
        self._applySendResult(delivery, workerId, result, now, summary)

    def _reconcileAccepted(self, delivery: dict, summary: dict) -> None:
        messageId = str(delivery.get("provider_message_id") or "").strip()
        if not messageId:
            self._terminalReconciled(
                delivery,
                "FAILED",
                "PROVIDER_MESSAGE_ID_MISSING",
                "missing_message_id",
                None,
                summary,
            )
            return

        event = self.brevoClient.findByMessageId(messageId)
        if event is not None:
            self._applyProviderEvent(delivery, event, summary)
            return

        now = self.now()
        acceptedAt = parseUtc(delivery.get("accepted_at"))
        if acceptedAt is None:
            self._terminalReconciled(
                delivery,
                "FAILED",
                "ACCEPTED_AT_MISSING",
                "missing_accepted_at",
                None,
                summary,
            )
            return
        age = now - acceptedAt
        if age >= datetime.timedelta(hours=72):
            self._terminalReconciled(
                delivery,
                "FAILED",
                "DELIVERY_STATUS_TIMEOUT",
                "timeout",
                None,
                summary,
            )
            return

        delay = (
            datetime.timedelta(minutes=5)
            if age < datetime.timedelta(hours=1)
            else datetime.timedelta(minutes=30)
        )
        self._advanceReconciliation(
            delivery,
            str(delivery.get("provider_status") or "request"),
            now + delay,
            None,
        )
        summary["pending"] += 1

    def _reconcileAmbiguous(self, delivery: dict, summary: dict) -> None:
        tag = f"nubrix_delivery:{delivery['id']}"
        event = self.brevoClient.findByTag(tag)
        if event is not None:
            if not self.repository.attachRecoveredMessageId(
                str(delivery["id"]),
                event.messageId,
            ):
                raise RuntimeError("DELIVERY_STATE_CHANGED")
            summary["recovered"] += 1
            self._audit(delivery, "ACCEPTED", "AMBIGUOUS_SEND_RECOVERED")
            self._applyProviderEvent(delivery, event, summary)
            return

        now = self.now()
        nextAttemptAt = parseUtc(delivery.get("next_attempt_at"))
        if nextAttemptAt is not None and now >= nextAttemptAt:
            if int(delivery.get("attempt_count") or 0) >= 6:
                self._terminalReconciled(
                    delivery,
                    "FAILED",
                    "AMBIGUOUS_SEND_UNRESOLVED",
                    "ambiguous_timeout",
                    None,
                    summary,
                )
                return
            self._advanceReconciliation(
                delivery,
                "ambiguous_unresolved",
                now + datetime.timedelta(minutes=30),
                "AMBIGUOUS_SEND",
            )
            summary["pending"] += 1
            return

        self._advanceReconciliation(
            delivery,
            "ambiguous_search",
            now + datetime.timedelta(minutes=5),
            "AMBIGUOUS_SEND",
        )
        summary["pending"] += 1

    def _applyProviderEvent(
        self,
        delivery: dict,
        event: ProviderEvent,
        summary: dict,
    ) -> None:
        if event.terminalStatus is None:
            acceptedAt = parseUtc(delivery.get("accepted_at")) or self.now()
            delay = (
                datetime.timedelta(minutes=5)
                if self.now() - acceptedAt < datetime.timedelta(hours=1)
                else datetime.timedelta(minutes=30)
            )
            self._advanceReconciliation(
                delivery,
                event.providerStatus,
                self.now() + delay,
                event.errorCode,
            )
            summary["pending"] += 1
            return

        self._terminalReconciled(
            delivery,
            event.terminalStatus,
            event.errorCode,
            event.providerStatus,
            event.occurredAt if event.terminalStatus == "DELIVERED" else None,
            summary,
        )

    def _advanceReconciliation(
        self,
        delivery: dict,
        providerStatus: str,
        nextReconcileAt: datetime.datetime,
        errorCode: str | None,
    ) -> None:
        if not self.repository.advanceReconciliation(
            str(delivery["id"]),
            providerStatus,
            nextReconcileAt.isoformat(),
            errorCode,
        ):
            raise RuntimeError("DELIVERY_STATE_CHANGED")

    def _terminalReconciled(
        self,
        delivery: dict,
        status: str,
        errorCode: str | None,
        providerStatus: str,
        deliveredAt: str | None,
        summary: dict,
    ) -> None:
        if not self.repository.markTerminal(
            str(delivery["id"]),
            status,
            errorCode=errorCode,
            providerStatus=providerStatus,
            deliveredAt=deliveredAt,
        ):
            raise RuntimeError("DELIVERY_STATE_CHANGED")
        summary[status.lower()] += 1
        self._audit(delivery, status, errorCode)

    def _applySendResult(
        self,
        delivery: dict,
        workerId: str,
        result: EdgeSendResult,
        now: datetime.datetime,
        summary: dict,
    ) -> None:
        if result.outcome == "ACCEPTED":
            if not self.repository.markAccepted(
                str(delivery["id"]),
                workerId,
                str(result.messageId),
                now.isoformat(),
                **self._versionArguments(delivery),
            ):
                raise RuntimeError("DELIVERY_LEASE_LOST")
            summary["accepted"] += 1
            self._audit(delivery, "ACCEPTED")
            return

        if result.outcome == "AMBIGUOUS":
            self._scheduleRetry(
                delivery,
                workerId,
                result.errorCode or "AMBIGUOUS_SEND",
                now + datetime.timedelta(minutes=30),
                now + datetime.timedelta(minutes=5),
            )
            summary["ambiguous"] += 1
            self._audit(delivery, "RETRY_PENDING", result.errorCode)
            return

        if result.outcome == "PERMANENT":
            terminalStatus = (
                "BLOCKED"
                if result.errorCode in _BLOCKING_ERROR_CODES
                else "FAILED"
            )
            self._terminal(
                delivery,
                workerId,
                terminalStatus,
                result.errorCode or "PERMANENT_EDGE_FAILURE",
            )
            summary["failed"] += 1
            return

        attemptCount = int(delivery.get("attempt_count") or 0)
        if attemptCount >= 6:
            self._terminal(
                delivery,
                workerId,
                "FAILED",
                result.errorCode or "RETRY_ATTEMPTS_EXHAUSTED",
            )
            summary["failed"] += 1
            return

        delaySeconds = _RETRY_DELAYS_SECONDS[attemptCount - 1]
        self._scheduleRetry(
            delivery,
            workerId,
            result.errorCode or "RETRYABLE_EDGE_FAILURE",
            now + datetime.timedelta(seconds=delaySeconds),
            None,
        )
        summary["retryScheduled"] += 1
        self._audit(delivery, "RETRY_PENDING", result.errorCode)

    @staticmethod
    def _versionArguments(delivery):
        return {'payloadVersion':delivery['payload_version']} if delivery.get('notification_type') != 'trial_expiry_warning' else {}

    def _scheduleUnexpectedFailure(
        self,
        delivery: dict,
        workerId: str,
        summary: dict,
    ) -> None:
        attemptCount = int(delivery.get("attempt_count") or 0)
        if attemptCount >= 6:
            try:
                self._terminal(
                    delivery,
                    workerId,
                    "FAILED",
                    "AMBIGUOUS_SEND" if delivery.get("submission_started_at") else "INTERNAL_DISPATCH_ERROR",
                )
                summary["failed"] += 1
            except Exception:
                logger.error(
                    "Failed to terminalize notification after internal error "
                    f"deliveryId={delivery.get('id', 'unknown')}"
                )
            return
        try:
            nextAttempt = self.now() + datetime.timedelta(
                seconds=_RETRY_DELAYS_SECONDS[attemptCount - 1]
            )
            self._scheduleRetry(
                delivery,
                workerId,
                "AMBIGUOUS_SEND" if delivery.get("submission_started_at") else "INTERNAL_DISPATCH_ERROR",
                nextAttempt,
                self.now() + datetime.timedelta(minutes=5) if delivery.get("submission_started_at") else None,
            )
            summary["retryScheduled"] += 1
        except Exception:
            logger.error(
                "Failed to reschedule notification after internal error "
                f"deliveryId={delivery.get('id', 'unknown')}"
            )

    def _scheduleRetry(
        self,
        delivery: dict,
        workerId: str,
        errorCode: str,
        nextAttemptAt: datetime.datetime,
        nextReconcileAt: datetime.datetime | None,
    ) -> None:
        changed = self.repository.scheduleRetry(
            str(delivery["id"]),
            workerId,
            errorCode,
            nextAttemptAt.isoformat(),
            nextReconcileAt.isoformat() if nextReconcileAt else None,
            **self._versionArguments(delivery),
        )
        if not changed:
            raise RuntimeError("DELIVERY_LEASE_LOST")

    def _cancel(
        self,
        delivery: dict,
        workerId: str,
        errorCode: str,
        summary: dict,
    ) -> None:
        self._terminal(delivery, workerId, "CANCELLED", errorCode)
        summary["cancelled"] += 1

    def _terminal(
        self,
        delivery: dict,
        workerId: str,
        status: str,
        errorCode: str,
    ) -> None:
        changed = self.repository.markTerminal(
            str(delivery["id"]),
            status,
            errorCode=errorCode,
            leaseOwner=workerId,
            **self._versionArguments(delivery),
        )
        if not changed:
            raise RuntimeError("DELIVERY_LEASE_LOST")
        self._audit(delivery, status, errorCode)

    def _audit(
        self,
        delivery: dict,
        status: str,
        errorCode: str | None = None,
    ) -> None:
        attemptCount = int(delivery.get("attempt_count") or 0)
        metadata = {
            "deliveryId": str(delivery["id"]),
            "notificationType": delivery.get("notification_type"),
            "attemptCount": attemptCount,
        }
        if errorCode:
            metadata["errorCode"] = errorCode
        eventNames = {
            "PENDING": "email.expiry_warning.queued",
            "SENDING": "email.expiry_warning.sending",
            "ACCEPTED": "email.expiry_warning.accepted",
            "RETRY_PENDING": "email.expiry_warning.retry_scheduled",
            "DELIVERED": "email.expiry_warning.delivered",
            "BOUNCED": "email.expiry_warning.bounced",
            "BLOCKED": "email.expiry_warning.blocked",
            "FAILED": "email.expiry_warning.failed",
            "CANCELLED": "email.expiry_warning.cancelled",
        }
        self.eventService.log_event(
            user_id=delivery.get("user_id"),
            subscription_id=delivery.get("subscription_id"),
            event_type=eventNames[status],
            event_status=status,
            category="notification",
            metadata=metadata,
            idempotency_key=(
                f"{delivery['id']}:{status}:{attemptCount}"
            ),
        )

    def _findOne(
        self,
        table: str,
        field: str,
        value,
        columns: str,
    ) -> dict | None:
        if value is None:
            return None
        rows = (
            self.supabaseClient.table(table)
            .select(columns)
            .eq(field, value)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    @staticmethod
    def _samePeriod(expected, actual) -> bool:
        expectedUtc = parseUtc(expected)
        actualUtc = parseUtc(actual)
        return expectedUtc is not None and expectedUtc == actualUtc


_notificationDeliveryService: NotificationDeliveryService | None = None


def getNotificationDeliveryService() -> NotificationDeliveryService:
    global _notificationDeliveryService
    if _notificationDeliveryService is None:
        _notificationDeliveryService = NotificationDeliveryService()
    return _notificationDeliveryService
