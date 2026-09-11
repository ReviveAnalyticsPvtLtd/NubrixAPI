"""Administrator single-user free-trial reduction orchestration."""

import hashlib
import json
import uuid

from loguru import logger

from api.adminErrors import AdminApiError
from api.adminModels import AdminFreeTrialReductionRequest
from api.services.adminAuthService import AdminContext


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class AdminTrialReductionService:
    def __init__(
        self,
        repository=None,
        auditService=None,
    ):
        self._repository = repository
        self._auditService = auditService

    @property
    def repository(self):
        if self._repository is None:
            from api.services.adminTrialReductionRepository import (
                getAdminTrialReductionRepository,
            )

            self._repository = getAdminTrialReductionRepository()
        return self._repository

    @property
    def auditService(self):
        if self._auditService is None:
            from api.services.adminAuditService import getAdminAuditService

            self._auditService = getAdminAuditService()
        return self._auditService

    @staticmethod
    def requestHash(payload: AdminFreeTrialReductionRequest) -> str:
        canonical = json.dumps(
            {
                "confirmation": payload.confirmation,
                "days": payload.days,
                "reason": payload.reason,
                "userId": payload.userId,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def reduce(
        self,
        payload: AdminFreeTrialReductionRequest,
        idempotencyKey: str,
        admin: AdminContext,
    ) -> dict:
        try:
            normalizedKey = str(uuid.UUID(str(idempotencyKey)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdminApiError(422, "Invalid Idempotency-Key header") from exc

        requestHash = self.requestHash(payload)
        try:
            reduction = self.repository.createOrGetReduction(
                idempotencyKey=normalizedKey,
                requestHash=requestHash,
                userId=payload.userId,
                days=payload.days,
                reason=payload.reason,
                adminId=admin.adminId,
            )
        except AdminApiError:
            raise
        except Exception as exc:
            logger.error(
                "Admin trial-reduction persistence failed: {}",
                type(exc).__name__,
            )
            raise AdminApiError(500, "Failed to reduce free trial") from exc

        if str(reduction.get("request_hash") or "") != requestHash:
            raise AdminApiError(409, "Idempotency key is already in use")

        reductionId = str(reduction["id"])
        if reduction.get("outcome") == "PENDING":
            try:
                reduction = self.repository.reduceUser(
                    reductionId=reductionId,
                    userId=payload.userId,
                    days=payload.days,
                )
            except Exception as exc:
                logger.error(
                    "Admin trial reduction failed for userId={}: {}",
                    payload.userId,
                    type(exc).__name__,
                )
                try:
                    reduction = self.repository.recordFailure(
                        reductionId=reductionId,
                        userId=payload.userId,
                        errorCode="REDUCTION_FAILED",
                    )
                except Exception as recordExc:
                    logger.error(
                        "Admin trial-reduction failure persistence failed for "
                        "userId={}: {}",
                        payload.userId,
                        type(recordExc).__name__,
                    )
                    raise AdminApiError(
                        500, "Failed to reduce free trial"
                    ) from recordExc

        self._auditReduction(reduction, admin)
        return self._publicReduction(reduction)

    def _auditReduction(self, reduction: dict, admin: AdminContext) -> None:
        succeeded = reduction.get("outcome") == "REDUCED"
        self.auditService.record(
            action="free_trial.reduce",
            targetType="user",
            targetId=reduction.get("user_id"),
            changedFields=(
                [
                    "current_period_end",
                    "renewal_due_at",
                    "billing_state",
                    "version",
                ]
                if succeeded
                else []
            ),
            outcome="success" if succeeded else "failed",
            admin=admin,
            details={
                "reductionId": str(reduction["id"]),
                "daysRemoved": reduction.get("days_removed"),
                "errorCode": reduction.get("error_code"),
            },
        )

    @staticmethod
    def _publicReduction(reduction: dict) -> dict:
        return {
            "reductionId": str(reduction["id"]),
            "userId": str(reduction["user_id"]),
            "outcome": reduction["outcome"],
            "daysRemoved": reduction.get("days_removed"),
            "previousExpiry": _iso(reduction.get("previous_expiry")),
            "newExpiry": _iso(reduction.get("new_expiry")),
            "accessStillBanned": bool(
                reduction.get("access_still_banned")
            ),
            "errorCode": reduction.get("error_code"),
        }


_adminTrialReductionService: AdminTrialReductionService | None = None


def getAdminTrialReductionService() -> AdminTrialReductionService:
    global _adminTrialReductionService
    if _adminTrialReductionService is None:
        _adminTrialReductionService = AdminTrialReductionService()
    return _adminTrialReductionService
