"""Durable administrator credit-reset operations.

Each target reaches its terminal outcome in one owner-locked transaction that
also holds the balance mutation, the new credit allocation event and a strict
admin_audit_log row. An audit insert failure therefore rolls back the grant.
Bulk membership is frozen when the operation is created; processing happens
in bounded, resumable batches driven by repeated idempotent requests.
"""

import hashlib
import json
import uuid

from psycopg2.extras import Json, RealDictCursor

from api.services.billing.manualBillingRepository import _utc


TERMINAL_OUTCOMES = ("RESET", "SKIPPED")
UNFINISHED_OUTCOMES = ("PENDING", "RETRYABLE_FAILED")
OPERATION_COLUMNS = (
    "id, scope, target_user_id, admin_id, admin_email, session_id, reason, "
    "idempotency_key_hash, request_fingerprint, created_at"
)
TARGET_COLUMNS = (
    "operation_id, user_id, outcome, reason_code, before_snapshot, "
    "after_snapshot, audit_id, reset_at, cache_state, updated_at"
)
_CANONICAL_ERRORS = {
    "OWNERSHIP_OR_CANONICAL_SUBSCRIPTION_MISSING": "CANONICAL_SUBSCRIPTION_MISSING",
    "AMBIGUOUS_CANONICAL_SUBSCRIPTION": "CANONICAL_SUBSCRIPTION_AMBIGUOUS",
}


class AdminCreditResetConflict(ValueError):
    """The idempotency key is bound to a different reset request."""


def hashIdempotencyKey(idempotencyKey: str) -> str:
    return hashlib.sha256(idempotencyKey.strip().encode("utf-8")).hexdigest()


