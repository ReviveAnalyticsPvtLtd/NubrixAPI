"""Administrator credit reset orchestration.

Resets refresh only the eligible current configured allowance. The financial
mutation, terminal target outcome and strict audit row commit together in the
repository; this service drives bounded processing, maps outcomes to the
admin error contract and repairs the per-user Redis projection after commit.
"""

import uuid

from loguru import logger

from api.adminErrors import AdminApiError
from api.adminModels import AdminCreditResetRequest
from api.services.adminAuthService import AdminContext


MAX_TARGETS_PER_REQUEST = 100
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
_UNAVAILABLE = "Credit reset is temporarily unavailable"


def _iso(value) -> str | None:
    """UTC ISO-8601 regardless of the database session time zone."""
    from api.services.billing.manualBillingRepository import _utc
    instant = _utc(value)
    return instant.isoformat() if instant is not None else None


def _validIdempotencyKey(value) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not key or len(key) > 128:
        raise AdminApiError(422, "Validation failed",
                            {"Idempotency-Key": "Must be 1-128 non-blank characters"})
    return key


def _validUserId(value) -> str:
    userId = value.strip() if isinstance(value, str) else ""
    if not userId or len(userId) > 128:
        raise AdminApiError(422, "Validation failed",
                            {"userId": "Must be 1-128 non-blank characters"})
    return userId


