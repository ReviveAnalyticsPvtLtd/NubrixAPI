"""
reconciliationService.py

Admin-facing reconciliation service for billing operations.

Provides:
    - Anomaly reporting: pending attempts older than threshold,
      captured-on-provider-but-unpaid-internal mismatches,
      and duplicate/late webhook anomalies.
    - Safe manual actions: replay webhook processing by event ID,
      and mark investigated mismatches with audit notes.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["ReconciliationService"]


from api.services.billing.billingEventService import BillingEventService
from api.commons import client
from utils.exceptionHandler import CustomException
from utils.logger import logger
import datetime
import os


_PENDING_ATTEMPT_THRESHOLD_MINUTES = int(
    os.environ.get("RECONCILIATION_PENDING_THRESHOLD_MINUTES", "60")
)
_REPORT_BATCH_LIMIT = 100




class ReconciliationService:
    """
    Admin-facing reconciliation service for detecting billing anomalies
    and executing safe remediation actions with full audit trails.
    """

    def __init__(self,repository=None):
        self.client = client
        if repository is None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repository=getManualBillingRepository()
        self.repository=repository

    def listManualObligations(self,limit=100,cursor=None):
        from api.services.billing.manualObligationReport import listObligations
        return listObligations(self.repository,limit,cursor)

    def generateReport(self) -> dict:
        """
        Generate a comprehensive reconciliation report covering all
        known anomaly categories.

        Returns:
            dict: Report containing staleAttempts, providerMismatches,
                  webhookAnomalies, and metadata.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        report = {
            "generatedAt": now.isoformat(),
            "staleAttempts": self._findStaleAttempts(now),
            "providerMismatches": self._findProviderMismatches(now),
            "webhookAnomalies": self._findWebhookAnomalies(now),
            "manualObligations": self.listManualObligations(),
        }
        report["summary"] = {
            "staleAttemptCount": len(report["staleAttempts"]),
            "providerMismatchCount": len(report["providerMismatches"]),
            "webhookAnomalyCount": len(report["webhookAnomalies"]),
            "totalAnomalies": (
                len(report["staleAttempts"])
                + len(report["providerMismatches"])
                + len(report["webhookAnomalies"])
            ),
        }
        logger.info(
            f"Reconciliation report generated — "
            f"stale={report['summary']['staleAttemptCount']}, "
            f"mismatches={report['summary']['providerMismatchCount']}, "
            f"webhookAnomalies={report['summary']['webhookAnomalyCount']}"
        )
        return report

    def _findStaleAttempts(self, now: datetime.datetime) -> list[dict]:
        """
        Find billing event payment attempts that remain in unresolved states
        (created, pending_provider_ack, authorized) beyond the configured threshold.

        Args:
            now: Current UTC datetime for threshold computation.

        Returns:
            list[dict]: Stale attempt rows with age metadata.
        """
        cutoff = (
            now - datetime.timedelta(minutes=_PENDING_ATTEMPT_THRESHOLD_MINUTES)
        ).isoformat()

        stale = (
            self.client.table("billing_events")
            .select(
                "id, user_id, subscription_id, payment_status, amount, currency, "
                "attempted_at, provider_payment_id, provider_order_id, cycle_key"
            )
            .eq("event_category", "payment_attempt")
            .in_("payment_status", ["created", "pending_provider_ack", "authorized"])
            .lte("attempted_at", cutoff)
            .limit(_REPORT_BATCH_LIMIT)
            .execute()
            .data
        )

        for attempt in stale:
            attemptedAt = attempt.get("attempted_at", "")
            if attemptedAt:
                try:
                    from dateutil import parser as dtparser
                    attemptDt = dtparser.isoparse(attemptedAt)
                    if attemptDt.tzinfo is None:
                        attemptDt = attemptDt.replace(tzinfo=datetime.timezone.utc)
                    attempt["ageMinutes"] = int((now - attemptDt).total_seconds() / 60)
                except (ValueError, TypeError):
                    attempt["ageMinutes"] = None

        return stale

    def _findProviderMismatches(self, now: datetime.datetime) -> list[dict]:
        """
        Find billing event payment attempts marked as captured on the provider (Razorpay)
        but whose corresponding internal invoice is not marked as paid.

        Queries billing_events with payment_status='captured' and cross-references
        the linked invoice status.

        Args:
            now: Current UTC datetime (for report context).

        Returns:
            list[dict]: Mismatch records with provider and internal state.
        """
        captured = (
            self.client.table("billing_events")
            .select(
                "id, user_id, provider_payment_id, provider_order_id, "
                "amount, payment_status, cycle_key"
            )
            .eq("event_category", "payment_attempt")
            .eq("payment_status", "captured")
            .limit(_REPORT_BATCH_LIMIT)
            .execute()
            .data
        )

        mismatches = []
        for attempt in captured:
            orderId = attempt.get("provider_order_id")
            if not orderId:
                continue

            invoices = (
                self.client.table("Invoices")
                .select("id, status, total_amount")
                .eq("razorpay_order_id", orderId)
                .limit(1)
                .execute()
                .data
            )

            if not invoices:
                mismatches.append({
                    **attempt,
                    "anomalyType": "captured_no_internal_invoice",
                    "internalInvoice": None,
                })
                continue

            invoice = invoices[0]
            invoiceStatus = (invoice.get("status") or "").upper()
            if invoiceStatus != "PAID":
                mismatches.append({
                    **attempt,
                    "anomalyType": "captured_but_invoice_unpaid",
                    "internalInvoice": invoice,
                })

        return mismatches

    def _findWebhookAnomalies(self, now: datetime.datetime) -> list[dict]:
        """
        Find webhook events that are stuck in processing or failed states,
        indicating duplicate delivery or handler failures.

        Args:
            now: Current UTC datetime for staleness computation.

        Returns:
            list[dict]: Anomalous webhook event rows.
        """
        anomalies = (
            self.client.table("WebhookEvents")
            .select(
                "razorpayEventId, eventType, status, attempts, "
                "lastAttemptAt, errorMessage"
            )
            .in_("status", ["processing", "failed"])
            .limit(_REPORT_BATCH_LIMIT)
            .execute()
            .data
        )

        return anomalies

    def replayWebhookEvent(self, eventId: str, adminUserId: str) -> dict:
        """
        Replay webhook processing for a specific event ID.

        Fetches the stored event payload from WebhookEvents and
        re-dispatches it through the webhook handler pipeline.
        Records an audit trail entry for the replay action.

        Args:
            eventId: The Razorpay event ID to replay.
            adminUserId: The admin user initiating the replay.

        Returns:
            dict: Replay outcome with status and event metadata.

        Raises:
            ValueError: If the event ID is not found.
        """
        eventRows = (
            self.client.table("WebhookEvents")
            .select("razorpayEventId, eventType, payload, status")
            .eq("razorpayEventId", eventId)
            .limit(1)
            .execute()
            .data
        )
        if not eventRows:
            raise ValueError(f"Webhook event {eventId} not found in WebhookEvents")

        eventRow = eventRows[0]
        payload = eventRow.get("payload", {})
        eventType = eventRow.get("eventType", "")

        self.client.table("WebhookEvents").update({
            "status": "processing",
            "attempts": (eventRow.get("attempts") or 0) + 1,
            "lastAttemptAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "errorMessage": None,
        }).eq("razorpayEventId", eventId).execute()

        try:
            from api.services.webhookService import webhookService
            handlerName = webhookService.EVENT_HANDLERS.get(eventType) if hasattr(webhookService, 'EVENT_HANDLERS') else None
            if not handlerName:
                from api.services.webhookService import EVENT_HANDLERS
                handlerName = EVENT_HANDLERS.get(eventType)

            if not handlerName:
                raise ValueError(f"No handler registered for event type: {eventType}")

            handler = getattr(webhookService, handlerName)
            handler(payload)

            self.client.table("WebhookEvents").update({
                "status": "completed",
            }).eq("razorpayEventId", eventId).execute()

            self._auditLog(
                adminUserId, "reconciliation.webhook_replayed",
                status="SUCCESS",
                metadata={
                    "eventId": eventId,
                    "eventType": eventType,
                },
            )

            logger.info(f"Webhook event {eventId} replayed successfully")
            return {"eventId": eventId, "eventType": eventType, "status": "replayed"}

        except Exception as e:
            self.client.table("WebhookEvents").update({
                "status": "failed",
                "errorMessage": str(e)[:2000],
            }).eq("razorpayEventId", eventId).execute()

            self._auditLog(
                adminUserId, "reconciliation.webhook_replay_failed",
                status="FAILED",
                metadata={
                    "eventId": eventId,
                    "eventType": eventType,
                    "error": str(e)[:500],
                },
            )
            raise

    def markInvestigated(self, entityType: str, entityId: str,
                         adminUserId: str, note: str) -> dict:
        """
        Mark a reconciliation anomaly as investigated with an audit note.

        Supports billing event payment attempts and WebhookEvents entity types.

        Args:
            entityType: 'payment_attempt' or 'webhook_event'.
            entityId: The primary key of the entity.
            adminUserId: The admin user recording the investigation.
            note: Free-text audit note describing the investigation outcome.

        Returns:
            dict: Confirmation with entity metadata.

        Raises:
            ValueError: If entity type is unsupported or entity not found.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if entityType == "payment_attempt":
            existing = (
                self.client.table("billing_events")
                .select("id, payment_status, user_id")
                .eq("event_category", "payment_attempt")
                .eq("id", entityId)
                .limit(1)
                .execute()
                .data
            )
            if not existing:
                raise ValueError(f"Payment attempt {entityId} not found")
            if existing[0].get("payment_status") in ("authorized", "captured"):
                # Received money is resolved only by the audited payment-case
                # actions; an investigation label must not hide it.
                raise CustomException(ValueError("PAYMENT_CASE_ACTION_REQUIRED"), statusCode=409,
                    uiMessage="Payments holding money must be handled as a payment case.",
                    errorCode="PAYMENT_CASE_ACTION_REQUIRED")

            self.client.table("billing_events").update({
                "event_status": "investigated",
                "payment_status": "investigated",
                "failure_reason": f"[INVESTIGATED] {note[:1500]}",
                "completed_at": now,
            }).eq("id", entityId).execute()

        elif entityType == "webhook_event":
            existing = (
                self.client.table("WebhookEvents")
                .select("razorpayEventId, status")
                .eq("razorpayEventId", entityId)
                .limit(1)
                .execute()
                .data
            )
            if not existing:
                raise ValueError(f"Webhook event {entityId} not found")

            self.client.table("WebhookEvents").update({
                "status": "investigated",
                "errorMessage": f"[INVESTIGATED] {note[:1500]}",
            }).eq("razorpayEventId", entityId).execute()

        else:
            raise ValueError(
                f"Unsupported entity type: {entityType}. "
                f"Supported: payment_attempt, webhook_event"
            )

        self._auditLog(
            adminUserId, "reconciliation.marked_investigated",
            status="INVESTIGATED",
            metadata={
                "entityType": entityType,
                "entityId": entityId,
                "note": note[:500],
            },
        )

        logger.info(
            f"Marked {entityType} {entityId} as investigated by {adminUserId}"
        )
        return {
            "entityType": entityType,
            "entityId": entityId,
            "status": "investigated",
            "investigatedAt": now,
        }

    def _auditLog(self, userId: str, eventType: str, **kwargs) -> None:
        """
        Insert an audit row into the unified billing ledger.

        Args:
            userId: The user or admin ID.
            eventType: The event type identifier.
            **kwargs: status and metadata fields.
        """
        try:
            status = kwargs.pop("status", None)
            metadata = kwargs.pop("metadata", None) or {}
            metadata.update(kwargs)
            BillingEventService(self.client).log_event(
                user_id=userId,
                event_type=eventType,
                event_status=status,
                category="reconciliation",
                metadata=metadata if metadata else None,
            )
        except Exception as e:
            logger.error(
                f"billing_events insert failed for {userId}, event {eventType}: {e}"
            )
