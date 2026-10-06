"""PostgreSQL persistence for one-user administrator trial extensions."""

import math
import os
from datetime import datetime, timedelta, timezone

import psycopg2
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
from psycopg2.extras import Json, RealDictCursor

from api.services.credits.creditConfig import getTokenQuotaForPlan


EXTENSION_SELECT = """
    id, idempotency_key, request_hash, user_id, subscription_id,
    requested_by, days, reason, outcome, days_added, previous_expiry,
    new_expiry, credit_sync_status, credit_quota, credit_topup_tokens,
    credit_period_end, credit_generation, access_still_banned, error_code,
    created_at, updated_at, completed_at
"""


def _defaultConnection():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(
        databaseUrl, application_name="nubrix-admin-trial-extension"
    )


def _asDatetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = dateparser.parse(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def calculateTrialWindow(
    subscription: dict, now: datetime, days: int
) -> tuple[datetime, datetime]:
    """Stack a live trial from its expiry; restart an expired trial from now."""
    currentEnd = _asDatetime(subscription.get("current_period_end"))
    currentStart = _asDatetime(subscription.get("current_period_start"))
    isLive = (
        str(subscription.get("status") or "").lower() == "trial"
        and currentEnd is not None
        and currentEnd > now
    )
    if isLive:
        return currentStart or now, currentEnd + timedelta(days=days)
    return now, now + timedelta(days=days)


class AdminTrialExtensionRepository:
    def __init__(self, connectionFactory=None, freeQuotaProvider=None):
        self.connectionFactory = connectionFactory or _defaultConnection
        self.freeQuotaProvider = freeQuotaProvider or (
            lambda: getTokenQuotaForPlan("free", 1)
        )

    def createOrGetExtension(
        self,
        idempotencyKey: str,
        requestHash: str,
        userId: str,
        days: int,
        reason: str | None,
        adminId: str,
    ) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    insert into public.admin_free_trial_extensions (
                        idempotency_key, request_hash, user_id, requested_by,
                        days, reason
                    )
                    values (%s, %s, %s, %s, %s, %s)
                    on conflict (idempotency_key) do nothing
                    returning {EXTENSION_SELECT}
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
                        select {EXTENSION_SELECT}
                        from public.admin_free_trial_extensions
                        where idempotency_key = %s
                        limit 1
                        """,
                        (idempotencyKey,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("Trial extension could not be loaded")
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def extendUser(
        self, extensionId: str, userId: str, days: int, now: datetime
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
                    select {EXTENSION_SELECT}
                    from public.admin_free_trial_extensions
                    where id = %s
                    limit 1
                    for update
                    """,
                    (extensionId,),
                )
                operation = cursor.fetchone()
                if operation is None:
                    raise RuntimeError("Trial extension does not exist")
                if operation.get("outcome") != "PENDING":
                    connection.commit()
                    return dict(operation)
                if str(operation.get("user_id")) != str(userId):
                    raise RuntimeError("Trial extension user does not match request")
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
                        cursor, extensionId, "USER_NOT_FOUND", False
                    )
                    connection.commit()
                    return result

                accessStillBanned = bool(user.get("is_banned"))
                cursor.execute(
                    """
                    select id, billing_mode, plan_type, status, domain_count,
                           version, admin_credit_generation,
                           current_period_start, current_period_end,
                           erasure_pending
                    from public.subscriptions
                    where user_id = %s and is_canonical = true
                    limit 1
                    for update
                    """,
                    (userId,),
                )
                subscription = cursor.fetchone()
                if subscription is None:
                    result = self._recordOutcomeFailure(
                        cursor,
                        extensionId,
                        "SUBSCRIPTION_NOT_FOUND",
                        accessStillBanned,
                    )
                    connection.commit()
                    return result

                errorCode = self._eligibilityError(dict(subscription))
                if errorCode is not None:
                    result = self._recordOutcomeFailure(
                        cursor, extensionId, errorCode, accessStillBanned
                    )
                    connection.commit()
                    return result

                previousExpiry = _asDatetime(subscription.get("current_period_end"))
                periodStart, newExpiry = calculateTrialWindow(
                    dict(subscription), now, days
                )
                creditPeriodEnd = now + relativedelta(months=1)
                quota = int(self.freeQuotaProvider())
                domainCount = max(1, int(subscription.get("domain_count") or 1))
                subscriptionVersion = int(subscription.get("version") or 0) + 1
                creditGeneration = int(
                    subscription.get("admin_credit_generation") or 0
                ) + 1
                lifecycleSnapshot = {
                    "subscription_days_left": max(
                        0,
                        math.ceil((newExpiry - now).total_seconds() / 86400),
                    ),
                    "calculated_at": now.isoformat(),
                    "current_period_end": newExpiry.isoformat(),
                    "status": "trial",
                }
                cursor.execute(
                    """
                    update public.subscriptions
                    set billing_mode = 'none', status = 'trial', plan_type = 'free',
                        current_period_start = %s, current_period_end = %s,
                        renewal_due_at = %s, auto_renew_enabled = false,
                        payment_collection_mode = 'authenticated_checkout',
                        cancellation_reason = null,
                        billing_state = jsonb_set(
                            coalesce(billing_state, '{}'::jsonb),
                            '{lifecycle_snapshot}', %s::jsonb, true
                        ),
                        version = %s, admin_credit_generation = %s, updated_at = %s
                    where id = %s
                    """,
                    (
                        periodStart,
                        newExpiry,
                        newExpiry,
                        Json(lifecycleSnapshot),
                        subscriptionVersion,
                        creditGeneration,
                        now,
                        subscription["id"],
                    ),
                )
                cursor.execute(
                    """
                    insert into public.credit_balances (
                        user_id, subscription_id, plan_tier, domain_count,
                        monthly_token_quota, used_tokens, remaining_tokens,
                        period_start, period_end, last_reset_at, updated_at
                    )
                    values (%s, %s, 'free', %s, %s, 0, %s, %s, %s, %s, %s)
                    on conflict (user_id) do update
                    set subscription_id = excluded.subscription_id,
                        plan_tier = 'free', domain_count = excluded.domain_count,
                        monthly_token_quota = excluded.monthly_token_quota,
                        used_tokens = 0,
                        remaining_tokens = excluded.remaining_tokens,
                        period_start = excluded.period_start,
                        period_end = excluded.period_end,
                        last_reset_at = excluded.last_reset_at,
                        updated_at = excluded.updated_at
                    returning topup_tokens
                    """,
                    (
                        userId,
                        subscription["id"],
                        domainCount,
                        quota,
                        quota,
                        now,
                        creditPeriodEnd,
                        now,
                        now,
                    ),
                )
                creditRow = cursor.fetchone() or {}
                topupTokens = int(creditRow.get("topup_tokens") or 0)
                cursor.execute(
                    f"""
                    update public.admin_free_trial_extensions
                    set subscription_id = %s, outcome = 'EXTENDED',
                        days_added = %s, previous_expiry = %s,
                        new_expiry = %s, credit_sync_status = 'PENDING',
                        credit_quota = %s, credit_topup_tokens = %s,
                        credit_period_end = %s, credit_generation = %s,
                        access_still_banned = %s, error_code = null,
                        completed_at = %s, updated_at = %s
                    where id = %s and outcome = 'PENDING'
                    returning {EXTENSION_SELECT}
                    """,
                    (
                        subscription["id"],
                        days,
                        previousExpiry,
                        newExpiry,
                        quota,
                        topupTokens,
                        creditPeriodEnd,
                        creditGeneration,
                        accessStillBanned,
                        now,
                        now,
                        extensionId,
                    ),
                )
                result = cursor.fetchone()
                if result is None:
                    raise RuntimeError("Trial extension result could not be recorded")
            connection.commit()
            return dict(result)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _eligibilityError(subscription: dict) -> str | None:
        if bool(subscription.get("erasure_pending")):
            return "USER_ERASURE_PENDING"
        billingMode = str(subscription.get("billing_mode") or "").lower()
        planType = str(subscription.get("plan_type") or "").lower()
        if billingMode != "none" or planType != "free":
            return "PAID_SUBSCRIPTION_NOT_ELIGIBLE"
        if str(subscription.get("status") or "").lower() not in {
            "trial",
            "expired",
        }:
            return "FREE_TRIAL_NOT_ELIGIBLE"
        return None

    @staticmethod
    def _recordOutcomeFailure(
        cursor,
        extensionId: str,
        errorCode: str,
        accessStillBanned: bool,
    ) -> dict:
        cursor.execute(
            f"""
            update public.admin_free_trial_extensions
            set outcome = 'FAILED', credit_sync_status = 'NOT_APPLICABLE',
                access_still_banned = %s, error_code = %s,
                completed_at = now(), updated_at = now()
            where id = %s and outcome = 'PENDING'
            returning {EXTENSION_SELECT}
            """,
            (accessStillBanned, errorCode, extensionId),
        )
        result = cursor.fetchone()
        if result is None:
            raise RuntimeError("Trial-extension failure could not be recorded")
        return dict(result)

    def recordFailure(self, extensionId: str, errorCode: str) -> dict:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    update public.admin_free_trial_extensions
                    set outcome = 'FAILED', credit_sync_status = 'NOT_APPLICABLE',
                        access_still_banned = false, error_code = %s,
                        completed_at = now(), updated_at = now()
                    where id = %s and outcome = 'PENDING'
                    returning {EXTENSION_SELECT}
                    """,
                    (errorCode, extensionId),
                )
                row = cursor.fetchone()
                if row is None:
                    cursor.execute(
                        f"""
                        select {EXTENSION_SELECT}
                        from public.admin_free_trial_extensions
                        where id = %s
                        limit 1
                        """,
                        (extensionId,),
                    )
                    row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("Trial-extension failure could not be recorded")
            connection.commit()
            return dict(row)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def synchronizeCreditExtension(
        self, extensionId: str, syncCallback
    ) -> dict | None:
        """Fence, publish, and finalize one pending cache reset transactionally."""
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    select user_id, subscription_id
                    from public.admin_free_trial_extensions
                    where id = %s
                    limit 1
                    """,
                    (extensionId,),
                )
                locator = cursor.fetchone()
                if locator is None:
                    connection.commit()
                    return None

                cursor.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (locator["user_id"],),
                )
                cursor.execute(
                    """
                    select admin_credit_generation, erasure_pending,
                           billing_mode, plan_type, status
                    from public.subscriptions
                    where id = %s and user_id = %s
                    limit 1
                    for update
                    """,
                    (locator.get("subscription_id"), locator["user_id"]),
                )
                subscription = cursor.fetchone()
                cursor.execute(
                    f"""
                    select {EXTENSION_SELECT}
                    from public.admin_free_trial_extensions
                    where id = %s
                    limit 1
                    for update
                    """,
                    (extensionId,),
                )
                locked = cursor.fetchone()
                if locked is None:
                    connection.commit()
                    return None
                extension = dict(locked)
                if extension.get("credit_sync_status") != "PENDING":
                    connection.commit()
                    return extension

                eligible = (
                    subscription is not None
                    and not bool(subscription.get("erasure_pending"))
                    and str(subscription.get("billing_mode") or "").lower()
                    == "none"
                    and str(subscription.get("plan_type") or "").lower()
                    == "free"
                    and str(subscription.get("status") or "").lower() == "trial"
                )
                if not eligible:
                    status = "CANCELLED"
                elif int(subscription.get("admin_credit_generation") or 0) != int(
                    extension.get("credit_generation") or 0
                ):
                    status = "SUPERSEDED"
                else:
                    syncResult = syncCallback(extension)
                    if syncResult == "FAILED":
                        connection.commit()
                        return extension
                    status = "SYNCED" if syncResult == "APPLIED" else "SUPERSEDED"

                cursor.execute(
                    f"""
                    update public.admin_free_trial_extensions
                    set credit_sync_status = %s, updated_at = now()
                    where id = %s
                    returning {EXTENSION_SELECT}
                    """,
                    (status, extensionId),
                )
                updated = cursor.fetchone()
            connection.commit()
            return dict(updated)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def getExtensionById(self, extensionId: str) -> dict | None:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    select {EXTENSION_SELECT}
                    from public.admin_free_trial_extensions
                    where id = %s
                    limit 1
                    """,
                    (extensionId,),
                )
                row = cursor.fetchone()
                return dict(row) if row is not None else None
        finally:
            connection.close()

    def listPendingCreditSync(self, limit: int = 100) -> list[dict]:
        connection = self.connectionFactory()
        try:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""
                    select {EXTENSION_SELECT}
                    from public.admin_free_trial_extensions
                    where credit_sync_status = 'PENDING'
                    order by created_at
                    limit %s
                    """,
                    (max(1, min(int(limit), 500)),),
                )
                return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()


_adminTrialExtensionRepository: AdminTrialExtensionRepository | None = None


def getAdminTrialExtensionRepository() -> AdminTrialExtensionRepository:
    global _adminTrialExtensionRepository
    if _adminTrialExtensionRepository is None:
        _adminTrialExtensionRepository = AdminTrialExtensionRepository()
    return _adminTrialExtensionRepository
