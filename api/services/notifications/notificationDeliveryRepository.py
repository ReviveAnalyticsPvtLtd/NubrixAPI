"""PostgreSQL persistence for durable notification delivery."""

__all__ = [
    "NotificationDeliveryRepository",
    "getNotificationDeliveryRepository",
]


import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor


_TERMINAL_STATUSES = {
    "DELIVERED",
    "BOUNCED",
    "BLOCKED",
    "FAILED",
    "CANCELLED",
}


def _defaultConnection():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(
        databaseUrl,
        application_name="nubrix-notification-delivery",
    )


class NotificationDeliveryRepository:
    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or _defaultConnection

    def enqueueTrialExpiry(
        self,
        userId: str,
        subscriptionId: str,
        periodEnd: str,
        dedupeKey: str,
        metadata: dict,
    ) -> tuple[dict, bool]:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    insert into public.notification_deliveries (
                        notification_type,
                        template_version,
                        dedupe_key,
                        user_id,
                        subscription_id,
                        period_end,
                        metadata_json
                    )
                    values ('trial_expiry_warning', '1', %s, %s, %s, %s, %s)
                    on conflict (dedupe_key) do nothing
                    returning *
                    """,
                    (
                        dedupeKey,
                        userId,
                        subscriptionId,
                        periodEnd,
                        Json(metadata),
                    ),
                )
                row = cursor.fetchone()
                created = row is not None
                if row is None:
                    cursor.execute(
                        """
                        select *
                        from public.notification_deliveries
                        where dedupe_key = %s
                        limit 1
                        """,
                        (dedupeKey,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("notification enqueue returned no row")
            connection.commit()
            return dict(row), created
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claimDue(
        self,
        workerId: str,
        limit: int = 50,
        leaseSeconds: int = 300,
    ) -> list[dict]:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    select *
                    from public.claim_notification_deliveries(%s, %s, %s)
                    """,
                    (workerId, limit, leaseSeconds),
                )
                rows = cursor.fetchall()
            connection.commit()
            return [dict(row) for row in rows]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def markAccepted(
        self,
        deliveryId: str,
        leaseOwner: str,
        messageId: str,
        acceptedAt: str,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set status = 'ACCEPTED',
                provider = 'brevo',
                provider_message_id = %s,
                provider_status = 'request',
                accepted_at = %s,
                next_reconcile_at = %s::timestamptz + interval '5 minutes',
                lease_owner = null,
                lease_expires_at = null,
                last_error_code = null,
                updated_at = now()
            where id = %s and status = 'SENDING' and lease_owner = %s
            """,
            (messageId, acceptedAt, acceptedAt, deliveryId, leaseOwner),
        )

    def scheduleRetry(
        self,
        deliveryId: str,
        leaseOwner: str,
        errorCode: str,
        nextAttemptAt: str,
        nextReconcileAt: str | None = None,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set status = 'RETRY_PENDING',
                last_error_code = %s,
                next_attempt_at = %s,
                next_reconcile_at = %s,
                lease_owner = null,
                lease_expires_at = null,
                updated_at = now()
            where id = %s and status = 'SENDING' and lease_owner = %s
            """,
            (
                errorCode,
                nextAttemptAt,
                nextReconcileAt,
                deliveryId,
                leaseOwner,
            ),
        )

    def markTerminal(
        self,
        deliveryId: str,
        status: str,
        errorCode: str | None = None,
        providerStatus: str | None = None,
        deliveredAt: str | None = None,
        leaseOwner: str | None = None,
    ) -> bool:
        normalizedStatus = status.upper()
        if normalizedStatus not in _TERMINAL_STATUSES:
            raise ValueError(f"Unsupported terminal notification status: {status}")

        if leaseOwner is not None:
            predicate = "id = %s and status = 'SENDING' and lease_owner = %s"
            predicateParameters = (deliveryId, leaseOwner)
        else:
            predicate = "id = %s and status in ('ACCEPTED', 'RETRY_PENDING')"
            predicateParameters = (deliveryId,)

        return self._write(
            f"""
            update public.notification_deliveries
            set status = %s,
                last_error_code = %s,
                provider_status = coalesce(%s, provider_status),
                delivered_at = coalesce(%s, delivered_at),
                terminal_at = now(),
                next_reconcile_at = null,
                lease_owner = null,
                lease_expires_at = null,
                updated_at = now()
            where {predicate}
            """,
            (
                normalizedStatus,
                errorCode,
                providerStatus,
                deliveredAt,
                *predicateParameters,
            ),
        )

    def listForReconciliation(self, limit: int = 100) -> list[dict]:
        return self._readMany(
            """
            select *
            from public.notification_deliveries
            where status = 'ACCEPTED'
              and next_reconcile_at <= now()
            order by next_reconcile_at, accepted_at
            limit %s
            """,
            (limit,),
        )

    def listAmbiguous(self, limit: int = 100) -> list[dict]:
        return self._readMany(
            """
            select *
            from public.notification_deliveries
            where status = 'RETRY_PENDING'
              and last_error_code = 'AMBIGUOUS_SEND'
              and next_reconcile_at <= now()
            order by next_reconcile_at, updated_at
            limit %s
            """,
            (limit,),
        )

    def attachRecoveredMessageId(
        self,
        deliveryId: str,
        messageId: str,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set status = 'ACCEPTED',
                provider = 'brevo',
                provider_message_id = %s,
                provider_status = 'recovered',
                accepted_at = coalesce(accepted_at, now()),
                next_reconcile_at = now(),
                last_error_code = null,
                updated_at = now()
            where id = %s and status = 'RETRY_PENDING'
              and last_error_code = 'AMBIGUOUS_SEND'
            """,
            (messageId, deliveryId),
        )

    def recoverClaimedAmbiguous(
        self,
        deliveryId: str,
        leaseOwner: str,
        messageId: str,
        acceptedAt: str,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set status = 'ACCEPTED',
                provider = 'brevo',
                provider_message_id = %s,
                provider_status = 'recovered',
                accepted_at = coalesce(accepted_at, %s),
                next_reconcile_at = now(),
                last_error_code = null,
                lease_owner = null,
                lease_expires_at = null,
                updated_at = now()
            where id = %s and status = 'SENDING' and lease_owner = %s
            """,
            (messageId, acceptedAt, deliveryId, leaseOwner),
        )

    def advanceReconciliation(
        self,
        deliveryId: str,
        providerStatus: str,
        nextReconcileAt: str,
        errorCode: str | None = None,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set provider_status = %s,
                last_error_code = %s,
                next_reconcile_at = %s,
                updated_at = now()
            where id = %s and status in ('ACCEPTED', 'RETRY_PENDING')
            """,
            (providerStatus, errorCode, nextReconcileAt, deliveryId),
        )

    def cancelAndScrubUser(self, userId: str) -> int:
        return self._writeCount(
            """
            update public.notification_deliveries
            set status = case
                    when status in ('PENDING', 'RETRY_PENDING', 'SENDING')
                        then 'CANCELLED'
                    else status
                end,
                terminal_at = case
                    when status in ('PENDING', 'RETRY_PENDING', 'SENDING')
                        then now()
                    else terminal_at
                end,
                user_id = null,
                subscription_id = null,
                lease_owner = null,
                lease_expires_at = null,
                next_reconcile_at = case
                    when status in ('PENDING', 'RETRY_PENDING', 'SENDING')
                        then null
                    else next_reconcile_at
                end,
                updated_at = now()
            where user_id = %s
            """,
            (userId,),
        )

    def deleteTerminalBefore(self, cutoff: str) -> int:
        return self._writeCount(
            """
            delete from public.notification_deliveries
            where status in (
                'DELIVERED', 'BOUNCED', 'BLOCKED', 'FAILED', 'CANCELLED'
            )
              and terminal_at < %s
            """,
            (cutoff,),
        )

    def collectHealth(self, now: str) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    select
                        count(*) filter (where status = 'PENDING')::integer
                            as "pending",
                        count(*) filter (where status = 'RETRY_PENDING')::integer
                            as "retryPending",
                        count(*) filter (where status = 'ACCEPTED')::integer
                            as "acceptedUnresolved",
                        count(*) filter (
                            where status = 'SENDING' and lease_expires_at <= %s
                        )::integer as "expiredLeases",
                        count(*) filter (where status = 'DELIVERED')::integer
                            as "delivered",
                        count(*) filter (
                            where status in ('BOUNCED', 'BLOCKED', 'FAILED')
                        )::integer as "terminalFailures",
                        coalesce(max(
                            extract(epoch from (%s::timestamptz - created_at)) / 60
                        ) filter (
                            where status in ('PENDING', 'RETRY_PENDING')
                              and next_attempt_at <= %s
                        ), 0)::integer as "oldestPendingMinutes"
                    from public.notification_deliveries
                    """,
                    (now, now, now),
                )
                row = cursor.fetchone()
                return dict(row) if row is not None else {}
        finally:
            connection.close()

    def validateClaimCapability(self) -> None:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    select (
                        to_regprocedure(
                            'public.claim_notification_deliveries(text,integer,integer)'
                        ) is not null
                        and has_function_privilege(
                            current_user,
                            'public.claim_notification_deliveries(text,integer,integer)',
                            'EXECUTE'
                        )
                    ) as available
                    """
                )
                row = cursor.fetchone()
                if row is None or not row["available"]:
                    raise RuntimeError("NOTIFICATION_CLAIM_UNAVAILABLE")
        finally:
            connection.close()

    def _readMany(self, query: str, parameters: tuple) -> list[dict]:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, parameters)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def _write(self, query: str, parameters: tuple) -> bool:
        return self._writeCount(query, parameters) > 0

    def _writeCount(self, query: str, parameters: tuple) -> int:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
                changed = int(cursor.rowcount or 0)
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


_notificationDeliveryRepository: NotificationDeliveryRepository | None = None


def getNotificationDeliveryRepository() -> NotificationDeliveryRepository:
    global _notificationDeliveryRepository
    if _notificationDeliveryRepository is None:
        _notificationDeliveryRepository = NotificationDeliveryRepository()
    return _notificationDeliveryRepository
