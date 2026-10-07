"""PostgreSQL persistence for durable notification delivery."""

__all__ = [
    "NotificationDeliveryRepository",
    "getNotificationDeliveryRepository",
]


import os
import json
from api.services.subscriptions.paymentValidationService import parseUtc

import psycopg2
from psycopg2.extras import Json, RealDictCursor


_TERMINAL_STATUSES = {
    "DELIVERED",
    "BOUNCED",
    "BLOCKED",
    "FAILED",
    "CANCELLED",
}

# Server-selected template versions for the manual monthly billing types.
# Template IDs/branding are deployment configuration; a missing configured
# template surfaces an operator error at dispatch, never a trial-template
# substitution.
_BILLING_NOTIFICATION_TEMPLATE_VERSIONS = {
    "monthly_renewal_ready": "1",
    "monthly_renewal_reminder": "1",
    "monthly_subscription_expired": "1",
    "payment_receipt": "1",
    "monthly_cancellation_confirmation": "1",
    "subscription_refund_initiated": "1",
    "subscription_refund_processed": "1",
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
        return self._enqueue(
            notificationType="trial_expiry_warning",
            templateVersion="1",
            userId=userId,
            subscriptionId=subscriptionId,
            periodEnd=periodEnd,
            dedupeKey=dedupeKey,
            metadata=metadata,
        )

    def enqueueBillingNotification(
        self,
        userId: str,
        subscriptionId: str | None,
        notificationType: str,
        dedupeKey: str,
        periodEnd: str,
        metadata: dict,
    ) -> tuple[dict, bool]:
        """Idempotently enqueue a committed billing notification intent.

        Uses the same dedupe-key conflict guard as trial expiry; the logical
        identity (lifecycle + cycle + milestone or payment/receipt id) is
        the caller's dedupeKey, so replayed bridges create no duplicates.
        """
        if notificationType not in _BILLING_NOTIFICATION_TEMPLATE_VERSIONS:
            raise ValueError(
                f"Unsupported billing notification type: {notificationType}"
            )
        return self._enqueue(
            notificationType=notificationType,
            templateVersion=_BILLING_NOTIFICATION_TEMPLATE_VERSIONS[notificationType],
            userId=userId,
            subscriptionId=subscriptionId,
            periodEnd=periodEnd,
            dedupeKey=dedupeKey,
            metadata=metadata,
        )

    def _enqueue(
        self,
        notificationType: str,
        templateVersion: str,
        userId: str,
        subscriptionId: str | None,
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
                    values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (dedupe_key) do nothing
                    returning *
                    """,
                    (
                        notificationType,
                        templateVersion,
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
                        limit 1 for update
                        """,
                        (dedupeKey,),
                    )
                    row = cursor.fetchone()
                    if row is not None and notificationType != 'trial_expiry_warning':
                        old = row.get('metadata_json') or {}
                        if isinstance(old, str):
                            old = json.loads(old)
                        mutable = (row.get('user_id') == userId and str(row.get('subscription_id')) == str(subscriptionId)
                            and not row.get('submission_started_at') and not row.get('provider_message_id')
                            and row.get('last_error_code') != 'AMBIGUOUS_SEND'
                            and (row.get('status') in ('PENDING', 'RETRY_PENDING', 'SENDING')
                                 or (row.get('status') == 'CANCELLED' and row.get('last_error_code')
                                     in ('SUBSCRIPTION_NOT_ELIGIBLE', 'OBSOLETE_INVOICE', 'RENEWAL_DECLINED'))))
                        if mutable and (old != metadata or parseUtc(row.get('period_end')) != parseUtc(periodEnd)
                                        or row.get('status') == 'CANCELLED'):
                            cursor.execute('''update public.notification_deliveries
                                set metadata_json=%s, period_end=%s, payload_version=payload_version+1,
                                    status='PENDING', next_attempt_at=now(), terminal_at=null,
                                    lease_owner=null, lease_expires_at=null, last_error_code=null,
                                    updated_at=now()
                                where id=%s returning *''', (Json(metadata),periodEnd,row['id']))
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
        payloadVersion: int | None = None,
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
            """ + self._versionPredicate(payloadVersion),
            (messageId, acceptedAt, acceptedAt, deliveryId, leaseOwner) + self._versionParameters(payloadVersion),
        )

    def scheduleRetry(
        self,
        deliveryId: str,
        leaseOwner: str,
        errorCode: str,
        nextAttemptAt: str,
        nextReconcileAt: str | None = None,
        payloadVersion: int | None = None,
    ) -> bool:
        return self._write(
            """
            update public.notification_deliveries
            set status = 'RETRY_PENDING',
                submission_started_at = case when %s = 'AMBIGUOUS_SEND'
                    then submission_started_at else null end,
                last_error_code = %s,
                -- A payment hold is not a delivery attempt: return the claim's count.
                attempt_count = case when %s = 'PAYMENT_HOLD' and attempt_count > 0
                    then attempt_count - 1 else attempt_count end,
                next_attempt_at = %s,
                next_reconcile_at = %s,
                lease_owner = null,
                lease_expires_at = null,
                updated_at = now()
            where id = %s and status = 'SENDING' and lease_owner = %s
            """ + self._versionPredicate(payloadVersion),
            (
                errorCode,
                errorCode,
                errorCode,
                nextAttemptAt,
                nextReconcileAt,
                deliveryId,
                leaseOwner,
            ) + self._versionParameters(payloadVersion),
        )

    def markTerminal(
        self,
        deliveryId: str,
        status: str,
        errorCode: str | None = None,
        providerStatus: str | None = None,
        deliveredAt: str | None = None,
        leaseOwner: str | None = None,
        payloadVersion: int | None = None,
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
            where {predicate}{self._versionPredicate(payloadVersion)}
            """,
            (
                normalizedStatus,
                errorCode,
                providerStatus,
                deliveredAt,
                *predicateParameters,
            ) + self._versionParameters(payloadVersion),
        )

    @staticmethod
    def _versionPredicate(version):
        return '' if version is None else ' and payload_version=%s and claimed_payload_version=%s'

    @staticmethod
    def _versionParameters(version):
        return () if version is None else (version,version)

    def upsertBillingRevision(self, revision) -> str:
        row, _ = self.enqueueBillingNotification(revision.userId, revision.subscriptionId,
            revision.notificationType, revision.dedupeKey, revision.periodEnd, revision.metadata)
        return str(row['id'])

    def authorizeBillingSubmission(self, deliveryId, leaseOwner, payloadVersion) -> bool:
        return self.authorizeBillingSubmissionResult(deliveryId, leaseOwner, payloadVersion) == 'AUTHORIZED'

    def authorizeBillingSubmissionResult(self, deliveryId, leaseOwner, payloadVersion) -> str:
        """Fence the immutable provider submission under the financial owner lock.

        Returns AUTHORIZED, HELD (otherwise eligible solicitation for a cycle
        with unresolved received money; retry later) or REJECTED. Once
        committed, possibly submitted payloads cannot be revised. No network
        IO occurs inside this transaction; crashed submissions reconcile by tag.
        """
        from api.services.billing.manualBillingRepository import ManualBillingRepository
        from api.services.notifications.billingNotificationService import (
            _SOLICITATION_TYPES, isBillingNotificationEligible)
        repository = ManualBillingRepository(self.connectionFactory)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id from public.notification_deliveries where id=%s',(deliveryId,))
                identity = cursor.fetchone()
                if not identity or not identity['user_id']: return 'REJECTED'
                repository._lockUser(cursor,identity['user_id'])
                subscription = repository._canonical(cursor,identity['user_id'])
                cursor.execute('select * from public.notification_deliveries where id=%s for update',(deliveryId,))
                row = cursor.fetchone()
                if (not row or row['status'] != 'SENDING' or row['lease_owner'] != leaseOwner
                        or row['payload_version'] != payloadVersion
                        or row['claimed_payload_version'] != payloadVersion
                        or row.get('submission_started_at') or row.get('last_error_code') == 'AMBIGUOUS_SEND'
                        or str(row.get('subscription_id')) != str(subscription['id'])): return 'REJECTED'
                row['metadata_json'] = repository._json(row.get('metadata_json'))
                invoiceId = row['metadata_json'].get('invoiceId')
                invoice = None
                if invoiceId:
                    cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s for update',
                                   (invoiceId,identity['user_id']))
                    invoice = cursor.fetchone()
                cursor.execute('select clock_timestamp() as observed_at')
                from api.services.subscriptions.paymentValidationService import parseUtc
                observedAt = parseUtc(cursor.fetchone()['observed_at'])
                if row.get('lease_expires_at') and parseUtc(row['lease_expires_at']) <= observedAt: return 'REJECTED'
                subscription['billing_state'] = repository._json(subscription.get('billing_state'))
                held = (row['notification_type'] in _SOLICITATION_TYPES
                    and repository._unresolvedCycleCaptureLocked(cursor, subscription, parseUtc(row.get('period_end'))))
                snapshot = {'subscription':subscription,'invoice':invoice,'unresolvedOwnedCapture':held}
                if not isBillingNotificationEligible(row,snapshot,observedAt):
                    # A hold never revives a message whose own window or facts have lapsed.
                    if held and isBillingNotificationEligible(row,{**snapshot,'unresolvedOwnedCapture':False},observedAt):
                        return 'HELD'
                    return 'REJECTED'
                cursor.execute('update public.notification_deliveries set submission_started_at=clock_timestamp() where id=%s',(deliveryId,))
                return 'AUTHORIZED'
        return repository._run(operation)

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
