import datetime
import json

from loguru import logger

from api.adminErrors import AdminApiError
from api.adminModels import (
    AdminSubscriptionPatch,
    AdminUserAccessPatch,
    AdminUserPatch,
)
from api.services.adminAuthService import AdminContext
from api.services.adminUserAccessRepository import AdminUserAccessRestoreError
from api.services.subscriptions.subscriptionFieldUtils import (
    mapBillingModeToPlanType,
    normalizeDomainList,
)


ADMIN_USER_FIELDS = (
    "userId", "email", "fullName", "phoneNumber", "profileImage",
    "onboarded", "currentWorkspaceId", "companyName", "role", "profileBio",
    "usage", "industryType", "companySize", "country", "goals", "source",
    "isBanned", "bannedAt", "bannedBy", "banReason",
)
ADMIN_USER_SELECT = ",".join(ADMIN_USER_FIELDS)
ADMIN_SUBSCRIPTION_FIELDS = (
    "id", "user_id", "billing_mode", "current_period_start",
    "current_period_end", "renewal_due_at", "auto_renew_enabled",
    "payment_collection_mode", "status", "default_currency",
    "subscribed_experts", "domain_count", "pending_removals",
    "pending_additions", "billing_state", "is_canonical", "renewal_opt_out",
    "cancellation_reason", "version", "plan_type", "created_at", "updated_at",
)
ADMIN_SUBSCRIPTION_SELECT = ",".join(ADMIN_SUBSCRIPTION_FIELDS)
ADMIN_SUBSCRIPTION_MUTATION_SELECT = (
    f"{ADMIN_SUBSCRIPTION_SELECT},erasure_pending"
)
ADMIN_SUBSCRIPTION_JSON_FIELDS = (
    "subscribed_experts",
    "pending_removals",
    "pending_additions",
    "billing_state",
)
ADMIN_BATCH_SIZE = 1000


def _serializeUser(row: dict) -> dict:
    result = {field: row.get(field) for field in ADMIN_USER_FIELDS}
    result["onboarded"] = bool(result["onboarded"])
    result["isBanned"] = bool(result["isBanned"])
    return result


def _serializeSubscription(row: dict) -> dict:
    result = {field: row.get(field) for field in ADMIN_SUBSCRIPTION_FIELDS}
    for field in ADMIN_SUBSCRIPTION_JSON_FIELDS:
        result[field] = json.dumps(
            result[field], separators=(",", ":"), sort_keys=True
        )
    return result


def _parseSubscribedExperts(rawValue: str) -> list[str]:
    try:
        parsed = json.loads(rawValue)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdminApiError(
            422,
            "Invalid subscription patch",
            {"subscribed_experts": "Must be a JSON array of non-empty strings"},
        ) from exc

    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise AdminApiError(
            422,
            "Invalid subscription patch",
            {"subscribed_experts": "Must be a JSON array of non-empty strings"},
        )

    experts = []
    seen = set()
    for item in parsed:
        expert = item.strip()
        if not expert:
            raise AdminApiError(
                422,
                "Invalid subscription patch",
                {"subscribed_experts": "Expert names cannot be blank"},
            )
        key = expert.casefold()
        if key not in seen:
            seen.add(key)
            experts.append(expert)

    if not experts:
        raise AdminApiError(
            422,
            "Invalid subscription patch",
            {"subscribed_experts": "At least one expert is required"},
        )
    if len(experts) > 4:
        raise AdminApiError(
            422,
            "Invalid subscription patch",
            {"subscribed_experts": "At most four experts are allowed"},
        )
    return experts


