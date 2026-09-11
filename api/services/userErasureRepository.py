"""PostgreSQL persistence for durable user-erasure requests."""

__all__ = [
    "ERASURE_STEP_NAMES",
    "UserErasureRepository",
    "getUserErasureRepository",
]


import datetime
import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from api.adminErrors import AdminApiError


ERASURE_STEP_NAMES = (
    "revoke_access",
    "inventory",
    "stop_billing",
    "delete_storage",
    "delete_transient_state",
    "delete_auth_identity",
    "delete_database_data",
    "verify_and_finalize",
)

REQUEST_SELECT = """
    id, target_user_id, subject_fingerprint, requested_by, idempotency_key,
    status, last_error_code, attempt_count, created_at, updated_at, started_at,
    completed_at, next_retry_at, lease_expires_at, resource_manifest
"""


def _defaultConnection():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(databaseUrl, application_name="nubrix-user-erasure")


class UserErasureRepository:
    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or _defaultConnection

    def findByIdempotency(self, idempotencyKey: str) -> dict | None:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    select {REQUEST_SELECT}
                    from public.user_erasure_requests
                    where idempotency_key = %s
                    limit 1
                    """,
                    (idempotencyKey,),
                )
                row = cursor.fetchone()
                return dict(row) if row is not None else None
        finally:
            connection.close()

    def createRequest(
        self,
        userId: str,
        subjectFingerprint: str,
        adminId: str,
        idempotencyKey: str,
        reason: str | None,
    ) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (userId,),
                )
                cursor.execute(
                    """
                    select "userId", "isBanned", "bannedAt", "bannedBy",
                           "banReason"
                    from public."Users"
                    where "userId" = %s
                    limit 1
                    for update
                    """,
                    (userId,),
                )
                if cursor.fetchone() is None:
                    raise AdminApiError(404, "User not found")

                cursor.execute(
                    """
                    select id, status
                    from public.user_erasure_requests
                    where target_user_id = %s
                      and status <> 'COMPLETED'
                    limit 1
                    for update
                    """,
                    (userId,),
                )
                if cursor.fetchone() is not None:
                    raise AdminApiError(
                        409, "User already has an active erasure request"
                    )

                cursor.execute(
                    """
                    update public."Users"
                    set "isBanned" = true,
                        "bannedAt" = case
                            when not "isBanned" or "bannedAt" is null then now()
                            else "bannedAt"
                        end,
                        "bannedBy" = case
                            when not "isBanned" or "bannedBy" is null then %s
                            else "bannedBy"
                        end,
                        "banReason" = case
                            when not "isBanned" then %s
                            else coalesce(%s, "banReason")
                        end
                    where "userId" = %s
                    """,
                    (adminId, reason, reason, userId),
                )
                if int(cursor.rowcount or 0) != 1:
                    raise RuntimeError("erasure subject disappeared while locked")
                cursor.execute(
                    'delete from public."Sessions" where "userId" = %s',
                    (userId,),
                )

                cursor.execute(
                    f"""
                    insert into public.user_erasure_requests (
                        target_user_id,
                        subject_fingerprint,
                        requested_by,
                        idempotency_key,
                        reason
                    )
                    values (%s, %s, %s, %s, %s)
                    returning {REQUEST_SELECT}
                    """,
                    (
                        userId,
                        subjectFingerprint,
                        adminId,
                        idempotencyKey,
                        reason,
                    ),
                )
                request = dict(cursor.fetchone())

                cursor.executemany(
                    """
                    insert into public.user_erasure_steps (request_id, step_name)
                    values (%s, %s)
                    on conflict (request_id, step_name) do nothing
                    """,
                    [(request["id"], stepName) for stepName in ERASURE_STEP_NAMES],
                )
                cursor.execute(
                    """
                    update public.subscriptions
                    set erasure_pending = true,
                        auto_renew_enabled = false,
                        updated_at = now()
                    where user_id = %s
                    """,
                    (userId,),
                )
                cursor.execute(
                    """
                    update public.admin_free_trial_extensions
                    set credit_sync_status = 'CANCELLED', updated_at = now()
                    where user_id = %s and credit_sync_status = 'PENDING'
                    """,
                    (userId,),
                )
            connection.commit()
            return request
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def getRequest(self, requestId: str) -> dict | None:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    select {REQUEST_SELECT}
                    from public.user_erasure_requests
                    where id = %s
                    limit 1
                    """,
                    (requestId,),
                )
                request = cursor.fetchone()
                if request is None:
                    return None
                cursor.execute(
                    """
                    select step_name, status, attempt_count, last_error_code,
                           started_at, completed_at, next_retry_at
                    from public.user_erasure_steps
                    where request_id = %s
                    order by id
                    """,
                    (requestId,),
                )
                result = dict(request)
                result["steps"] = [dict(step) for step in cursor.fetchall()]
                return result
        finally:
            connection.close()

    def claimRequest(
        self, requestId: str, workerId: str, leaseSeconds: int
    ) -> dict | None:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    update public.user_erasure_requests
                    set status = 'IN_PROGRESS',
                        worker_id = %s,
                        lease_expires_at = now() + (%s * interval '1 second'),
                        started_at = coalesce(started_at, now()),
                        updated_at = now(),
                        attempt_count = attempt_count + 1,
                        next_retry_at = null,
                        last_error_code = null
                    where id = %s
                      and status <> 'COMPLETED'
                      and (
                        status = 'PENDING'
                        or (
                            status = 'PARTIALLY_FAILED'
                            and next_retry_at is not null
                            and next_retry_at <= now()
                        )
                        or (
                            status = 'IN_PROGRESS'
                            and (
                                lease_expires_at is null
                                or lease_expires_at <= now()
                            )
                        )
                      )
                    returning {REQUEST_SELECT}
                    """,
                    (workerId, leaseSeconds, requestId),
                )
                row = cursor.fetchone()
                if row is None:
                    connection.rollback()
                    return None
                cursor.execute(
                    """
                    select step_name, status, attempt_count, last_error_code,
                           started_at, completed_at, next_retry_at
                    from public.user_erasure_steps
                    where request_id = %s
                    order by id
                    """,
                    (requestId,),
                )
                result = dict(row)
                result["steps"] = [dict(step) for step in cursor.fetchall()]
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def listClaimable(self, limit: int = 100) -> list[str]:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select id
                    from public.user_erasure_requests
                    where (
                        status = 'PENDING'
                        or (
                            status = 'PARTIALLY_FAILED'
                            and next_retry_at is not null
                            and next_retry_at <= now()
                        )
                        or (
                            status = 'IN_PROGRESS'
                            and lease_expires_at <= now()
                        )
                    )
                    order by created_at
                    limit %s
                    """,
                    (limit,),
                )
                return [str(row[0]) for row in cursor.fetchall()]
        finally:
            connection.close()

    def startStep(self, requestId: str, stepName: str) -> None:
        self._execute(
            """
            update public.user_erasure_steps
            set status = 'IN_PROGRESS', attempt_count = attempt_count + 1,
                started_at = now(), completed_at = null, last_error_code = null,
                next_retry_at = null
            where request_id = %s and step_name = %s
              and status not in ('COMPLETED', 'SKIPPED', 'RETAINED')
            """,
            (requestId, stepName),
        )

    def completeStep(
        self,
        requestId: str,
        stepName: str,
        details: dict | None = None,
        status: str = "COMPLETED",
    ) -> None:
        self._execute(
            """
            update public.user_erasure_steps
            set status = %s, details = %s, completed_at = now(),
                last_error_code = null, next_retry_at = null
            where request_id = %s and step_name = %s
            """,
            (status, Json(details or {}), requestId, stepName),
        )

    def failStep(
        self,
        requestId: str,
        stepName: str,
        errorCode: str,
        maxAttempts: int,
    ) -> bool:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    select attempt_count
                    from public.user_erasure_steps
                    where request_id = %s and step_name = %s
                    for update
                    """,
                    (requestId, stepName),
                )
                row = cursor.fetchone()
                attempts = int(row[0] if row else 1)
                nextRetry = None
                if attempts < maxAttempts:
                    delaySeconds = min(3600, 30 * (2 ** max(0, attempts - 1)))
                    nextRetry = datetime.datetime.now(
                        datetime.timezone.utc
                    ) + datetime.timedelta(seconds=delaySeconds)
                cursor.execute(
                    """
                    update public.user_erasure_steps
                    set status = 'FAILED', last_error_code = %s,
                        last_error_at = now(), next_retry_at = %s
                    where request_id = %s and step_name = %s
                    """,
                    (errorCode, nextRetry, requestId, stepName),
                )
                cursor.execute(
                    """
                    update public.user_erasure_requests
                    set status = 'PARTIALLY_FAILED', last_error_code = %s,
                        next_retry_at = %s, lease_expires_at = null,
                        worker_id = null, updated_at = now()
                    where id = %s
                    """,
                    (errorCode, nextRetry, requestId),
                )
            connection.commit()
            return nextRetry is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def inventory(self, userId: str) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'select "projectId" from public."Projects" '
                    'where "ownerUserId" = %s',
                    (userId,),
                )
                projectIds = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute(
                    'select id from public."Workspaces" where "ownerId" = %s',
                    (userId,),
                )
                workspaceIds = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    select razorpay_customer_id, razorpay_token_id
                    from public.subscriptions
                    where user_id = %s
                    """,
                    (userId,),
                )
                billingCredentials = [
                    {"customerId": row[0], "tokenId": row[1]}
                    for row in cursor.fetchall()
                    if row[0] and row[1]
                ]
                return {
                    "projectIds": projectIds,
                    "workspaceIds": workspaceIds,
                    "billingCredentials": billingCredentials,
                }
        finally:
            connection.close()

    def freezeBilling(self, userId: str) -> None:
        self._execute(
            """
            update public.subscriptions
            set erasure_pending = true, auto_renew_enabled = false,
                razorpay_token_id = null, renewal_due_at = null,
                updated_at = now()
            where user_id = %s
            """,
            (userId,),
        )

    def saveInventory(
        self, requestId: str, projectIds: list[str], workspaceIds: list[str]
    ) -> None:
        self._execute(
            """
            update public.user_erasure_requests
            set resource_manifest = %s, updated_at = now()
            where id = %s and status <> 'COMPLETED'
            """,
            (
                Json(
                    {
                        "projectIds": [str(value) for value in projectIds],
                        "workspaceIds": [str(value) for value in workspaceIds],
                    }
                ),
                requestId,
            ),
        )

    def deleteDatabaseData(
        self, requestId: str, userId: str, projectIds: list[str]
    ) -> None:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (userId,),
                )
                if self._tableExists(cursor, "Invoices"):
                    cursor.execute(
                        """
                        update public."Invoices"
                        set "userId" = null, subscription_id = null,
                            metadata_json = '{}'::jsonb
                        where "userId" = %s
                        """,
                        (userId,),
                    )
                if self._tableExists(cursor, "billing_events"):
                    cursor.execute(
                        """
                        update public.billing_events
                        set user_id = null, subscription_id = null,
                            metadata_json = '{}'::jsonb,
                            failure_reason = null
                        where user_id = %s
                        """,
                        (userId,),
                    )
                if self._tableExists(cursor, "WebhookEvents"):
                    cursor.execute(
                        """
                        update public."WebhookEvents"
                        set user_id = null, payload = '{}'::jsonb
                        where user_id = %s
                        """,
                        (userId,),
                    )
                if self._tableExists(cursor, "admin_audit_log"):
                    cursor.execute(
                        """
                        update public.admin_audit_log
                        set target_id = %s, details = '{}'::jsonb
                        where target_id = %s
                        """,
                        (requestId, userId),
                    )
                if projectIds and self._tableExists(cursor, "transformations"):
                    cursor.execute(
                        "delete from public.transformations where project_id = any(%s)",
                        (projectIds,),
                    )
                if self._tableExists(cursor, "message_store"):
                    cursor.execute(
                        """
                        delete from public.message_store
                        where user_id = %s or project_id = any(%s)
                        """,
                        (userId, projectIds),
                    )
                self._deleteByUser(
                    cursor,
                    "admin_free_trial_extensions",
                    "user_id",
                    userId,
                )
                self._deleteByUser(
                    cursor,
                    "admin_free_trial_reductions",
                    "user_id",
                    userId,
                )
                self._deleteByUser(cursor, "Projects", '"ownerUserId"', userId)
                self._deleteByUser(cursor, "Workspaces", '"ownerId"', userId)
                self._deleteByUser(cursor, "credit_balances", "user_id", userId)
                self._deleteByUser(
                    cursor, "credit_balances_backup_pre_019", "user_id", userId
                )
                self._deleteByUser(cursor, "subscriptions", "user_id", userId)
                self._deleteByUser(cursor, "Sessions", '"userId"', userId)
                self._deleteByUser(cursor, "Users", '"userId"', userId)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def verifyDatabaseErasure(self, userId: str) -> dict:
        checks = (
            ("Users", '"userId"'),
            ("Sessions", '"userId"'),
            ("Workspaces", '"ownerId"'),
            ("Projects", '"ownerUserId"'),
            ("subscriptions", "user_id"),
            ("credit_balances", "user_id"),
            ("credit_balances_backup_pre_019", "user_id"),
            ("message_store", "user_id"),
            ("Invoices", '"userId"'),
            ("billing_events", "user_id"),
            ("WebhookEvents", "user_id"),
            ("admin_free_trial_extensions", "user_id"),
            ("admin_free_trial_reductions", "user_id"),
        )
        residuals = {}
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                for tableName, columnName in checks:
                    if not self._tableExists(cursor, tableName):
                        continue
                    quotedTable = f'"{tableName}"'
                    cursor.execute(
                        f"select count(*) from public.{quotedTable} where {columnName} = %s",
                        (userId,),
                    )
                    count = int(cursor.fetchone()[0])
                    if count:
                        residuals[tableName] = count
            return residuals
        finally:
            connection.close()

    def cancelPendingTrialCreditSync(self, userId: str) -> int:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (userId,),
                )
                cursor.execute(
                    """
                    update public.admin_free_trial_extensions
                    set credit_sync_status = 'CANCELLED', updated_at = now()
                    where user_id = %s and credit_sync_status = 'PENDING'
                    """,
                    (userId,),
                )
                count = int(cursor.rowcount or 0)
            connection.commit()
            return count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finalizeRequest(self, requestId: str, details: dict) -> None:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update public.user_erasure_steps
                    set status = 'COMPLETED', details = %s, completed_at = now(),
                        last_error_code = null, next_retry_at = null
                    where request_id = %s and step_name = 'verify_and_finalize'
                    """,
                    (Json(details), requestId),
                )
                cursor.execute(
                    """
                    update public.user_erasure_requests
                    set status = 'COMPLETED', target_user_id = null, reason = null,
                        resource_manifest = %s, last_error_code = null,
                        next_retry_at = null, lease_expires_at = null, worker_id = null,
                        completed_at = now(), updated_at = now()
                    where id = %s
                    """,
                    (Json(details), requestId),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retryRequest(self, requestId: str) -> bool:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    update public.user_erasure_requests
                    set status = 'PENDING', next_retry_at = now(),
                        last_error_code = null, lease_expires_at = null,
                        worker_id = null, updated_at = now()
                    where id = %s and status = 'PARTIALLY_FAILED'
                    returning id
                    """,
                    (requestId,),
                )
                changed = cursor.fetchone() is not None
                if changed:
                    cursor.execute(
                        """
                        update public.user_erasure_steps
                        set status = 'PENDING', next_retry_at = null,
                            last_error_code = null
                        where request_id = %s and status = 'FAILED'
                        """,
                        (requestId,),
                    )
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _execute(self, query: str, parameters: tuple) -> None:
        connection = self.connectionFactory()
        try:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _tableExists(cursor, tableName: str) -> bool:
        cursor.execute("select to_regclass(%s)", (f'public."{tableName}"',))
        return cursor.fetchone()[0] is not None

    def _deleteByUser(
        self, cursor, tableName: str, columnName: str, userId: str
    ) -> None:
        if not self._tableExists(cursor, tableName):
            return
        cursor.execute(
            f'delete from public."{tableName}" where {columnName} = %s',
            (userId,),
        )


_userErasureRepository: UserErasureRepository | None = None


def getUserErasureRepository() -> UserErasureRepository:
    global _userErasureRepository
    if _userErasureRepository is None:
        _userErasureRepository = UserErasureRepository()
    return _userErasureRepository