class AdminCreditResetService:
    def __init__(self, repository=None, creditService=None):
        self._repository = repository
        self._creditService = creditService

    @property
    def repository(self):
        if self._repository is None:
            from api.services.adminCreditResetRepository import getAdminCreditResetRepository
            self._repository = getAdminCreditResetRepository()
        return self._repository

    @property
    def creditService(self):
        if self._creditService is None:
            from api.services.credits.creditService import creditService
            self._creditService = creditService
        return self._creditService

    # -- public operations ----------------------------------------------------

    def resetUser(self, userId: str, request: AdminCreditResetRequest,
                  idempotencyKey: str, admin: AdminContext) -> dict:
        userId = _validUserId(userId)
        key = _validIdempotencyKey(idempotencyKey)
        operation = self._createOrGet("individual", userId, request.reason, key, admin)
        operationId = str(operation["id"])
        self._process(operationId, limit=1)
        self._repairCache(operationId)
        view = self._view(operationId)
        target = view["targets"][0]
        errors = {"operationId": operationId}
        if target["reasonCode"]:
            errors["reasonCode"] = target["reasonCode"]
        if target["outcome"] == "RESET":
            return view
        if target["outcome"] == "SKIPPED" and target["reasonCode"] == "USER_NOT_FOUND":
            raise AdminApiError(404, "User not found", errors)
        if target["outcome"] == "SKIPPED":
            raise AdminApiError(409, "Credit reset was not applied", errors)
        raise AdminApiError(503, _UNAVAILABLE, errors)

    def resetAll(self, request: AdminCreditResetRequest, idempotencyKey: str,
                 admin: AdminContext) -> dict:
        """Create or resume one frozen all-user operation.

        Each call processes at most MAX_TARGETS_PER_REQUEST unfinished targets;
        there is no background worker. Repeat the same request and key to
        continue. Terminal targets are never reset again.
        """
        key = _validIdempotencyKey(idempotencyKey)
        operation = self._createOrGet("all", None, request.reason, key, admin)
        operationId = str(operation["id"])
        self._process(operationId, limit=MAX_TARGETS_PER_REQUEST)
        self._repairCache(operationId)
        return self._view(operationId)

    def getOperation(self, operationId: str, afterUserId: str | None = None,
                     limit: int = DEFAULT_PAGE_SIZE) -> dict:
        """Read-only summary and keyset page; performs no cache repair."""
        try:
            operationId = str(uuid.UUID(str(operationId)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdminApiError(404, "Credit reset operation not found") from exc
        if afterUserId is not None and (not afterUserId.strip() or len(afterUserId) > 128):
            raise AdminApiError(422, "Validation failed",
                                {"afterUserId": "Must be 1-128 non-blank characters"})
        return self._view(operationId, afterUserId, max(1, min(int(limit), MAX_PAGE_SIZE)))

    # -- internals --------------------------------------------------------------

    def _createOrGet(self, scope, userId, reason, key, admin) -> dict:
        from api.services.adminCreditResetRepository import AdminCreditResetConflict
        try:
            return self.repository.createOrGetOperation(scope, userId, reason, key, admin)
        except AdminCreditResetConflict as exc:
            raise AdminApiError(409, "Idempotency key is already in use") from exc
        except Exception as exc:
            logger.error("Admin credit reset persistence failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc

    def _process(self, operationId: str, limit: int) -> None:
        try:
            pending = self.repository.unfinishedTargets(operationId, limit)
        except Exception as exc:
            logger.error("Admin credit reset target lookup failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE, {"operationId": operationId}) from exc
        for userId in pending:
            try:
                self.repository.resetTarget(operationId, userId)
            except Exception as exc:
                logger.error("Admin credit reset failed for operation={}: {}",
                             operationId, type(exc).__name__)
                try:
                    self.repository.recordRetryableFailure(
                        operationId, userId, "RESET_TRANSACTION_FAILED")
                except Exception as recordExc:
                    logger.error("Admin credit reset failure persistence failed: {}",
                                 type(recordExc).__name__)

    def _repairCache(self, operationId: str) -> None:
        """Invalidate committed resets' projections; never regrants.

        Stops at the first unavailable projection so a Redis outage cannot
        hold the request open for every remaining target; the next same-key
        POST retries what is still PENDING.
        """
        try:
            pending = self.repository.cachePendingTargets(operationId, MAX_TARGETS_PER_REQUEST)
        except Exception as exc:
            logger.warning("Admin credit reset cache lookup failed: {}", type(exc).__name__)
            return
        for userId in pending:
            if not self.creditService.invalidateCreditProjection(userId):
                break
            try:
                self.repository.markCacheInvalidated(operationId, userId)
            except Exception as exc:
                logger.warning("Admin credit reset cache progress write failed: {}",
                               type(exc).__name__)

    def _view(self, operationId: str, afterUserId=None, limit=DEFAULT_PAGE_SIZE) -> dict:
        try:
            stored = self.repository.operationView(operationId, afterUserId, limit)
        except Exception as exc:
            logger.error("Admin credit reset read failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE, {"operationId": operationId}) from exc
        if stored is None:
            raise AdminApiError(404, "Credit reset operation not found")
        return self._publicView(stored)

    @staticmethod
    def _publicView(stored: dict) -> dict:
        operation, counts = stored["operation"], stored["counts"]
        pending = counts.get("PENDING", 0)
        retryable = counts.get("RETRYABLE_FAILED", 0)
        return {
            "operationId": str(operation["id"]),
            "scope": operation["scope"],
            "status": "COMPLETED" if pending == 0 and retryable == 0 else "RUNNING",
            "requestedByAdminId": str(operation["admin_id"]),
            "reason": operation["reason"],
            "createdAt": _iso(operation["created_at"]),
            "totalTargets": sum(counts.values()),
            "resetCount": counts.get("RESET", 0),
            "skippedCount": counts.get("SKIPPED", 0),
            "pendingCount": pending,
            "retryableFailureCount": retryable,
            "cachePendingCount": stored["cachePendingCount"],
            "targets": [
                {
                    "userId": target["user_id"],
                    "outcome": target["outcome"],
                    "reasonCode": target.get("reason_code"),
                    "auditId": target.get("audit_id"),
                    "resetAt": _iso(target.get("reset_at")),
                    "before": target.get("before_snapshot"),
                    "after": target.get("after_snapshot"),
                    "cacheState": target["cache_state"],
                }
                for target in stored["targets"]
            ],
            "nextAfterUserId": stored["nextAfterUserId"],
        }


_adminCreditResetService: AdminCreditResetService | None = None


def getAdminCreditResetService() -> AdminCreditResetService:
    global _adminCreditResetService
    if _adminCreditResetService is None:
        _adminCreditResetService = AdminCreditResetService()
    return _adminCreditResetService