def _escapeIlikeLiteral(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _isMissingSupabaseAuthUser(exc: Exception) -> bool:
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    return status == 404 or "not found" in str(exc).casefold()


class AdminManagementService:
    def __init__(
        self,
        client=None,
        creditService=None,
        auditService=None,
        userAccessRepository=None,
    ):
        self._client = client
        self._creditService = creditService
        self._auditService = auditService
        self._userAccessRepository = userAccessRepository

    @property
    def client(self):
        if self._client is None:
            from api.commons import client
            self._client = client
        return self._client

    @property
    def creditService(self):
        if self._creditService is None:
            from api.services.credits.creditService import creditService
            self._creditService = creditService
        return self._creditService

    @property
    def auditService(self):
        if self._auditService is None:
            from api.services.adminAuditService import getAdminAuditService
            self._auditService = getAdminAuditService()
        return self._auditService

    @property
    def userAccessRepository(self):
        if self._userAccessRepository is None:
            from api.services.adminUserAccessRepository import (
                getAdminUserAccessRepository,
            )

            self._userAccessRepository = getAdminUserAccessRepository()
        return self._userAccessRepository

    def listUsers(self) -> list[dict]:
        try:
            rows = self._fetchAll("Users", ADMIN_USER_SELECT, "email")
        except Exception as exc:
            logger.error("Admin user list failed: {}", type(exc).__name__)
            raise AdminApiError(500, "Failed to list users") from exc
        users = [_serializeUser(row) for row in rows]
        return sorted(users, key=lambda user: str(user["email"]).casefold())

    def listSubscriptions(self) -> list[dict]:
        try:
            rows = self._fetchAll(
                "subscriptions", ADMIN_SUBSCRIPTION_SELECT, "id"
            )
        except Exception as exc:
            logger.error("Admin subscription list failed: {}", type(exc).__name__)
            raise AdminApiError(500, "Failed to list subscriptions") from exc
        return [_serializeSubscription(row) for row in rows]

    def _isErasurePending(self, userId: str) -> bool:
        try:
            rows = (
                self.client.table("subscriptions")
                .select("erasure_pending")
                .eq("user_id", userId)
                .execute().data
            ) or []
        except Exception as exc:
            message = str(exc).lower()
            if "erasure_pending" in message and (
                "does not exist" in message or "42703" in message
            ):
                rows = []
            else:
                raise AdminApiError(
                    500, "Failed to verify user erasure state"
                ) from exc
        if any(row.get("erasure_pending") for row in rows):
            return True
        try:
            requests = (
                self.client.table("user_erasure_requests")
                .select("id")
                .eq("target_user_id", userId)
                .neq("status", "COMPLETED")
                .execute().data
            ) or []
        except Exception as exc:
            message = str(exc).lower()
            if "user_erasure_requests" in message and (
                "does not exist" in message
                or "42p01" in message
                or "could not find" in message
                or "schema cache" in message
            ):
                return False
            raise AdminApiError(500, "Failed to verify user erasure state") from exc
        return bool(requests)

    def updateSubscription(
        self,
        subscriptionId: str,
        patch: AdminSubscriptionPatch,
        admin: AdminContext,
    ) -> dict:
        changedFields = sorted(patch.model_fields_set)
        try:
            existingRows = (
                self.client.table("subscriptions")
                .select(ADMIN_SUBSCRIPTION_MUTATION_SELECT)
                .eq("id", subscriptionId)
                .execute().data
            )
        except Exception as exc:
            self._auditSubscriptionUpdate(
                admin, subscriptionId, changedFields, "failed"
            )
            raise AdminApiError(500, "Failed to update subscription") from exc

        if not existingRows:
            self._auditSubscriptionUpdate(
                admin, subscriptionId, changedFields, "not_found"
            )
            raise AdminApiError(404, "Subscription not found")

        current = existingRows[0]
        if current.get("erasure_pending"):
            self._auditSubscriptionUpdate(
                admin, subscriptionId, changedFields, "conflict"
            )
            raise AdminApiError(409, "User erasure is in progress")
        updatePayload = patch.model_dump(include=patch.model_fields_set)
        expertsSupplied = "subscribed_experts" in patch.model_fields_set
        countSupplied = "domain_count" in patch.model_fields_set

        if countSupplied and not 1 <= updatePayload["domain_count"] <= 4:
            self._auditSubscriptionUpdate(
                admin, subscriptionId, changedFields, "invalid"
            )
            raise AdminApiError(
                422,
                "Invalid subscription patch",
                {"domain_count": "Must be between 1 and 4"},
            )

        if expertsSupplied:
            experts = _parseSubscribedExperts(updatePayload["subscribed_experts"])
            derivedCount = len(experts)
            if countSupplied and updatePayload["domain_count"] != derivedCount:
                self._auditSubscriptionUpdate(
                    admin, subscriptionId, changedFields, "invalid"
                )
                raise AdminApiError(
                    422,
                    "Invalid subscription patch",
                    {"domain_count": "Must match the subscribed expert count"},
                )
            updatePayload["subscribed_experts"] = experts
            updatePayload["domain_count"] = derivedCount
        elif countSupplied:
            currentCount = len(normalizeDomainList(current["subscribed_experts"]))
            if updatePayload["domain_count"] != currentCount:
                self._auditSubscriptionUpdate(
                    admin, subscriptionId, changedFields, "invalid"
                )
                raise AdminApiError(
                    422,
                    "Invalid subscription patch",
                    {"domain_count": "Must match the subscribed expert count"},
                )

        if "status" in patch.model_fields_set:
            updatePayload["plan_type"] = mapBillingModeToPlanType(
                current.get("billing_mode"), updatePayload["status"]
            )

        hasDurableChanges = any(
            current.get(field) != value
            for field, value in updatePayload.items()
        )
        if hasDurableChanges:
            oldVersion = current["version"]
            updatePayload["updated_at"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            updatePayload["version"] = oldVersion + 1

            try:
                updatedRows = (
                    self.client.table("subscriptions")
                    .update(updatePayload)
                    .eq("id", subscriptionId)
                    .eq("version", oldVersion)
                    .execute().data
                )
            except Exception as exc:
                self._auditSubscriptionUpdate(
                    admin, subscriptionId, changedFields, "failed"
                )
                raise AdminApiError(500, "Failed to update subscription") from exc

            if not updatedRows:
                self._auditSubscriptionUpdate(
                    admin, subscriptionId, changedFields, "conflict"
                )
                raise AdminApiError(409, "Subscription changed; reload and try again")
        else:
            updatedRows = [current]

        try:
            if expertsSupplied or countSupplied:
                creditResult = self.creditService.applyDomainCountChange(
                    userId=current["user_id"],
                    domainCount=updatePayload["domain_count"],
                    grantImmediately=False,
                )
                if (
                    isinstance(creditResult, dict)
                    and creditResult.get("applied") is False
                ):
                    raise RuntimeError("Credit domain count change was not applied")
            if "status" in patch.model_fields_set:
                (
                    self.client.table("Sessions")
                    .delete()
                    .eq("userId", current["user_id"])
                    .execute()
                )
        except Exception as exc:
            self._auditSubscriptionUpdate(
                admin, subscriptionId, changedFields, "side_effect_failed"
            )
            raise AdminApiError(500, "Failed to update subscription") from exc

        self._auditSubscriptionUpdate(
            admin, subscriptionId, changedFields, "success"
        )
        return _serializeSubscription(updatedRows[0])

    def updateUser(
        self,
        userId: str,
        patch: AdminUserPatch,
        admin: AdminContext,
    ) -> dict:
        changedFields = sorted(patch.model_fields_set)
        updatePayload = patch.model_dump(include=patch.model_fields_set)
        if "email" in updatePayload:
            updatePayload["email"] = str(updatePayload["email"]).strip().lower()

        try:
            existingRows = (
                self.client.table("Users")
                .select(ADMIN_USER_SELECT)
                .eq("userId", userId)
                .execute().data
            )
        except Exception as exc:
            self._auditUserUpdate(admin, userId, changedFields, "failed")
            raise AdminApiError(500, "Failed to update user") from exc

        if not existingRows:
            self._auditUserUpdate(admin, userId, changedFields, "not_found")
            raise AdminApiError(404, "User not found")

        existingUser = existingRows[0]
        if self._isErasurePending(userId):
            self._auditUserUpdate(admin, userId, changedFields, "conflict")
            raise AdminApiError(409, "User erasure is in progress")
        oldEmail = existingUser["email"]
        emailSupplied = "email" in updatePayload
        emailChanged = (
            emailSupplied
            and updatePayload["email"] != str(oldEmail).strip().lower()
        )

        if emailChanged:
            try:
                duplicateRows = (
                    self.client.table("Users")
                    .select("userId")
                    .ilike("email", _escapeIlikeLiteral(updatePayload["email"]))
                    .neq("userId", userId)
                    .execute().data
                )
            except Exception as exc:
                self._auditUserUpdate(admin, userId, changedFields, "failed")
                raise AdminApiError(500, "Failed to update user") from exc
            if duplicateRows:
                self._auditUserUpdate(admin, userId, changedFields, "conflict")
                raise AdminApiError(409, "A user with this email already exists")

            try:
                self.client.auth.admin.update_user_by_id(
                    userId,
                    {"email": updatePayload["email"], "email_confirm": True},
                )
            except Exception as exc:
                self._auditUserUpdate(admin, userId, changedFields, "failed")
                raise AdminApiError(500, "Failed to update user") from exc

        try:
            updatedRows = (
                self.client.table("Users")
                .update(updatePayload)
                .eq("userId", userId)
                .execute().data
            )
            if not updatedRows:
                raise RuntimeError("Users update returned no row")
        except Exception as exc:
            if emailChanged:
                try:
                    self.client.auth.admin.update_user_by_id(
                        userId,
                        {"email": oldEmail, "email_confirm": True},
                    )
                except Exception:
                    self._auditUserUpdate(
                        admin,
                        userId,
                        changedFields,
                        "compensation_failed",
                        critical=True,
                    )
            self._auditUserUpdate(admin, userId, changedFields, "failed")
            raise AdminApiError(500, "Failed to update user") from exc

        if emailSupplied:
            try:
                (
                    self.client.table("Sessions")
                    .delete()
                    .eq("userId", userId)
                    .execute()
                )
            except Exception as exc:
                self._auditUserUpdate(
                    admin, userId, changedFields, "side_effect_failed"
                )
                raise AdminApiError(500, "Failed to update user") from exc

        self._auditUserUpdate(admin, userId, changedFields, "success")
        return _serializeUser(updatedRows[0])

    def setUserAccess(
        self,
        userId: str,
        patch: AdminUserAccessPatch,
        admin: AdminContext,
    ) -> dict:
        changedFields = ["isBanned", "bannedAt", "bannedBy", "banReason"]
        try:
            existingRows = (
                self.client.table("Users")
                .select(ADMIN_USER_SELECT)
                .eq("userId", userId)
                .execute().data
            )
        except Exception as exc:
            self._auditUserAccess(
                admin, userId, patch.banned, changedFields, "failed", None
            )
            raise AdminApiError(500, "Failed to update user access") from exc

        if not existingRows:
            self._auditUserAccess(
                admin, userId, patch.banned, changedFields, "not_found", None
            )
            raise AdminApiError(404, "User not found")

        sessionsRevoked = 0
        current = existingRows[0]
        if not patch.banned:
            try:
                restored = self.userAccessRepository.restoreUserAccess(userId)
            except AdminApiError as exc:
                outcome = (
                    "conflict"
                    if exc.statusCode == 409
                    else "not_found"
                    if exc.statusCode == 404
                    else "failed"
                )
                self._auditUserAccess(
                    admin, userId, patch.banned, changedFields, outcome, None
                )
                raise
            except AdminUserAccessRestoreError as exc:
                if exc.stage == "session_revocation":
                    self._auditUserAccess(
                        admin,
                        userId,
                        patch.banned,
                        changedFields,
                        "side_effect_failed",
                        None,
                        details={"failedSideEffects": ["session_revocation"]},
                    )
                    raise AdminApiError(
                        500, "Failed to restore user access"
                    ) from exc
                self._auditUserAccess(
                    admin, userId, patch.banned, changedFields, "failed", None
                )
                raise AdminApiError(500, "Failed to update user access") from exc
            except Exception as exc:
                self._auditUserAccess(
                    admin, userId, patch.banned, changedFields, "failed", None
                )
                raise AdminApiError(500, "Failed to restore user access") from exc

        if patch.banned:
            transitioning = not bool(current.get("isBanned"))
            reason = (
                patch.reason
                if "reason" in patch.model_fields_set or transitioning
                else current.get("banReason")
            )
            updatePayload = {
                "isBanned": True,
                "bannedAt": (
                    datetime.datetime.now(datetime.timezone.utc).isoformat()
                    if transitioning or not current.get("bannedAt")
                    else current.get("bannedAt")
                ),
                "bannedBy": (
                    admin.adminId
                    if transitioning or not current.get("bannedBy")
                    else current.get("bannedBy")
                ),
                "banReason": reason,
            }
        else:
            reason = None
            updatePayload = {
                "isBanned": False,
                "bannedAt": None,
                "bannedBy": None,
                "banReason": None,
            }
        if patch.banned:
            try:
                updatedRows = (
                    self.client.table("Users")
                    .update(updatePayload)
                    .eq("userId", userId)
                    .execute().data
                )
                if not updatedRows:
                    raise RuntimeError("Users access update returned no row")
            except Exception as exc:
                self._auditUserAccess(
                    admin, userId, patch.banned, changedFields, "failed", reason
                )
                raise AdminApiError(500, "Failed to update user access") from exc
        else:
            sessionsRevoked = int(restored.get("sessionsRevoked") or 0)
            updatedRows = [restored]

        warnings = []
        failedSideEffects = []
        if patch.banned:
            try:
                revokedRows = (
                    self.client.table("Sessions")
                    .delete()
                    .eq("userId", userId)
                    .execute().data
                ) or []
                sessionsRevoked = len(revokedRows)
            except Exception:
                warnings.append("Product session revocation failed")
                failedSideEffects.append("session_revocation")

        try:
            self.client.auth.admin.update_user_by_id(
                userId,
                {"ban_duration": "876000h" if patch.banned else "none"},
            )
            supabaseAuthSynced = True
        except Exception as exc:
            supabaseAuthSynced = False
            if not _isMissingSupabaseAuthUser(exc):
                warnings.append("Supabase Auth synchronization failed")
                failedSideEffects.append("supabase_auth_sync")

        outcome = "side_effect_failed" if warnings else "success"
        self._auditUserAccess(
            admin,
            userId,
            patch.banned,
            changedFields,
            outcome,
            reason,
            details={
                "sessionsRevoked": sessionsRevoked,
                "supabaseAuthSynced": supabaseAuthSynced,
                "failedSideEffects": failedSideEffects,
            },
        )
        updated = updatedRows[0]
        return {
            "userId": updated["userId"],
            "isBanned": bool(updated.get("isBanned")),
            "bannedAt": updated.get("bannedAt"),
            "bannedBy": updated.get("bannedBy"),
            "banReason": updated.get("banReason"),
            "sessionsRevoked": sessionsRevoked,
            "supabaseAuthSynced": supabaseAuthSynced,
            "warnings": warnings,
        }

    def _fetchAll(
        self,
        tableName: str,
        selectFields: str,
        orderColumn: str,
    ) -> list[dict]:
        rows = []
        start = 0
        while True:
            batch = (
                self.client.table(tableName)
                .select(selectFields)
                .order(orderColumn)
                .range(start, start + ADMIN_BATCH_SIZE - 1)
                .execute().data
            ) or []
            rows.extend(batch)
            if len(batch) < ADMIN_BATCH_SIZE:
                return rows
            start += ADMIN_BATCH_SIZE

    def _auditUserUpdate(
        self,
        admin: AdminContext,
        userId: str,
        changedFields: list[str],
        outcome: str,
        critical: bool = False,
    ) -> None:
        event = logger.bind(
            adminId=admin.adminId,
            sessionId=admin.sessionId,
            targetType="user",
            targetId=userId,
            changedFields=changedFields,
            outcome=outcome,
        )
        if critical:
            event.critical("admin_audit")
        else:
            event.info("admin_audit")
        self._recordDurableAudit(
            "user.update", "user", userId, changedFields, outcome, admin
        )

    def _auditUserAccess(
        self,
        admin: AdminContext,
        userId: str,
        banned: bool,
        changedFields: list[str],
        outcome: str,
        reason: str | None,
        details: dict | None = None,
    ) -> None:
        action = "user.ban" if banned else "user.unban"
        logger.bind(
            adminId=admin.adminId,
            sessionId=admin.sessionId,
            targetType="user",
            targetId=userId,
            changedFields=changedFields,
            outcome=outcome,
        ).info("admin_audit")
        auditDetails = dict(details or {})
        if reason is not None:
            auditDetails["reason"] = reason
        self._recordDurableAudit(
            action,
            "user",
            userId,
            changedFields,
            outcome,
            admin,
            details=auditDetails,
        )

    def _auditSubscriptionUpdate(
        self,
        admin: AdminContext,
        subscriptionId: str,
        changedFields: list[str],
        outcome: str,
    ) -> None:
        logger.bind(
            adminId=admin.adminId,
            sessionId=admin.sessionId,
            targetType="subscription",
            targetId=subscriptionId,
            changedFields=changedFields,
            outcome=outcome,
        ).info("admin_audit")
        self._recordDurableAudit(
            "subscription.update", "subscription", subscriptionId,
            changedFields, outcome, admin,
        )

    def _recordDurableAudit(
        self,
        action: str,
        targetType: str,
        targetId: str,
        changedFields: list[str],
        outcome: str,
        admin: AdminContext,
        details: dict | None = None,
    ) -> None:
        """
        Persist the audit event alongside the log line already emitted above.

        emitLog is False because this method's callers have just written the
        structured `admin_audit` line themselves; passing True would put the
        same event into the log stream twice.

        Resolving the audit service can itself fail when Supabase credentials
        are unavailable, so the lookup is guarded too. record() already
        swallows write failures.
        """
        try:
            self.auditService.record(
                action=action,
                targetType=targetType,
                targetId=targetId,
                changedFields=changedFields,
                outcome=outcome,
                admin=admin,
                emitLog=False,
                details=details,
            )
        except Exception as exc:
            logger.error(
                "Durable admin audit unavailable for {}: {}",
                action,
                type(exc).__name__,
            )


_adminManagementService: AdminManagementService | None = None


def getAdminManagementService() -> AdminManagementService:
    global _adminManagementService
    if _adminManagementService is None:
        _adminManagementService = AdminManagementService()
    return _adminManagementService
