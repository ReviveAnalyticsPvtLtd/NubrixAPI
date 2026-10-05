"""Transactional repository for manual monthly billing mutations.

Every operation runs inside ONE PostgreSQL transaction on a psycopg2
connection, following the connection-factory pattern used by
notificationDeliveryRepository.py. Lock order per spec 02:
per-user advisory lock -> canonical subscription -> invoices (by id) ->
attempts/events (by id) -> credit balance. Provider network calls never
happen inside these methods.
"""

__all__ = [
    "ManualBillingRepository",
    "getManualBillingRepository",
    "defaultConnectionFactory",
]


import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from api.services.billing.manualBillingContracts import (
    CheckoutIntent,
    CoveragePeriod,
    FinalizationResult,
    RefundIntent,
    RefundQuote,
    VerifiedPaymentEvidence,
)


def defaultConnectionFactory():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(
        databaseUrl,
        application_name="nubrix-manual-billing",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    from dateutil import parser as dateparser

    try:
        parsed = dateparser.isoparse(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payloadHash(payload: dict) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _advisoryKey(userId: str) -> int:
    digest = hashlib.sha256(("manual-billing:" + str(userId)).encode("utf-8"))
    return int(digest.hexdigest()[:15], 16)


class ManualBillingRepository:
    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or defaultConnectionFactory

    # -- transaction helpers ------------------------------------------------

    def _run(self, operation):
        connection = self.connectionFactory()
        try:
            result = operation(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _lockUser(self, cursor, userId: str) -> None:
        cursor.execute("select pg_advisory_xact_lock(%s)", (_advisoryKey(userId),))

    # -- canonical shell ------------------------------------------------------

    def ensureCanonicalSubscription(self, userId: str) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                cursor.execute(
                    """
                    select id, user_id, billing_mode, status, plan_type,
                           current_period_start, current_period_end,
                           renewal_due_at, auto_renew_enabled,
                           payment_collection_mode, default_currency,
                           version, erasure_pending, is_canonical
                    from public.subscriptions
                    where user_id = %s and is_canonical = true
                    limit 1
                    """,
                    (userId,),
                )
                row = cursor.fetchone()
                if row is not None:
                    return dict(row)
                cursor.execute(
                    """
                    select id, user_id, billing_mode, status, plan_type,
                           current_period_start, current_period_end,
                           renewal_due_at, auto_renew_enabled,
                           payment_collection_mode, default_currency,
                           version, erasure_pending, is_canonical
                    from public.subscriptions
                    where user_id = %s
                    order by updated_at desc, id desc
                    limit 1
                    """,
                    (userId,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    cursor.execute(
                        """
                        update public.subscriptions
                        set is_canonical = true
                        where id = %s
                        """,
                        (existing["id"],),
                    )
                    promoted = dict(existing)
                    promoted["is_canonical"] = True
                    return promoted
                cursor.execute(
                    """
                    insert into public.subscriptions (
                        user_id, billing_mode, status, plan_type,
                        auto_renew_enabled, payment_collection_mode,
                        default_currency, is_canonical
                    )
                    values (%s, 'none', 'none', 'none', false,
                            'authenticated_checkout', 'INR', true)
                    returning id, user_id, billing_mode, status, plan_type,
                               current_period_start, current_period_end,
                               renewal_due_at, auto_renew_enabled,
                               payment_collection_mode, default_currency,
                               version, erasure_pending, is_canonical
                    """,
                    (userId,),
                )
                created = cursor.fetchone()
                return dict(created)

        return self._run(operation)

    # -- checkout intents -----------------------------------------------------

    def reserveCheckoutIntent(
        self,
        userId: str,
        purpose: str,
        requestKey: str,
        payloadHash: str,
        snapshot: dict,
    ) -> CheckoutIntent:
        manualBilling = {
            "schemaVersion": 1,
            "lifecycleId": snapshot.get("lifecycleId"),
            "cycleId": snapshot.get("cycleId"),
            "revision": snapshot.get("revision", 1),
            "purpose": purpose,
            "billingMode": snapshot.get("billingMode"),
            "domains": snapshot.get("domains", []),
            "payloadHash": payloadHash,
            "frozenAmount": snapshot.get("amount"),
            "currency": snapshot.get("currency", "INR"),
            "expiresAt": snapshot.get("expiresAt"),
            "closedAt": None,
            "closedReason": None,
        }

        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                namespaceKey = f"{purpose}:{userId}:{requestKey}"
                cursor.execute(
                    """
                    select id, user_id, subscription_id, invoice_id,
                           payment_status, provider_order_id,
                           metadata_json
                    from public.billing_events
                    where idempotency_key = %s
                      and event_category = 'payment_attempt'
                    limit 1
                    """,
                    (namespaceKey,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    storedMeta = existing.get("metadata_json") or {}
                    if isinstance(storedMeta, str):
                        try:
                            storedMeta = json.loads(storedMeta)
                        except (ValueError, TypeError):
                            storedMeta = {}
                    storedBilling = (
                        storedMeta.get("manualBilling") or {}
                        if isinstance(storedMeta, dict)
                        else {}
                    )
                    if storedBilling.get("payloadHash") != payloadHash:
                        raise ValueError(
                            "IDEMPOTENCY_CONFLICT: same request key with a "
                            "different payload"
                        )
                    return self._intentFromAttemptRow(
                        existing, storedMeta, userId, purpose
                    )

                subscriptionId = snapshot.get("subscriptionId")
                invoiceId = snapshot.get("invoiceId")
                attemptId = snapshot.get("attemptId") or str(uuid.uuid4())
                cursor.execute(
                    """
                    insert into public.billing_events (
                        id, user_id, subscription_id, invoice_id,
                        event_category, event_type, event_status,
                        payment_attempt_type, payment_status,
                        provider, amount, currency,
                        idempotency_key, metadata_json,
                        period_start, period_end,
                        attempted_at, occurred_at
                    )
                    values (
                        %s, %s, %s, %s,
                        'payment_attempt', 'payment.attempt', 'created',
                        'authenticated_checkout', 'created',
                        'razorpay', %s, %s,
                        %s, %s,
                        %s, %s,
                        now(), now()
                    )
                    """,
                    (
                        attemptId,
                        userId,
                        subscriptionId,
                        invoiceId,
                        snapshot.get("amount"),
                        snapshot.get("currency", "INR"),
                        namespaceKey,
                        Json({"manualBilling": manualBilling}),
                        snapshot.get("periodStart"),
                        snapshot.get("periodEnd"),
                    ),
                )
                cursor.execute(
                    """
                    select id, user_id, subscription_id, invoice_id,
                           payment_status, provider_order_id,
                           metadata_json
                    from public.billing_events
                    where id = %s
                    limit 1
                    """,
                    (attemptId,),
                )
                row = cursor.fetchone()
                return self._intentFromAttemptRow(row, {"manualBilling": manualBilling}, userId, purpose)

        return self._run(operation)

    def _intentFromAttemptRow(
        self, row: dict, metadata: dict, userId: str, purpose: str
    ) -> CheckoutIntent:
        billing = (metadata or {}).get("manualBilling") or {}
        expiresAt = _utc(billing.get("expiresAt")) or _now()
        return CheckoutIntent(
            attemptId=str(row["id"]),
            invoiceId=str(row.get("invoice_id") or "") or "",
            userId=str(row.get("user_id") or userId),
            lifecycleId=str(billing.get("lifecycleId") or ""),
            purpose=purpose,
            billingMode=str(billing.get("billingMode") or ""),
            payloadHash=str(billing.get("payloadHash") or ""),
            currency=str(billing.get("currency") or "INR"),
            state=str(row.get("payment_status") or "created"),
            revision=int(billing.get("revision") or 1),
            amount=int(billing.get("frozenAmount") or 0),
            expiresAt=expiresAt,
            razorpayOrderId=row.get("provider_order_id"),
            snapshot=dict(billing),
        )

    def bindProviderOrder(self, attemptId: str, order: dict) -> CheckoutIntent:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    update public.billing_events
                    set provider_order_id = %s,
                        payment_status = 'pending_provider_ack',
                        event_status = 'pending_provider_ack'
                    where id = %s
                      and payment_status in ('created', 'pending_provider_ack')
                    returning id, user_id, subscription_id, invoice_id,
                              payment_status, provider_order_id, metadata_json
                    """,
                    (order.get("id"), attemptId),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        f"Attempt {attemptId} is not bindable (closed or missing)"
                    )
                metadata = row.get("metadata_json") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (ValueError, TypeError):
                        metadata = {}
                purpose = (
                    (metadata.get("manualBilling") or {}).get("purpose")
                    or "unknown"
                )
                return self._intentFromAttemptRow(row, metadata, row.get("user_id"), purpose)

        return self._run(operation)

    # -- finalization ---------------------------------------------------------

    def finalizeCapturedPayment(
        self, evidence: VerifiedPaymentEvidence
    ) -> FinalizationResult:
        raise NotImplementedError(
            "finalizeCapturedPayment is implemented in task 4 alongside "
            "monthly coverage activation"
        )

    def activateDueCoverage(self, userId: str, now: datetime) -> FinalizationResult:
        raise NotImplementedError(
            "activateDueCoverage is implemented in task 4 alongside "
            "monthly coverage activation"
        )

    # -- renewal opt-out ------------------------------------------------------

    def setRenewalOptOut(
        self,
        userId: str,
        optOut: bool,
        reason: str | None,
        requestKey: str,
    ) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                cursor.execute(
                    """
                    update public.subscriptions
                    set renewal_opt_out = %s,
                        cancellation_reason = case
                            when %s then coalesce(cancellation_reason, %s)
                            else null
                        end,
                        auto_renew_enabled = false
                    where user_id = %s and is_canonical = true
                    returning id, user_id, status, renewal_opt_out,
                              cancellation_reason, current_period_end
                    """,
                    (optOut, optOut, reason, userId),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        f"No canonical subscription row for user {userId}"
                    )
                return dict(row)

        return self._run(operation)

    # -- staff refunds --------------------------------------------------------

    def saveRefundQuote(self, staffId: str, quote: RefundQuote, reason: str) -> RefundQuote:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, quote.userId)
                cursor.execute(
                    """
                    insert into public.billing_events (
                        user_id, event_category, event_type, event_status,
                        amount, currency, idempotency_key, metadata_json,
                        occurred_at
                    )
                    values (
                        %s, 'reconciliation', 'refund.quote', 'QUOTED',
                        %s, %s, %s, %s, now()
                    )
                    """,
                    (
                        quote.userId,
                        quote.amount,
                        quote.currency,
                        f"refund-quote:{quote.quoteId}",
                        Json(
                            {
                                "staffId": staffId,
                                "reason": reason,
                                "caseReference": quote.caseReference,
                                "quoteId": quote.quoteId,
                                "cutoff": quote.cutoff.isoformat(),
                                "expiresAt": quote.expiresAt.isoformat(),
                                "items": list(quote.items),
                                "accessExpired": quote.accessExpired,
                                "currentAccessPreserved": quote.currentAccessPreserved,
                            }
                        ),
                    ),
                )
            return quote

        return self._run(operation)

    def reserveUnusedTimeRefund(
        self,
        quoteId: str,
        staffId: str,
        caseReference: str,
        reason: str,
        expectedAmount: int,
        requestKey: str,
    ) -> RefundIntent:
        raise NotImplementedError(
            "reserveUnusedTimeRefund is implemented in task 8 with the "
            "support refund service"
        )

    def settleRefundEvidence(self, refundIntentId: str, providerEvidence: dict) -> dict:
        raise NotImplementedError(
            "settleRefundEvidence is implemented in task 8 with the "
            "support refund service"
        )


_repository: ManualBillingRepository | None = None


def getManualBillingRepository() -> ManualBillingRepository:
    global _repository
    if _repository is None:
        _repository = ManualBillingRepository()
    return _repository