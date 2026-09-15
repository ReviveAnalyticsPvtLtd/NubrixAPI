"""Idempotent cross-system cleanup for administrator user-erasure requests."""

from __future__ import annotations

import math
import os
import time
import uuid

from api.commons import client as supabaseClient
from api.services.adminAuditService import (
    ADMIN_AUDIT_SYSTEM_ACTOR,
    getAdminAuditService,
)
from api.services.userErasureRepository import (
    ERASURE_STEP_NAMES,
    getUserErasureRepository,
)
from utils.initMethods import invalidate_data_cache
from utils.logger import logger


_ERROR_CODES = {
    "revoke_access": "ACCESS_REVOCATION_FAILED",
    "inventory": "INVENTORY_FAILED",
    "stop_billing": "BILLING_FREEZE_FAILED",
    "delete_storage": "STORAGE_DELETE_FAILED",
    "delete_transient_state": "TRANSIENT_DELETE_FAILED",
    "delete_auth_identity": "AUTH_DELETE_FAILED",
    "delete_database_data": "DATABASE_DELETE_FAILED",
    "verify_and_finalize": "ERASURE_VERIFICATION_FAILED",
}


class UserErasureExternalCleanup:
    """Supabase Auth/Storage and Redis operations kept outside DB transactions."""

    def __init__(
        self,
        client=None,
        redisClient=None,
        cacheInvalidator=None,
        razorpayClient=None,
        redisCleanupTimeoutSeconds: float | None = None,
        monotonic=None,
    ):
        self.client = client if client is not None else supabaseClient
        self.redis = redisClient if redisClient is not None else self._buildRedis()
        self.cacheInvalidator = cacheInvalidator or invalidate_data_cache
        self.razorpayClient = razorpayClient
        self.redisCleanupTimeoutSeconds = (
            float(redisCleanupTimeoutSeconds)
            if redisCleanupTimeoutSeconds is not None
            else float(
                os.environ.get(
                    "USER_ERASURE_REDIS_CLEANUP_TIMEOUT_SECONDS", "240"
                )
            )
        )
        leaseSeconds = float(os.environ.get("USER_ERASURE_LEASE_SECONDS", "300"))
        if (
            not math.isfinite(self.redisCleanupTimeoutSeconds)
            or self.redisCleanupTimeoutSeconds <= 0
            or not math.isfinite(leaseSeconds)
            or leaseSeconds <= 0
            or self.redisCleanupTimeoutSeconds >= leaseSeconds
        ):
            raise ValueError(
                "Redis cleanup timeout must be finite, positive, and shorter than lease"
            )
        self.monotonic = monotonic or time.monotonic

    @staticmethod
    def _buildRedis():
        import redis

        socketTimeout = float(
            os.environ.get("USER_ERASURE_REDIS_SOCKET_TIMEOUT_SECONDS", "10")
        )
        return redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            password=os.environ.get("REDIS_PASSWORD"),
            decode_responses=True,
            socket_connect_timeout=socketTimeout,
            socket_timeout=socketTimeout,
            health_check_interval=30,
        )

    def revokeAccess(self, userId: str) -> None:
        self.client.table("Users").update({"isBanned": True}).eq(
            "userId", userId
        ).execute()
        self.client.table("Sessions").delete().eq("userId", userId).execute()
        try:
            self.client.auth.admin.update_user_by_id(
                userId, {"ban_duration": "876000h"}
            )
        except Exception as exc:
            if not self._isMissing(exc):
                raise

    def deleteStorage(self, userId: str, projectIds: list[str]) -> dict:
        deleted = 0
        for projectId in projectIds:
            paths = self._listPaths("AnalyticsHub", projectId)
            deleted += self._removePaths("AnalyticsHub", paths)
            if self._listPaths("AnalyticsHub", projectId):
                raise RuntimeError("project storage remains")

        profilePaths = [
            path
            for path in self._listPaths("userProfileImages", "")
            if path.rsplit("/", 1)[-1].startswith(f"{userId}.")
        ]
        deleted += self._removePaths("userProfileImages", profilePaths)
        if any(
            path.rsplit("/", 1)[-1].startswith(f"{userId}.")
            for path in self._listPaths("userProfileImages", "")
        ):
            raise RuntimeError("profile storage remains")
        return {"objectsDeleted": deleted}

    def stopBilling(self, billingCredentials: list[dict]) -> dict:
        if not billingCredentials:
            return {"providerTokensDeleted": 0}
        if self.razorpayClient is None:
            import razorpay

            self.razorpayClient = razorpay.Client(
                auth=(
                    os.environ.get("RAZORPAY_KEY_ID", ""),
                    os.environ.get("RAZORPAY_KEY_SECRET", ""),
                )
            )
        deleted = 0
        for credential in billingCredentials:
            customerId = credential.get("customerId")
            tokenId = credential.get("tokenId")
            if not customerId or not tokenId:
                continue
            try:
                self.razorpayClient.token.delete(customerId, tokenId)
                deleted += 1
            except Exception as exc:
                if not self._isMissing(exc):
                    raise
        return {"providerTokensDeleted": deleted}

    def _listPaths(self, bucketName: str, prefix: str) -> list[str]:
        bucket = self.client.storage.from_(bucketName)
        discovered: list[str] = []
        pending = [prefix]
        while pending:
            current = pending.pop()
            offset = 0
            while True:
                rows = bucket.list(
                    path=current,
                    options={"limit": 1000, "offset": offset, "sortBy": {"column": "name", "order": "asc"}},
                ) or []
                for row in rows:
                    name = row.get("name")
                    if not name:
                        continue
                    path = f"{current}/{name}" if current else name
                    if row.get("id") is None and not row.get("metadata"):
                        pending.append(path)
                    else:
                        discovered.append(path)
                if len(rows) < 1000:
                    break
                offset += len(rows)
        return discovered

    def _removePaths(self, bucketName: str, paths: list[str]) -> int:
        bucket = self.client.storage.from_(bucketName)
        for start in range(0, len(paths), 1000):
            bucket.remove(paths[start : start + 1000])
        return len(paths)

    def deleteTransientState(self, userId: str, projectIds: list[str]) -> dict:
        deadline = self.monotonic() + self.redisCleanupTimeoutSeconds

        def ensureWithinDeadline() -> None:
            if self.monotonic() >= deadline:
                raise TimeoutError("Redis cleanup deadline exceeded")

        logger.info(
            "User erasure transient cleanup started — projects={}",
            len(projectIds),
        )
        for projectId in projectIds:
            self.cacheInvalidator(projectId)

        exactKeys = sorted({
            f"credits:v3:{userId}",
            *(f"semaphore:{projectId}" for projectId in projectIds),
        })
        prefixes = tuple(
            prefix
            for projectId in projectIds
            for prefix in (
                f"{projectId}::",
                f"transformation-preview:{projectId}:",
            )
        )

        ensureWithinDeadline()
        deleted = int(self.redis.unlink(*exactKeys) or 0)

        def scanKeys():
            cursor = 0
            while True:
                ensureWithinDeadline()
                cursor, batch = self.redis.scan(cursor=cursor, count=500)
                ensureWithinDeadline()
                for key in batch:
                    yield key
                if int(cursor) == 0:
                    return

        keys = set()
        for key in scanKeys():
            if key.startswith(prefixes):
                keys.add(key)
        ensureWithinDeadline()
        logger.info(
            "User erasure transient cleanup scan completed — keys={}",
            len(keys),
        )
        keysList = list(keys)
        for start in range(0, len(keysList), 500):
            ensureWithinDeadline()
            deleted += int(
                self.redis.unlink(*keysList[start : start + 500]) or 0
            )
        ensureWithinDeadline()
        if self.redis.exists(*exactKeys):
            raise RuntimeError("transient state remains")
        ensureWithinDeadline()
        logger.info(
            "User erasure transient cleanup completed — keys={}",
            deleted,
        )
        return {"keysDeleted": deleted}

    def deleteAuthIdentity(self, userId: str) -> None:
        try:
            self.client.auth.admin.delete_user(userId)
        except Exception as exc:
            if not self._isMissing(exc):
                raise

    def verifyExternalErasure(self, userId: str, projectIds: list[str]) -> dict:
        for projectId in projectIds:
            if self._listPaths("AnalyticsHub", projectId):
                raise RuntimeError("project storage remains")
        if any(
            path.rsplit("/", 1)[-1].startswith(f"{userId}.")
            for path in self._listPaths("userProfileImages", "")
        ):
            raise RuntimeError("profile storage remains")
        try:
            result = self.client.auth.admin.get_user_by_id(userId)
            if getattr(result, "user", None) is not None:
                raise RuntimeError("auth identity remains")
        except Exception as exc:
            if not self._isMissing(exc):
                raise
        return {"externalResiduals": 0}

    @staticmethod
    def _isMissing(exc: Exception) -> bool:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        return status == 404 or "not found" in str(exc).lower()


