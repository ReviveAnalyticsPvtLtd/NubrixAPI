"""PostgreSQL persistence for one-user administrator trial reductions."""

import math
import os
from datetime import datetime, timedelta, timezone

import psycopg2
from dateutil import parser as dateparser
from psycopg2.extras import Json, RealDictCursor

from api.adminErrors import AdminApiError


REDUCTION_SELECT = """
    id, idempotency_key, request_hash, user_id, subscription_id,
    requested_by, days, reason, outcome, days_removed, previous_expiry,
    new_expiry, access_still_banned, error_code, created_at, updated_at,
    completed_at
"""


def _defaultConnection():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(
        databaseUrl, application_name="nubrix-admin-trial-reduction"
    )


def _asDatetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = dateparser.parse(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AdminTrialReductionRepository:
    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or _defaultConnection

    def createOrGetReduction(
        self,
        idempotencyKey: str,
        requestHash: str,
        userId: str,
        days: int,
        reason: str,
        adminId: str,
    ) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    select {REDUCTION_SELECT}
                    from public.admin_free_trial_reductions
                    where idempotency_key = %s
                    limit 1
                    """,
                    (idempotencyKey,),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (userId,),
                    )
                    cursor.execute(
                        """
                        select "userId" as user_id
                        from public."Users"
                        where "userId" = %s
                        limit 1
                        """,
                        (userId,),
                    )
                    if cursor.fetchone() is None:
                        raise AdminApiError(404, "User not found")
                    cursor.execute(
                        """
                        select erasure_pending
                        from public.subscriptions
                        where user_id = %s
                        order by updated_at desc, id desc
                        limit 1
                        for update
                        """,
                        (userId,),
                    )
                    subscription = cursor.fetchone()
                    if subscription is not None and bool(
                        subscription.get("erasure_pending")
                    ):
                        raise AdminApiError(409, "User erasure is in progress")
                    cursor.execute(
                        f"""
                        insert into public.admin_free_trial_reductions (
                            idempotency_key, request_hash, user_id, requested_by,
                            days, reason
                        )
                        values (%s, %s, %s, %s, %s, %s)
                        on conflict (idempotency_key) do nothing
                        returning {REDUCTION_SELECT}
                        """,
                        (
                            idempotencyKey,
                            requestHash,
                            userId,
                            adminId,
                            days,
                            reason,
                        ),
                    )
                    row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""
                        select {REDUCTION_SELECT}
                        from public.admin_free_trial_reductions
                        where idempotency_key = %s
                        limit 1
                        """,
                        (idempotencyKey,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("Trial reduction could not be loaded")
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reduceUser(
        self, reductionId: str, userId: str, days: int
    ) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (userId,),
                )
                cursor.execute(
                    f"""
                    select {REDUCTION_SELECT}
                    from public.admin_free_trial_reductions
                    where id = %s
                    limit 1
                    for update
                    """,
                    (reductionId,),
                )
                operation = cursor.fetchone()
                if operation is None:
                    raise RuntimeError("Trial reduction does not exist")
                if operation.get("outcome") != "PENDING":
                    connection.commit()
                    return dict(operation)
                if str(operation.get("user_id")) != str(userId):
                    raise RuntimeError("Trial reduction user does not match request")

                cursor.execute(
                    """
                    select "isBanned" as is_banned
                    from public."Users"
                    where "userId" = %s
                    limit 1
                    for update
                    """,
                    (userId,),
                )
                user = cursor.fetchone()
                if user is None:
                    result = self._recordOutcomeFailure(
                        cursor, reductionId, "USER_NOT_FOUND", False
                    )
                    connection.commit()
                    return result

                accessStillBanned = bool(user.get("is_banned"))
                cursor.execute(
                    """
                    select id, billing_mode, plan_type, status, version,
                           current_period_start, current_period_end,
                           erasure_pending
                    from public.subscriptions
                    where user_id = %s
                    order by updated_at desc, id desc
                    limit 1
                    for update
                    """,
                    (userId,),
                )
                subscription = cursor.fetchone()
                if subscription is None:
                    result = self._recordOutcomeFailure(
                        cursor,
                        reductionId,
                        "SUBSCRIPTION_NOT_FOUND",
                        accessStillBanned,
                    )
                    connection.commit()
                    return result

                errorCode = self.eligibilityError(dict(subscription))
                if errorCode is not None:
                    result = self._recordOutcomeFailure(
                        cursor, reductionId, errorCode, accessStillBanned
                    )
                    connection.commit()
                    return result

                cursor.execute(
                    "select clock_timestamp() as current_time"
                )
                clockRow = cursor.fetchone() or {}
                now = _asDatetime(clockRow.get("current_time"))
                if now is None:
                    raise RuntimeError("Database clock could not be loaded")

                previousExpiry = _asDatetime(
                    subscription.get("current_period_end")
                )
                if previousExpiry is None:
                    result = self._recordOutcomeFailure(
                        cursor,
                        reductionId,
                        "INVALID_TRIAL_EXPIRY",
                        accessStillBanned,
                    )
                    connection.commit()
                    return result
                if previousExpiry <= now:
                    result = self._recordOutcomeFailure(
                        cursor,
                        reductionId,
                        "FREE_TRIAL_NOT_ACTIVE",
                        accessStillBanned,
                    )
                    connection.commit()
                    return result

                newExpiry = previousExpiry - timedelta(days=days)
                if newExpiry <= now:
                    result = self._recordOutcomeFailure(
                        cursor,
                        reductionId,
                        "REDUCTION_WOULD_EXPIRE_TRIAL",
                        accessStillBanned,
                    )
                    connection.commit()
                    return result

                subscriptionVersion = int(subscription.get("version") or 0) + 1
                lifecycleSnapshot = {
                    "subscription_days_left": math.ceil(
                        (newExpiry - now).total_seconds() / 86400
                    ),
                    "calculated_at": now.isoformat(),
                    "current_period_end": newExpiry.isoformat(),
                    "status": "trial",
                }
                cursor.execute(
                    """
                    update public.subscriptions
                    set current_period_end = %s, renewal_due_at = %s,
                        billing_state = jsonb_set(
                            coalesce(billing_state, '{}'::jsonb),
                            '{lifecycle_snapshot}', %s::jsonb, true
                        ),
                        version = %s, updated_at = %s
                    where id = %s
                    """,
                    (
                        newExpiry,
                        newExpiry,
                        Json(lifecycleSnapshot),
                        subscriptionVersion,
                        now,
                        subscription["id"],
                    ),
                )
                cursor.execute(
                    f"""
                    update public.admin_free_trial_reductions
                    set subscription_id = %s, outcome = 'REDUCED',
                        days_removed = %s, previous_expiry = %s,
                        new_expiry = %s, access_still_banned = %s,
                        error_code = null, completed_at = %s, updated_at = %s
                    where id = %s and outcome = 'PENDING'
                    returning {REDUCTION_SELECT}
                    """,
                    (
                        subscription["id"],
                        days,
                        previousExpiry,
                        newExpiry,
                        accessStillBanned,
                        now,
                        now,
                        reductionId,
                    ),
                )
                result = cursor.fetchone()
                if result is None:
                    raise RuntimeError("Trial reduction result could not be recorded")
            connection.commit()
            return dict(result)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def eligibilityError(subscription: dict) -> str | None:
        if bool(subscription.get("erasure_pending")):
            return "USER_ERASURE_PENDING"
        billingMode = str(subscription.get("billing_mode") or "").lower()
        planType = str(subscription.get("plan_type") or "").lower()
        if billingMode != "none" or planType != "free":
            return "PAID_SUBSCRIPTION_NOT_ELIGIBLE"
        if str(subscription.get("status") or "").lower() != "trial":
            return "FREE_TRIAL_NOT_ACTIVE"
        return None

    @staticmethod
    def _recordOutcomeFailure(
        cursor,
        reductionId: str,
        errorCode: str,
        accessStillBanned: bool,
    ) -> dict:
        cursor.execute(
            f"""
            update public.admin_free_trial_reductions
            set outcome = 'FAILED', access_still_banned = %s,
                error_code = %s, completed_at = clock_timestamp(),
                updated_at = clock_timestamp()
            where id = %s and outcome = 'PENDING'
            returning {REDUCTION_SELECT}
            """,
            (accessStillBanned, errorCode, reductionId),
        )
        result = cursor.fetchone()
        if result is None:
            raise RuntimeError("Trial-reduction failure could not be recorded")
        return dict(result)

    def recordFailure(
        self, reductionId: str, userId: str, errorCode: str
    ) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (userId,),
                )
                cursor.execute(
                    f"""
                    update public.admin_free_trial_reductions
                    set outcome = 'FAILED',
                        access_still_banned = coalesce((
                            select "isBanned" from public."Users"
                            where "userId" = %s limit 1
                        ), false),
                        error_code = %s, completed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    where id = %s and outcome = 'PENDING'
                    returning {REDUCTION_SELECT}
                    """,
                    (userId, errorCode, reductionId),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""
                        select {REDUCTION_SELECT}
                        from public.admin_free_trial_reductions
                        where id = %s
                        limit 1
                        """,
                        (reductionId,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("Trial-reduction failure could not be recorded")
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


_adminTrialReductionRepository: AdminTrialReductionRepository | None = None


def getAdminTrialReductionRepository() -> AdminTrialReductionRepository:
    global _adminTrialReductionRepository
    if _adminTrialReductionRepository is None:
        _adminTrialReductionRepository = AdminTrialReductionRepository()
    return _adminTrialReductionRepository