def requestFingerprint(scope: str, userId: str | None, reason: str) -> str:
    canonical = json.dumps(
        {"action": "credits.reset", "version": 1, "scope": scope,
         "targetUserId": userId, "reason": reason.strip()},
        separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _jsonValue(value):
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


class AdminCreditResetRepository:
    def __init__(self, billingRepository=None):
        if billingRepository is None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            billingRepository = getManualBillingRepository()
        self.billingRepository = billingRepository

    @property
    def credits(self):
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        return ManualCreditRepository(self.billingRepository)

    def _run(self, operation):
        return self.billingRepository._run(operation)

    @staticmethod
    def _target(row):
        if row is None:
            return None
        target = dict(row)
        target["before_snapshot"] = _jsonValue(target.get("before_snapshot"))
        target["after_snapshot"] = _jsonValue(target.get("after_snapshot"))
        for key in ("operation_id", "audit_id"):
            if target.get(key) is not None:
                target[key] = str(target[key])
        return target

    # -- operations ---------------------------------------------------------

    def createOrGetOperation(self, scope, userId, reason, idempotencyKey, admin) -> dict:
        """Persist the operation and its frozen target membership atomically.

        The same admin and key replay the original operation; a different
        normalized request under that key is a conflict without mutation.
        """
        if scope not in ("individual", "all"):
            raise ValueError("INVALID_RESET_SCOPE")
        reason = reason.strip()
        keyHash = hashIdempotencyKey(idempotencyKey)
        fingerprint = requestFingerprint(scope, userId, reason)

        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""insert into public.admin_credit_reset_operations
                        (id, scope, target_user_id, admin_id, admin_email, session_id,
                         reason, idempotency_key_hash, request_fingerprint, created_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    on conflict (admin_id, idempotency_key_hash) do nothing
                    returning {OPERATION_COLUMNS}""",
                    (str(uuid.uuid4()), scope, userId, admin.adminId, admin.email,
                     admin.sessionId, reason, keyHash, fingerprint),
                )
                created = cursor.fetchone()
                if created is None:
                    cursor.execute(
                        f"""select {OPERATION_COLUMNS} from public.admin_credit_reset_operations
                        where admin_id = %s and idempotency_key_hash = %s""",
                        (admin.adminId, keyHash),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise RuntimeError("ADMIN_CREDIT_RESET_OPERATION_UNAVAILABLE")
                    if existing["request_fingerprint"] != fingerprint:
                        raise AdminCreditResetConflict("IDEMPOTENCY_KEY_CONFLICT")
                    return dict(existing)
                if scope == "individual":
                    cursor.execute(
                        """insert into public.admin_credit_reset_targets
                            (operation_id, user_id, updated_at) values (%s, %s, now())""",
                        (created["id"], userId),
                    )
                else:
                    cursor.execute(
                        """insert into public.admin_credit_reset_targets
                            (operation_id, user_id, updated_at)
                        select %s, "userId", now() from public."Users\"""",
                        (created["id"],),
                    )
                return dict(created)

        return self._run(operation)

    def getOperation(self, operationId) -> dict | None:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""select {OPERATION_COLUMNS} from public.admin_credit_reset_operations
                    where id = %s""",
                    (operationId,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None

        return self._run(operation)

    def unfinishedTargets(self, operationId, limit=100) -> list[str]:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """select user_id from public.admin_credit_reset_targets
                    where operation_id = %s and outcome in ('PENDING', 'RETRYABLE_FAILED')
                    order by user_id limit %s""",
                    (operationId, int(limit)),
                )
                return [row["user_id"] for row in cursor.fetchall()]

        return self._run(operation)

    def cachePendingTargets(self, operationId, limit=100) -> list[str]:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """select user_id from public.admin_credit_reset_targets
                    where operation_id = %s and cache_state = 'PENDING'
                    order by user_id limit %s""",
                    (operationId, int(limit)),
                )
                return [row["user_id"] for row in cursor.fetchall()]

        return self._run(operation)

    # -- per-target financial transaction ---------------------------------------

    def resetTarget(self, operationId, userId) -> dict:
        """Reset or skip one unfinished target exactly once.

        Lock order: billing owner advisory lock -> canonical subscription ->
        operation target row -> coverage/balance. Actor and reason come from
        the stored operation, never from the caller.
        """
        billing = self.billingRepository
        credits = self.credits

        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""select {OPERATION_COLUMNS} from public.admin_credit_reset_operations
                    where id = %s""",
                    (operationId,),
                )
                stored = cursor.fetchone()
                if stored is None:
                    raise LookupError("ADMIN_CREDIT_RESET_OPERATION_NOT_FOUND")
                billing._lockUser(cursor, userId)
                cursor.execute("select clock_timestamp() as current_time")
                now = _utc(cursor.fetchone()["current_time"])
                cursor.execute('select "userId" from public."Users" where "userId" = %s', (userId,))
                userExists = cursor.fetchone() is not None
                subscription, code = None, None
                if not userExists:
                    code = "USER_NOT_FOUND"
                else:
                    try:
                        subscription = billing._canonical(cursor, userId)
                    except ValueError as exc:
                        code = _CANONICAL_ERRORS.get(str(exc), "CANONICAL_SUBSCRIPTION_MISSING")
                cursor.execute(
                    f"""select {TARGET_COLUMNS} from public.admin_credit_reset_targets
                    where operation_id = %s and user_id = %s for update""",
                    (operationId, userId),
                )
                target = cursor.fetchone()
                if target is None:
                    raise LookupError("ADMIN_CREDIT_RESET_TARGET_NOT_FOUND")
                if target["outcome"] in TERMINAL_OUTCOMES:
                    return self._target(target)

                result = None
                if code is None:
                    from api.services.credits.manualCreditRepository import CreditResetIneligible
                    try:
                        result = credits.resetQuotaLocked(cursor, subscription, now)
                    except CreditResetIneligible as exc:
                        code = exc.code
                if result is None:
                    cursor.execute("select * from public.credit_balances where user_id = %s", (userId,))
                    before, after = credits.creditSnapshot(cursor.fetchone()), None
                    changed = []
                else:
                    before, after, changed = result["before"], result["after"], result["changedFields"]
                outcome = "RESET" if result is not None else "SKIPPED"
                preserved = (after or before or {}).get("topupTokens")
                auditId = str(uuid.uuid4())
                details = {
                    "operationId": str(stored["id"]), "scope": stored["scope"],
                    "reason": stored["reason"], "reasonCode": code,
                    "before": before, "after": after, "topupTokensPreserved": preserved,
                }
                cursor.execute(
                    """insert into public.admin_audit_log
                        (id, admin_id, admin_email, session_id, actor_type, action,
                         target_type, target_id, changed_fields, details, outcome, created_at)
                    values (%s, %s, %s, %s, 'admin', 'credits.reset', 'user', %s, %s, %s, %s, %s)""",
                    (auditId, str(stored["admin_id"]), stored["admin_email"], str(stored["session_id"]),
                     userId, Json(changed), Json(details), outcome, now),
                )
                cursor.execute(
                    f"""update public.admin_credit_reset_targets
                    set outcome = %s, reason_code = %s, before_snapshot = %s, after_snapshot = %s,
                        audit_id = %s, reset_at = %s, cache_state = %s, updated_at = %s
                    where operation_id = %s and user_id = %s
                      and outcome in ('PENDING', 'RETRYABLE_FAILED')
                    returning {TARGET_COLUMNS}""",
                    (outcome, code, Json(before) if before is not None else None,
                     Json(after) if after is not None else None, auditId,
                     now if outcome == "RESET" else None,
                     "PENDING" if outcome == "RESET" else "NOT_APPLICABLE", now,
                     operationId, userId),
                )
                updated = cursor.fetchone()
                if updated is None:
                    raise RuntimeError("ADMIN_CREDIT_RESET_TARGET_CHANGED")
                return self._target(updated)

        return self._run(operation)

    def recordRetryableFailure(self, operationId, userId, reasonCode) -> bool:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """update public.admin_credit_reset_targets
                    set outcome = 'RETRYABLE_FAILED', reason_code = %s, updated_at = now()
                    where operation_id = %s and user_id = %s
                      and outcome in ('PENDING', 'RETRYABLE_FAILED')
                    returning user_id""",
                    (reasonCode, operationId, userId),
                )
                return cursor.fetchone() is not None

        return self._run(operation)

    def markCacheInvalidated(self, operationId, userId) -> bool:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """update public.admin_credit_reset_targets
                    set cache_state = 'INVALIDATED', updated_at = now()
                    where operation_id = %s and user_id = %s
                      and outcome = 'RESET' and cache_state = 'PENDING'
                    returning user_id""",
                    (operationId, userId),
                )
                return cursor.fetchone() is not None

        return self._run(operation)

    # -- read model ---------------------------------------------------------------

    def operationView(self, operationId, afterUserId=None, limit=50) -> dict | None:
        """Summary counts from persisted target rows plus one keyset page."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"""select {OPERATION_COLUMNS} from public.admin_credit_reset_operations
                    where id = %s""",
                    (operationId,),
                )
                stored = cursor.fetchone()
                if stored is None:
                    return None
                cursor.execute(
                    """select outcome, count(*) as total from public.admin_credit_reset_targets
                    where operation_id = %s group by outcome""",
                    (operationId,),
                )
                counts = {row["outcome"]: int(row["total"]) for row in cursor.fetchall()}
                cursor.execute(
                    """select count(*) as total from public.admin_credit_reset_targets
                    where operation_id = %s and cache_state = 'PENDING'""",
                    (operationId,),
                )
                cachePending = int(cursor.fetchone()["total"])
                if afterUserId is None:
                    cursor.execute(
                        f"""select {TARGET_COLUMNS} from public.admin_credit_reset_targets
                        where operation_id = %s order by user_id limit %s""",
                        (operationId, int(limit) + 1),
                    )
                else:
                    cursor.execute(
                        f"""select {TARGET_COLUMNS} from public.admin_credit_reset_targets
                        where operation_id = %s and user_id > %s order by user_id limit %s""",
                        (operationId, afterUserId, int(limit) + 1),
                    )
                page = [self._target(row) for row in cursor.fetchall()]
                nextAfter = page[int(limit) - 1]["user_id"] if len(page) > int(limit) else None
                return {"operation": dict(stored), "counts": counts,
                        "cachePendingCount": cachePending,
                        "targets": page[: int(limit)], "nextAfterUserId": nextAfter}

        return self._run(operation)


_adminCreditResetRepository: AdminCreditResetRepository | None = None


def getAdminCreditResetRepository() -> AdminCreditResetRepository:
    global _adminCreditResetRepository
    if _adminCreditResetRepository is None:
        _adminCreditResetRepository = AdminCreditResetRepository()
    return _adminCreditResetRepository