class UserErasureTask:
    def __init__(
        self,
        repository=None,
        externalCleanup=None,
        auditService=None,
        enqueue=None,
        maxAttempts: int | None = None,
        leaseSeconds: int | None = None,
    ):
        self.repository = repository or getUserErasureRepository()
        self.external = externalCleanup or UserErasureExternalCleanup()
        self.audit = auditService or getAdminAuditService()
        self.enqueue = enqueue or self._enqueue
        self.maxAttempts = maxAttempts or int(
            os.environ.get("USER_ERASURE_MAX_ATTEMPTS", "5")
        )
        self.leaseSeconds = leaseSeconds or int(
            os.environ.get("USER_ERASURE_LEASE_SECONDS", "300")
        )

    @staticmethod
    def _enqueue(requestId: str) -> None:
        from nubrix.triggers.celery import runUserErasure

        runUserErasure.delay(requestId)

    def execute(self, requestId: str) -> dict:
        workerId = str(uuid.uuid4())
        request = self.repository.claimRequest(
            requestId, workerId, self.leaseSeconds
        )
        if request is None:
            return {"requestId": requestId, "status": "SKIPPED"}

        userId = request.get("target_user_id")
        if not userId:
            return {"requestId": requestId, "status": request.get("status")}

        inventory = self.repository.inventory(userId)
        storedInventory = request.get("resource_manifest") or {}
        projectIds = list(
            inventory.get("projectIds") or storedInventory.get("projectIds") or []
        )
        workspaceIds = list(
            inventory.get("workspaceIds")
            or storedInventory.get("workspaceIds")
            or []
        )
        if inventory.get("projectIds") or inventory.get("workspaceIds"):
            self.repository.saveInventory(requestId, projectIds, workspaceIds)
        completed = {
            step["step_name"]
            for step in request.get("steps", [])
            if step.get("status") in {"COMPLETED", "SKIPPED", "RETAINED"}
        }

        handlers = {
            "revoke_access": lambda: self.external.revokeAccess(userId),
            "inventory": lambda: {
                "projects": len(projectIds),
                "workspaces": len(workspaceIds),
            },
            "stop_billing": lambda: self._stopBilling(
                userId, inventory.get("billingCredentials") or []
            ),
            "delete_storage": lambda: self.external.deleteStorage(
                userId, projectIds
            ),
            "delete_transient_state": lambda: self._deleteTransientState(
                userId, projectIds
            ),
            "delete_auth_identity": lambda: self.external.deleteAuthIdentity(userId),
            "delete_database_data": lambda: self.repository.deleteDatabaseData(
                requestId, userId, projectIds
            ),
            "verify_and_finalize": lambda: self._verifyAndFinalize(
                requestId, userId, projectIds
            ),
        }

        for stepName in ERASURE_STEP_NAMES:
            if stepName in completed:
                continue
            self.repository.startStep(requestId, stepName)
            try:
                details = handlers[stepName]() or {}
                if stepName != "verify_and_finalize":
                    self.repository.completeStep(requestId, stepName, details)
            except Exception:
                errorCode = _ERROR_CODES[stepName]
                retryScheduled = self.repository.failStep(
                    requestId, stepName, errorCode, self.maxAttempts
                )
                if not retryScheduled:
                    self.audit.record(
                        action="user.erasure.failed",
                        targetType="user_erasure_request",
                        targetId=requestId,
                        changedFields=["status", "last_error_code"],
                        outcome="failed",
                        actorEmail=ADMIN_AUDIT_SYSTEM_ACTOR,
                        details={
                            "status": "PARTIALLY_FAILED",
                            "step": stepName,
                            "errorCode": errorCode,
                        },
                    )
                logger.error(
                    "User erasure step failed: request={}, step={}, code={}",
                    requestId,
                    stepName,
                    errorCode,
                )
                return {"requestId": requestId, "status": "PARTIALLY_FAILED"}

        self.audit.record(
            action="user.erasure.complete",
            targetType="user_erasure_request",
            targetId=requestId,
            changedFields=["status", "target_user_id", "reason"],
            outcome="success",
            actorEmail=ADMIN_AUDIT_SYSTEM_ACTOR,
            details={"status": "COMPLETED"},
        )
        return {"requestId": requestId, "status": "COMPLETED"}

    def _verifyAndFinalize(
        self, requestId: str, userId: str, projectIds: list[str]
    ) -> dict:
        self.external.deleteTransientState(userId, projectIds)
        databaseResiduals = self.repository.verifyDatabaseErasure(userId)
        if any(databaseResiduals.values()):
            raise RuntimeError("database residuals remain")
        externalResiduals = self.external.verifyExternalErasure(userId, projectIds)
        details = {"databaseResiduals": 0, **externalResiduals}
        self.repository.finalizeRequest(requestId, details)
        return details

    def _deleteTransientState(self, userId: str, projectIds: list[str]) -> dict:
        cancelled = self.repository.cancelPendingTrialCreditSync(userId)
        details = self.external.deleteTransientState(userId, projectIds) or {}
        return {**details, "trialCreditSyncsCancelled": cancelled}

    def _stopBilling(self, userId: str, billingCredentials: list[dict]) -> dict:
        details = self.external.stopBilling(billingCredentials)
        self.repository.freezeBilling(userId)
        return details

    def sweep(self, limit: int = 100) -> dict:
        requestIds = self.repository.listClaimable(limit)
        for requestId in requestIds:
            self.enqueue(str(requestId))
        return {"queued": len(requestIds)}


__all__ = ["UserErasureExternalCleanup", "UserErasureTask"]
