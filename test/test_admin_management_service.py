import builtins
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.adminModels as adminModels
from api.adminErrors import AdminApiError
from api.adminModels import AdminSubscriptionPatch, AdminUserPatch
from api.services.adminAuthService import AdminContext
from api.services.adminManagementService import (
    ADMIN_USER_SELECT,
    AdminManagementService,
)


ADMIN_CONTEXT = AdminContext(
    adminId="admin-1",
    email="admin@example.com",
    name="Admin",
    sessionId="admin-session-1",
    token="secret-admin-token",
)


class RecordingAuditService:
    """Captures durable audit calls so tests never touch a real backend."""

    def __init__(self):
        self.calls = []

    def record(self, **kwargs):
        self.calls.append(kwargs)


class FakeRestoreRepository:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def restoreUserAccess(self, userId):
        self.calls.append(userId)
        user = next(
            (row for row in self.client.rows["Users"] if row["userId"] == userId),
            None,
        )
        if user is None:
            raise AdminApiError(404, "User not found")
        if any(
            row.get("user_id") == userId and row.get("erasure_pending")
            for row in self.client.rows["subscriptions"]
        ) or any(
            row.get("target_user_id") == userId
            and row.get("status") != "COMPLETED"
            for row in self.client.rows["user_erasure_requests"]
        ):
            raise AdminApiError(409, "User erasure is in progress")

        sessionsRevoked = 0
        if user.get("isBanned"):
            if self.client.failSessionDelete:
                from api.services.adminUserAccessRepository import (
                    AdminUserAccessRestoreError,
                )

                raise AdminUserAccessRestoreError("session_revocation")
            sessionsRevoked = sum(
                row.get("userId") == userId for row in self.client.rows["Sessions"]
            )
            self.client.deletes.append(("Sessions", [("userId", userId)]))
            self.client.rows["Sessions"] = [
                row for row in self.client.rows["Sessions"]
                if row.get("userId") != userId
            ]
            update = {
                "isBanned": False,
                "bannedAt": None,
                "bannedBy": None,
                "banReason": None,
            }
            user.update(update)
            self.client.updates.append(("Users", update, [("userId", userId)]))
        return {
            "userId": user["userId"],
            "isBanned": bool(user.get("isBanned")),
            "bannedAt": user.get("bannedAt"),
            "bannedBy": user.get("bannedBy"),
            "banReason": user.get("banReason"),
            "sessionsRevoked": sessionsRevoked,
        }


@pytest.fixture(autouse=True)
def stubDurableAudit():
    """
    Keep the lazily-resolved audit service off the network.

    AdminManagementService resolves getAdminAuditService() on first use, which
    would otherwise build a live Supabase client inside these unit tests.
    """
    recorder = RecordingAuditService()
    with patch(
        "api.services.adminAuditService.getAdminAuditService",
        return_value=recorder,
    ):
        yield recorder


EXPECTED_USER_FIELDS = {
    "userId", "email", "fullName", "phoneNumber", "profileImage",
    "onboarded", "currentWorkspaceId", "companyName", "role", "profileBio",
    "usage", "industryType", "companySize", "country", "goals", "source",
    "isBanned", "bannedAt", "bannedBy", "banReason",
}

EXPECTED_SUBSCRIPTION_FIELD_ORDER = (
    "id", "user_id", "billing_mode", "current_period_start",
    "current_period_end", "renewal_due_at", "auto_renew_enabled",
    "payment_collection_mode", "status", "default_currency",
    "subscribed_experts", "domain_count", "pending_removals",
    "pending_additions", "billing_state", "is_canonical", "renewal_opt_out",
    "cancellation_reason", "version", "plan_type", "created_at",
    "updated_at",
)
EXPECTED_SUBSCRIPTION_FIELDS = set(EXPECTED_SUBSCRIPTION_FIELD_ORDER)
EXPECTED_SUBSCRIPTION_SELECT = ",".join(EXPECTED_SUBSCRIPTION_FIELD_ORDER)


def userRow(userId="user-1", email="old@example.com", **overrides):
    row = {
        "userId": userId,
        "email": email,
        "fullName": "Old Name",
        "phoneNumber": "+10000000000",
        "profileImage": "avatar.png",
        "onboarded": 1,
        "currentWorkspaceId": "workspace-1",
        "companyName": "Nubrix",
        "role": "Analyst",
        "profileBio": "Profile",
        "usage": "Business",
        "industryType": "Technology",
        "companySize": "1-10",
        "country": "India",
        "goals": "Insights",
        "source": "Referral",
        "isBanned": False,
        "bannedAt": None,
        "bannedBy": None,
        "banReason": None,
        "password": "must-never-leave-storage",
        "internalFlag": "private",
    }
    row.update(overrides)
    return row


def subscriptionRow(subscriptionId="sub-1", **overrides):
    row = {
        "id": subscriptionId,
        "user_id": "user-1",
        "billing_mode": "monthly_recurring",
        "current_period_start": None,
        "current_period_end": "2026-09-01T00:00:00+00:00",
        "renewal_due_at": None,
        "auto_renew_enabled": True,
        "payment_collection_mode": "authenticated_checkout",
        "status": "active",
        "default_currency": "INR",
        "subscribed_experts": ["finance", "sales"],
        "domain_count": 2,
        "pending_removals": [],
        "pending_additions": [],
        "billing_state": {"z": 1, "lifecycle_snapshot": {}, "a": 2},
        "razorpay_customer_id": "cust-1",
        "razorpay_token_id": "token-1",
        "subscription_anchor_day": 1,
        "recurring_failures": 0,
        "cancellation_reason": None,
        "version": 7,
        "plan_type": "pro",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
        "erasure_pending": False,
        "provider_secret": "must-never-leave-storage",
    }
    row.update(overrides)
    return row


def test_erasure_pending_blocks_user_edits_unban_and_subscription_edits():
    client = FakeClient([userRow(isBanned=True)])
    client.rows["subscriptions"] = [subscriptionRow(erasure_pending=True)]
    accessPatch = getattr(adminModels, "AdminUserAccessPatch")

    with pytest.raises(AdminApiError) as userEdit:
        AdminManagementService(client=client).updateUser(
            "user-1",
            AdminUserPatch.model_validate({"fullName": "Changed"}),
            ADMIN_CONTEXT,
        )
    with pytest.raises(AdminApiError) as unban:
        AdminManagementService(
            client=client,
            userAccessRepository=FakeRestoreRepository(client),
        ).setUserAccess(
            "user-1",
            accessPatch.model_validate({"banned": False}),
            ADMIN_CONTEXT,
        )
    with pytest.raises(AdminApiError) as subscriptionEdit:
        AdminManagementService(client=client).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"status": "paused"}),
            ADMIN_CONTEXT,
        )

    for captured in (userEdit, unban, subscriptionEdit):
        assert captured.value.statusCode == 409
        assert captured.value.message == "User erasure is in progress"
    assert client.updates == []


def test_erasure_pending_still_allows_idempotent_ban_enforcement():
    client = FakeClient([userRow(isBanned=True)])
    client.rows["subscriptions"] = [subscriptionRow(erasure_pending=True)]
    accessPatch = getattr(adminModels, "AdminUserAccessPatch")

    result = AdminManagementService(client=client).setUserAccess(
        "user-1",
        accessPatch.model_validate({"banned": True}),
        ADMIN_CONTEXT,
    )

    assert result["isBanned"] is True


def test_active_erasure_ledger_blocks_unban_even_without_subscription_row():
    client = FakeClient([userRow(isBanned=True)])
    client.rows["user_erasure_requests"] = [
        {"id": "request-1", "target_user_id": "user-1", "status": "IN_PROGRESS"}
    ]
    accessPatch = getattr(adminModels, "AdminUserAccessPatch")

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(
            client=client,
            userAccessRepository=FakeRestoreRepository(client),
        ).setUserAccess(
            "user-1",
            accessPatch.model_validate({"banned": False}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 409
    assert client.updates == []


class FakeQuery:
    def __init__(self, client, tableName):
        self.client = client
        self.tableName = tableName
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.orderColumn = None
        self.rangeBounds = None
        self.selectFields = None

    def select(self, fields):
        self.operation = "select"
        self.selectFields = fields
        self.client.selects.setdefault(self.tableName, []).append(fields)
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def ilike(self, field, value):
        self.filters.append(("ilike", field, value))
        self.client.ilikePatterns.append((self.tableName, field, value))
        return self

    def neq(self, field, value):
        self.filters.append(("neq", field, value))
        return self

    def order(self, column):
        self.orderColumn = column
        return self

    def range(self, start, end):
        self.rangeBounds = (start, end)
        self.client.ranges.setdefault(self.tableName, []).append((start, end))
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = dict(payload)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def execute(self):
        if self.operation == "update" and self.tableName == "Users":
            if self.client.failNextUsersUpdate:
                self.client.failNextUsersUpdate = False
                raise RuntimeError(
                    "Users update failed: old@example.com -> new@example.com"
                )
        if self.operation == "delete" and self.tableName == "Sessions":
            if self.client.failSessionDelete:
                raise RuntimeError("session deletion failed: secret-session-value")

        matching = [
            row for row in self.client.rows[self.tableName]
            if self._matches(row)
        ]
        if (
            self.operation == "update"
            and self.tableName == "subscriptions"
            and self.client.forceEmptySubscriptionUpdate
        ):
            matching = []
        if self.orderColumn is not None:
            matching.sort(key=lambda row: str(row.get(self.orderColumn, "")))
        if self.rangeBounds is not None:
            start, end = self.rangeBounds
            matching = matching[start:end + 1]

        if self.operation == "update":
            for row in matching:
                row.update(self.payload)
            self.client.updates.append(
                (self.tableName, dict(self.payload), self._simpleFilters())
            )
        elif self.operation == "delete":
            self.client.deletes.append((self.tableName, self._simpleFilters()))
            self.client.rows[self.tableName] = [
                row for row in self.client.rows[self.tableName]
                if not self._matches(row)
            ]

        return SimpleNamespace(data=[self._project(row) for row in matching])

    def _matches(self, row):
        for operation, field, value in self.filters:
            actual = row.get(field)
            if operation == "eq" and actual != value:
                return False
            if operation == "neq" and actual == value:
                return False
            if operation == "ilike":
                if not postgresIlikeMatches(str(actual), str(value)):
                    return False
        return True

    def _simpleFilters(self):
        return [(field, value) for operation, field, value in self.filters if operation == "eq"]

    def _project(self, row):
        if self.operation != "select" or self.selectFields is None:
            return dict(row)
        fields = self.selectFields.split(",")
        return {field: row.get(field) for field in fields}


class FakeClient:
    def __init__(self, users, subscriptions=None):
        self.rows = {
            "Users": list(users),
            "subscriptions": list(subscriptions or []),
            "Sessions": [],
            "user_erasure_requests": [],
        }
        self.selects = {}
        self.ranges = {}
        self.updates = []
        self.deletes = []
        self.ilikePatterns = []
        self.failNextUsersUpdate = False
        self.failSessionDelete = False
        self.forceEmptySubscriptionUpdate = False
        self.auth = SimpleNamespace(
            admin=SimpleNamespace(update_user_by_id=MagicMock())
        )

    def table(self, tableName):
        if tableName not in self.rows:
            raise AssertionError(f"Unexpected table: {tableName}")
        return FakeQuery(self, tableName)

    def lastUpdate(self, tableName):
        return next(
            payload for table, payload, _filters in reversed(self.updates)
            if table == tableName
        )

    def deletedFilters(self, tableName):
        return next(
            filters for table, filters in reversed(self.deletes)
            if table == tableName
        )

    def lastUpdateFilters(self, tableName):
        return next(
            filters for table, _payload, filters in reversed(self.updates)
            if table == tableName
        )


class FakeBoundLogger:
    def __init__(self, logger, context):
        self.logger = logger
        self.context = context

    def info(self, message):
        self.logger.records.append(("info", message, self.context))

    def critical(self, message):
        self.logger.records.append(("critical", message, self.context))


class FakeLogger:
    def __init__(self):
        self.records = []

    def bind(self, **context):
        return FakeBoundLogger(self, context)


def postgresIlikeMatches(value, pattern):
    regexParts = []
    escaped = False
    for character in pattern:
        if escaped:
            regexParts.append(re.escape(character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "%":
            regexParts.append(".*")
        elif character == "_":
            regexParts.append(".")
        else:
            regexParts.append(re.escape(character))
    if escaped:
        raise AssertionError("Invalid trailing ILIKE escape")
    return re.fullmatch("".join(regexParts), value, re.IGNORECASE) is not None


def test_list_users_batches_and_never_selects_sensitive_fields():
    rows = [
        userRow(f"user-{index}", f"User{1001 - index:04d}@Example.com")
        for index in range(1002)
    ]
    client = FakeClient(rows)
    service = AdminManagementService(client=client)

    users = service.listUsers()

    assert len(users) == 1002
    assert all("password" not in user for user in users)
    assert client.selects["Users"] == [ADMIN_USER_SELECT, ADMIN_USER_SELECT]
    assert client.ranges["Users"] == [(0, 999), (1000, 1999)]
    assert [user["email"] for user in users] == sorted(
        (user["email"] for user in users), key=str.casefold
    )


def test_list_users_serializes_access_fields_and_normalizes_booleans():
    client = FakeClient([userRow(onboarded=0, profileBio=None)])

    users = AdminManagementService(client=client).listUsers()

    assert set(users[0]) == EXPECTED_USER_FIELDS
    assert len(users[0]) == 20
    assert users[0]["onboarded"] is False
    assert users[0]["isBanned"] is False
    assert users[0]["profileBio"] is None


def test_ban_user_sets_authoritative_state_revokes_sessions_and_syncs_auth(
    stubDurableAudit,
):
    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([userRow()])
    client.rows["Sessions"] = [
        {"id": "session-1", "userId": "user-1"},
        {"id": "session-2", "userId": "user-1"},
        {"id": "session-3", "userId": "user-2"},
    ]
    service = AdminManagementService(client=client, auditService=stubDurableAudit)

    result = service.setUserAccess(
        "user-1",
        patchModel.model_validate({"banned": True, "reason": "  Abuse  "}),
        ADMIN_CONTEXT,
    )

    update = client.lastUpdate("Users")
    assert update["isBanned"] is True
    assert update["bannedBy"] == ADMIN_CONTEXT.adminId
    assert update["banReason"] == "Abuse"
    assert update["bannedAt"] is not None
    assert client.deletedFilters("Sessions") == [("userId", "user-1")]
    assert [row["id"] for row in client.rows["Sessions"]] == ["session-3"]
    client.auth.admin.update_user_by_id.assert_called_once_with(
        "user-1", {"ban_duration": "876000h"}
    )
    assert result["isBanned"] is True
    assert result["banReason"] == "Abuse"
    assert result["sessionsRevoked"] == 2
    assert result["supabaseAuthSynced"] is True
    assert result["warnings"] == []
    assert stubDurableAudit.calls[-1]["action"] == "user.ban"
    assert stubDurableAudit.calls[-1]["details"] == {
        "reason": "Abuse",
        "sessionsRevoked": 2,
        "supabaseAuthSynced": True,
        "failedSideEffects": [],
    }


def test_ban_user_accepts_omitted_reason():
    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([userRow()])

    result = AdminManagementService(client=client).setUserAccess(
        "user-1",
        patchModel.model_validate({"banned": True}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdate("Users")["banReason"] is None
    assert result["banReason"] is None


def test_unban_user_revokes_residual_sessions_before_clearing_ban_snapshot():
    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([
        userRow(
            isBanned=True,
            bannedAt="2026-08-20T00:00:00+00:00",
            bannedBy=ADMIN_CONTEXT.adminId,
            banReason="Abuse",
        )
    ])
    client.rows["Sessions"] = [
        {"id": "old-session", "userId": "user-1"},
        {"id": "other-session", "userId": "user-2"},
    ]

    restoreRepository = FakeRestoreRepository(client)
    result = AdminManagementService(
        client=client,
        userAccessRepository=restoreRepository,
    ).setUserAccess(
        "user-1",
        patchModel.model_validate({"banned": False}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdate("Users") == {
        "isBanned": False,
        "bannedAt": None,
        "bannedBy": None,
        "banReason": None,
    }
    client.auth.admin.update_user_by_id.assert_called_once_with(
        "user-1", {"ban_duration": "none"}
    )
    assert client.deletedFilters("Sessions") == [("userId", "user-1")]
    assert [row["id"] for row in client.rows["Sessions"]] == ["other-session"]
    assert result["isBanned"] is False
    assert result["sessionsRevoked"] == 1
    assert restoreRepository.calls == ["user-1"]


def test_unban_keeps_user_banned_when_residual_session_revocation_fails(
    stubDurableAudit,
):
    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([
        userRow(
            isBanned=True,
            bannedAt="2026-08-20T00:00:00+00:00",
            bannedBy=ADMIN_CONTEXT.adminId,
            banReason="Abuse",
        )
    ])
    client.rows["Sessions"] = [{"id": "old-session", "userId": "user-1"}]
    client.failSessionDelete = True

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(
            client=client,
            auditService=stubDurableAudit,
            userAccessRepository=FakeRestoreRepository(client),
        ).setUserAccess(
            "user-1",
            patchModel.model_validate({"banned": False}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 500
    assert captured.value.message == "Failed to restore user access"
    assert client.rows["Users"][0]["isBanned"] is True
    assert client.rows["Sessions"][0]["id"] == "old-session"
    assert client.updates == []
    assert stubDurableAudit.calls[-1]["outcome"] == "side_effect_failed"
    assert stubDurableAudit.calls[-1]["details"] == {
        "failedSideEffects": ["session_revocation"]
    }


def test_ban_remains_enforced_when_native_auth_sync_fails(stubDurableAudit):
    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([userRow()])
    client.auth.admin.update_user_by_id.side_effect = RuntimeError(
        "auth backend secret must not leak"
    )

    result = AdminManagementService(
        client=client,
        auditService=stubDurableAudit,
    ).setUserAccess(
        "user-1",
        patchModel.model_validate({"banned": True}),
        ADMIN_CONTEXT,
    )

    assert client.rows["Users"][0]["isBanned"] is True
    assert result["supabaseAuthSynced"] is False
    assert result["warnings"] == ["Supabase Auth synchronization failed"]
    assert stubDurableAudit.calls[-1]["outcome"] == "side_effect_failed"
    assert stubDurableAudit.calls[-1]["details"]["failedSideEffects"] == [
        "supabase_auth_sync"
    ]
    assert "secret" not in repr(result)


def test_ban_skips_missing_native_auth_identity_without_warning(stubDurableAudit):
    class MissingAuthUserError(RuntimeError):
        status_code = 404

    patchModel = getattr(adminModels, "AdminUserAccessPatch")
    client = FakeClient([userRow()])
    client.auth.admin.update_user_by_id.side_effect = MissingAuthUserError(
        "User not found"
    )

    result = AdminManagementService(
        client=client,
        auditService=stubDurableAudit,
    ).setUserAccess(
        "user-1",
        patchModel.model_validate({"banned": True}),
        ADMIN_CONTEXT,
    )

    assert client.rows["Users"][0]["isBanned"] is True
    assert result["supabaseAuthSynced"] is False
    assert result["warnings"] == []
    assert stubDurableAudit.calls[-1]["outcome"] == "success"
    assert stubDurableAudit.calls[-1]["details"]["failedSideEffects"] == []


def test_set_user_access_returns_404_for_missing_user(stubDurableAudit):
    patchModel = getattr(adminModels, "AdminUserAccessPatch")

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(
            client=FakeClient([]),
            auditService=stubDurableAudit,
        ).setUserAccess(
            "missing-user",
            patchModel.model_validate({"banned": True}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 404
    assert captured.value.message == "User not found"
    assert stubDurableAudit.calls[-1]["outcome"] == "not_found"


def test_update_user_applies_only_fields_explicitly_supplied():
    client = FakeClient([userRow()])
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"fullName": None, "onboarded": True})

    updated = service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert client.lastUpdate("Users") == {"fullName": None, "onboarded": True}
    assert updated["fullName"] is None
    assert set(updated) == EXPECTED_USER_FIELDS


def test_update_user_returns_404_when_target_is_missing():
    client = FakeClient([])

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(client=client).updateUser(
            "missing-user",
            AdminUserPatch.model_validate({"fullName": "New Name"}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 404
    assert captured.value.message == "User not found"
    assert client.updates == []


def test_update_user_rejects_case_insensitive_duplicate_email():
    client = FakeClient([
        userRow(),
        userRow("user-2", "NEW@example.COM"),
    ])

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(client=client).updateUser(
            "user-1",
            AdminUserPatch.model_validate({"email": "New@Example.com"}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 409
    assert captured.value.message == "A user with this email already exists"
    client.auth.admin.update_user_by_id.assert_not_called()
    assert client.updates == []


@pytest.mark.parametrize(("requestedEmail", "similarEmail", "expectedPattern"), [
    (
        "first_last@example.com",
        "firstXlast@example.com",
        r"first\_last@example.com",
    ),
    (
        "sales%ops@example.com",
        "sales-anything-ops@example.com",
        r"sales\%ops@example.com",
    ),
])
def test_update_user_treats_ilike_metacharacters_as_literal_email_characters(
    requestedEmail,
    similarEmail,
    expectedPattern,
):
    client = FakeClient([
        userRow(),
        userRow("user-2", similarEmail),
    ])

    updated = AdminManagementService(client=client).updateUser(
        "user-1",
        AdminUserPatch.model_validate({"email": requestedEmail}),
        ADMIN_CONTEXT,
    )

    assert updated["email"] == requestedEmail
    assert client.ilikePatterns == [("Users", "email", expectedPattern)]


def test_email_update_user_syncs_auth_confirms_and_revokes_product_sessions():
    client = FakeClient([userRow()])
    client.rows["Sessions"] = [
        {"id": "session-1", "userId": "user-1"},
        {"id": "session-2", "userId": "user-2"},
    ]

    updated = AdminManagementService(client=client).updateUser(
        "user-1",
        AdminUserPatch.model_validate({"email": "New@Example.com"}),
        ADMIN_CONTEXT,
    )

    client.auth.admin.update_user_by_id.assert_called_once_with(
        "user-1", {"email": "new@example.com", "email_confirm": True}
    )
    assert client.deletedFilters("Sessions") == [("userId", "user-1")]
    assert updated["email"] == "new@example.com"


def test_users_failure_rolls_back_auth_email():
    client = FakeClient([userRow()])
    client.failNextUsersUpdate = True

    with pytest.raises(AdminApiError) as captured:
        AdminManagementService(client=client).updateUser(
            "user-1",
            AdminUserPatch.model_validate({"email": "new@example.com"}),
            ADMIN_CONTEXT,
        )

    assert captured.value.statusCode == 500
    assert captured.value.message == "Failed to update user"
    assert client.auth.admin.update_user_by_id.call_args_list[-1].args == (
        "user-1", {"email": "old@example.com", "email_confirm": True}
    )


def test_update_user_logs_redacted_critical_audit_when_auth_rollback_fails():
    client = FakeClient([userRow()])
    client.failNextUsersUpdate = True
    client.auth.admin.update_user_by_id.side_effect = [
        SimpleNamespace(user={"id": "user-1"}),
        RuntimeError("rollback leaked old@example.com and secret-auth-value"),
    ]
    auditLogger = FakeLogger()

    with patch("api.services.adminManagementService.logger", auditLogger):
        with pytest.raises(AdminApiError) as captured:
            AdminManagementService(client=client).updateUser(
                "user-1",
                AdminUserPatch.model_validate({"email": "new@example.com"}),
                ADMIN_CONTEXT,
            )

    assert captured.value.statusCode == 500
    critical = [record for record in auditLogger.records if record[0] == "critical"]
    assert critical == [(
        "critical",
        "admin_audit",
        {
            "adminId": "admin-1",
            "sessionId": "admin-session-1",
            "targetType": "user",
            "targetId": "user-1",
            "changedFields": ["email"],
            "outcome": "compensation_failed",
        },
    )]
    renderedLogs = repr(auditLogger.records)
    assert "old@example.com" not in renderedLogs
    assert "new@example.com" not in renderedLogs
    assert "secret-admin-token" not in renderedLogs
    assert "secret-auth-value" not in renderedLogs


def test_update_user_returns_generic_500_when_session_deletion_fails():
    client = FakeClient([userRow()])
    client.rows["Sessions"] = [{"id": "session-1", "userId": "user-1"}]
    client.failSessionDelete = True
    auditLogger = FakeLogger()

    with patch("api.services.adminManagementService.logger", auditLogger):
        with pytest.raises(AdminApiError) as captured:
            AdminManagementService(client=client).updateUser(
                "user-1",
                AdminUserPatch.model_validate({"email": "new@example.com"}),
                ADMIN_CONTEXT,
            )

    assert captured.value.statusCode == 500
    assert captured.value.message == "Failed to update user"
    assert any(
        record[2]["outcome"] == "side_effect_failed"
        for record in auditLogger.records
    )
    assert "secret-session-value" not in repr(auditLogger.records)


def test_update_user_retry_reattempts_session_deletion_without_repeating_auth_change():
    client = FakeClient([userRow()])
    client.rows["Sessions"] = [
        {"id": "session-1", "userId": "user-1"},
        {"id": "session-2", "userId": "user-2"},
    ]
    client.failSessionDelete = True
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"email": "new@example.com"})

    with pytest.raises(AdminApiError) as firstAttempt:
        service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert firstAttempt.value.statusCode == 500
    assert client.auth.admin.update_user_by_id.call_count == 1
    assert any(row["userId"] == "user-1" for row in client.rows["Sessions"])

    client.failSessionDelete = False
    updated = service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert updated["email"] == "new@example.com"
    assert client.auth.admin.update_user_by_id.call_count == 1
    assert client.deletedFilters("Sessions") == [("userId", "user-1")]
    assert all(row["userId"] != "user-1" for row in client.rows["Sessions"])


def test_update_user_writes_redacted_success_audit_with_sorted_field_names():
    client = FakeClient([userRow()])
    auditLogger = FakeLogger()

    with patch("api.services.adminManagementService.logger", auditLogger):
        AdminManagementService(client=client).updateUser(
            "user-1",
            AdminUserPatch.model_validate({
                "role": "Owner",
                "fullName": "New Name",
            }),
            ADMIN_CONTEXT,
        )

    assert auditLogger.records == [(
        "info",
        "admin_audit",
        {
            "adminId": "admin-1",
            "sessionId": "admin-session-1",
            "targetType": "user",
            "targetId": "user-1",
            "changedFields": ["fullName", "role"],
            "outcome": "success",
        },
    )]
    renderedLogs = repr(auditLogger.records)
    assert "New Name" not in renderedLogs
    assert "Owner" not in renderedLogs
    assert "secret-admin-token" not in renderedLogs


def test_list_subscriptions_batches_selects_exact_fields_and_json_encodes_jsonb():
    rows = [
        subscriptionRow(
            f"sub-{index:04d}",
            billing_state={"lifecycle_snapshot": {}},
        )
        for index in range(1002)
    ]
    client = FakeClient([], rows)

    subscriptions = AdminManagementService(client=client).listSubscriptions()

    first = subscriptions[0]
    assert len(subscriptions) == 1002
    assert set(first) == EXPECTED_SUBSCRIPTION_FIELDS
    assert tuple(first) == EXPECTED_SUBSCRIPTION_FIELD_ORDER
    assert first["subscribed_experts"] == '["finance","sales"]'
    assert first["pending_removals"] == "[]"
    assert first["pending_additions"] == "[]"
    assert first["billing_state"] == '{"lifecycle_snapshot":{}}'
    assert first["current_period_start"] is None
    assert client.selects["subscriptions"] == [
        EXPECTED_SUBSCRIPTION_SELECT,
        EXPECTED_SUBSCRIPTION_SELECT,
    ]
    assert client.ranges["subscriptions"] == [(0, 999), (1000, 1999)]


def test_list_subscriptions_uses_compact_deterministic_json_and_excludes_secrets():
    client = FakeClient([], [subscriptionRow()])

    result = AdminManagementService(client=client).listSubscriptions()[0]

    assert result["billing_state"] == (
        '{"a":2,"lifecycle_snapshot":{},"z":1}'
    )
    assert "provider_secret" not in result


@pytest.mark.parametrize("raw", ["not-json", "{}", '["", "sales"]', "[1, 2]"])
def test_subscription_patch_rejects_malformed_experts(raw):
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"subscribed_experts": raw}),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 422
    assert "subscribed_experts" in error.value.errors
    assert client.updates == []
    credits.applyDomainCountChange.assert_not_called()


def test_experts_are_trimmed_deduplicated_and_count_is_derived():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    credits.applyDomainCountChange.return_value = None
    patchPayload = AdminSubscriptionPatch.model_validate({
        "subscribed_experts": '[" Finance ", "finance", "Sales"]'
    })

    AdminManagementService(client=client, creditService=credits).updateSubscription(
        "sub-1", patchPayload, ADMIN_CONTEXT
    )

    payload = client.lastUpdate("subscriptions")
    assert payload["subscribed_experts"] == ["Finance", "Sales"]
    assert payload["domain_count"] == 2
    credits.applyDomainCountChange.assert_called_once_with(
        userId="user-1", domainCount=2, grantImmediately=False
    )


def test_subscription_expert_count_accepts_four():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    rawExperts = json.dumps(["a", "b", "c", "d"])

    AdminManagementService(client=client, creditService=credits).updateSubscription(
        "sub-1",
        AdminSubscriptionPatch.model_validate({"subscribed_experts": rawExperts}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdate("subscriptions")["domain_count"] == 4


def test_subscription_domain_count_accepts_four():
    client = FakeClient(
        [], [subscriptionRow(subscribed_experts=["a", "b", "c", "d"])]
    )

    AdminManagementService(client=client, creditService=MagicMock()).updateSubscription(
        "sub-1",
        AdminSubscriptionPatch.model_validate({"domain_count": 4}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdate("subscriptions")["domain_count"] == 4


@pytest.mark.parametrize("domainCount", [-1, 0, 5])
def test_subscription_domain_count_rejects_values_outside_one_to_four(domainCount):
    with pytest.raises(ValidationError):
        AdminSubscriptionPatch.model_validate({"domain_count": domainCount})


def test_subscription_service_rejects_zero_domain_count_with_flat_error_and_no_writes():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    patchPayload = AdminSubscriptionPatch.model_construct(
        _fields_set={"domain_count"}, domain_count=0
    )

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1", patchPayload, ADMIN_CONTEXT
        )

    assert error.value.statusCode == 422
    assert error.value.errors == {"domain_count": "Must be between 1 and 4"}
    assert client.updates == []
    credits.applyDomainCountChange.assert_not_called()


def test_subscription_service_rejects_empty_experts_with_flat_error_and_no_writes():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"subscribed_experts": "[]"}),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 422
    assert error.value.errors == {
        "subscribed_experts": "At least one expert is required"
    }
    assert client.updates == []
    credits.applyDomainCountChange.assert_not_called()


def test_subscription_expert_count_rejects_more_than_four_without_writes():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({
                "subscribed_experts": '["a","b","c","d","e"]'
            }),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 422
    assert "subscribed_experts" in error.value.errors
    assert client.updates == []


def test_conflicting_domain_count_returns_422_without_writes():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    patchPayload = AdminSubscriptionPatch.model_validate({
        "subscribed_experts": '["Finance", "Sales"]', "domain_count": 3
    })

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1", patchPayload, ADMIN_CONTEXT
        )

    assert error.value.statusCode == 422
    assert "domain_count" in error.value.errors
    assert client.updates == []
    credits.applyDomainCountChange.assert_not_called()


def test_domain_count_alone_repairs_stored_count_only_when_it_matches_experts():
    client = FakeClient([], [subscriptionRow(domain_count=1)])
    credits = MagicMock()

    result = AdminManagementService(client=client, creditService=credits).updateSubscription(
        "sub-1",
        AdminSubscriptionPatch.model_validate({"domain_count": 2}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdate("subscriptions")["domain_count"] == 2
    assert result["domain_count"] == 2
    credits.applyDomainCountChange.assert_called_once_with(
        userId="user-1", domainCount=2, grantImmediately=False
    )


def test_domain_count_alone_rejects_mismatch_with_current_experts():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"domain_count": 1}),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 422
    assert "domain_count" in error.value.errors
    assert client.updates == []


def test_subscription_update_returns_404_without_writes_or_side_effects():
    client = FakeClient([], [])
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "missing",
            AdminSubscriptionPatch.model_validate({"status": "expired"}),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 404
    assert client.updates == []
    credits.applyDomainCountChange.assert_not_called()


def test_status_change_derives_plan_and_revokes_sessions_without_loading_billing_lifecycle():
    client = FakeClient([], [subscriptionRow()])
    client.rows["Sessions"] = [
        {"id": "session-1", "userId": "user-1"},
        {"id": "session-2", "userId": "user-2"},
    ]
    credits = MagicMock()
    forbiddenModules = {
        "razorpay",
        "api.services.billing.billingEngine",
        "api.services.subscriptions.subscriptionService",
    }
    realImport = builtins.__import__

    def guardedImport(name, *args, **kwargs):
        if name in forbiddenModules:
            raise AssertionError(f"Forbidden billing lifecycle import: {name}")
        return realImport(name, *args, **kwargs)

    modulePath = Path(__file__).resolve().parents[1] / "api/services/adminManagementService.py"
    spec = importlib.util.spec_from_file_location(
        "admin_management_without_billing_lifecycle", modulePath
    )
    isolatedModule = importlib.util.module_from_spec(spec)
    with patch("builtins.__import__", side_effect=guardedImport):
        spec.loader.exec_module(isolatedModule)
        result = isolatedModule.AdminManagementService(
            client=client, creditService=credits
        ).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"status": "expired"}),
            ADMIN_CONTEXT,
        )

    # Manual monthly contract: an expired/reset paid row derives plan_type
    # none — historical billing mode must not force an active pro tier.
    assert client.lastUpdate("subscriptions")["plan_type"] == "none"
    assert client.deletedFilters("Sessions") == [("userId", "user-1")]
    assert result["status"] == "expired"


def test_subscription_update_uses_id_and_version_and_increments_version():
    client = FakeClient([], [subscriptionRow(version=12)])

    result = AdminManagementService(
        client=client, creditService=MagicMock()
    ).updateSubscription(
        "sub-1",
        AdminSubscriptionPatch.model_validate({"status": "paused"}),
        ADMIN_CONTEXT,
    )

    assert client.lastUpdateFilters("subscriptions") == [
        ("id", "sub-1"), ("version", 12)
    ]
    assert client.lastUpdate("subscriptions")["version"] == 13
    assert "updated_at" in client.lastUpdate("subscriptions")
    assert set(client.lastUpdate("subscriptions")) == {
        "status", "plan_type", "updated_at", "version"
    }
    assert client.selects["subscriptions"] == [
        f"{EXPECTED_SUBSCRIPTION_SELECT},erasure_pending"
    ]
    assert result["version"] == 13


def test_version_conflict_returns_409_without_side_effects():
    client = FakeClient([], [subscriptionRow()])
    client.forceEmptySubscriptionUpdate = True
    credits = MagicMock()

    with pytest.raises(AdminApiError) as error:
        AdminManagementService(client=client, creditService=credits).updateSubscription(
            "sub-1",
            AdminSubscriptionPatch.model_validate({"status": "paused"}),
            ADMIN_CONTEXT,
        )

    assert error.value.statusCode == 409
    assert client.deletes == []
    credits.applyDomainCountChange.assert_not_called()


def test_subscription_credit_failure_is_generic_redacted_and_retry_repairs_it():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    credits.applyDomainCountChange.side_effect = RuntimeError(
        "redis failed for user-1 with secret-credit-value"
    )
    auditLogger = FakeLogger()
    service = AdminManagementService(client=client, creditService=credits)
    patchPayload = AdminSubscriptionPatch.model_validate({
        "subscribed_experts": '["Finance"]'
    })

    with patch("api.services.adminManagementService.logger", auditLogger):
        with pytest.raises(AdminApiError) as firstAttempt:
            service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

        assert firstAttempt.value.statusCode == 500
        assert firstAttempt.value.message == "Failed to update subscription"
        assert client.rows["subscriptions"][0]["domain_count"] == 1
        assert any(record[2]["outcome"] == "side_effect_failed" for record in auditLogger.records)
        assert "secret-credit-value" not in repr(auditLogger.records)

        credits.applyDomainCountChange.side_effect = None
        result = service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert result["domain_count"] == 1
    assert credits.applyDomainCountChange.call_count == 2


def test_subscription_credit_applied_false_is_failure_and_retry_only_repairs_credit():
    client = FakeClient([], [subscriptionRow()])
    credits = MagicMock()
    credits.applyDomainCountChange.side_effect = [
        {"applied": False, "quota": 0, "delta": 0, "remaining": 0},
        {"applied": True, "quota": 1000, "delta": 0, "remaining": 1000},
    ]
    auditLogger = FakeLogger()
    service = AdminManagementService(client=client, creditService=credits)
    patchPayload = AdminSubscriptionPatch.model_validate({
        "subscribed_experts": '["Finance"]'
    })

    with patch("api.services.adminManagementService.logger", auditLogger):
        with pytest.raises(AdminApiError) as firstAttempt:
            service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert firstAttempt.value.statusCode == 500
    assert firstAttempt.value.message == "Failed to update subscription"
    storedAfterFailure = dict(client.rows["subscriptions"][0])
    assert storedAfterFailure["subscribed_experts"] == ["Finance"]
    assert storedAfterFailure["domain_count"] == 1
    assert storedAfterFailure["version"] == 8
    assert len(client.updates) == 1
    assert any(
        record[2]["outcome"] == "side_effect_failed"
        for record in auditLogger.records
    )

    result = service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert result["subscribed_experts"] == '["Finance"]'
    assert result["domain_count"] == 1
    assert client.rows["subscriptions"][0] == storedAfterFailure
    assert len(client.updates) == 1
    assert credits.applyDomainCountChange.call_count == 2


def test_subscription_session_failure_is_generic_and_retry_repairs_it():
    client = FakeClient([], [subscriptionRow()])
    client.rows["Sessions"] = [{"id": "session-1", "userId": "user-1"}]
    client.failSessionDelete = True
    auditLogger = FakeLogger()
    service = AdminManagementService(client=client, creditService=MagicMock())
    patchPayload = AdminSubscriptionPatch.model_validate({"status": "expired"})

    with patch("api.services.adminManagementService.logger", auditLogger):
        with pytest.raises(AdminApiError) as firstAttempt:
            service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert firstAttempt.value.statusCode == 500
    assert firstAttempt.value.message == "Failed to update subscription"
    assert any(row["userId"] == "user-1" for row in client.rows["Sessions"])
    assert any(record[2]["outcome"] == "side_effect_failed" for record in auditLogger.records)
    assert "secret-session-value" not in repr(auditLogger.records)

    client.failSessionDelete = False
    result = service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert result["status"] == "expired"
    assert all(row["userId"] != "user-1" for row in client.rows["Sessions"])


def test_user_update_writes_durable_audit_row(stubDurableAudit):
    client = FakeClient([userRow()])
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"fullName": "New Name"})

    service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert stubDurableAudit.calls[-1]["action"] == "user.update"
    assert stubDurableAudit.calls[-1]["targetType"] == "user"
    assert stubDurableAudit.calls[-1]["targetId"] == "user-1"
    assert stubDurableAudit.calls[-1]["changedFields"] == ["fullName"]
    assert stubDurableAudit.calls[-1]["outcome"] == "success"
    assert stubDurableAudit.calls[-1]["admin"] is ADMIN_CONTEXT


def test_durable_audit_does_not_duplicate_the_log_line(stubDurableAudit):
    client = FakeClient([userRow()])
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"fullName": "New Name"})

    service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert stubDurableAudit.calls[-1]["emitLog"] is False


def test_subscription_update_writes_durable_audit_row(stubDurableAudit):
    client = FakeClient([], [subscriptionRow()])
    service = AdminManagementService(client=client, creditService=MagicMock())
    patchPayload = AdminSubscriptionPatch.model_validate({"status": "active"})

    service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert stubDurableAudit.calls[-1]["action"] == "subscription.update"
    assert stubDurableAudit.calls[-1]["targetType"] == "subscription"
    assert stubDurableAudit.calls[-1]["outcome"] == "success"


def test_missing_user_writes_not_found_durable_audit_row(stubDurableAudit):
    client = FakeClient([])
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"fullName": "New Name"})

    with pytest.raises(AdminApiError):
        service.updateUser("ghost", patchPayload, ADMIN_CONTEXT)

    assert stubDurableAudit.calls[-1]["outcome"] == "not_found"


def test_conflicted_subscription_update_writes_conflict_durable_audit_row(stubDurableAudit):
    client = FakeClient([], [subscriptionRow()])
    client.forceEmptySubscriptionUpdate = True
    service = AdminManagementService(client=client, creditService=MagicMock())
    patchPayload = AdminSubscriptionPatch.model_validate({"status": "expired"})

    with pytest.raises(AdminApiError) as excinfo:
        service.updateSubscription("sub-1", patchPayload, ADMIN_CONTEXT)

    assert excinfo.value.statusCode == 409
    assert stubDurableAudit.calls[-1]["action"] == "subscription.update"
    assert stubDurableAudit.calls[-1]["outcome"] == "conflict"


def test_failed_user_update_writes_failure_durable_audit_row(stubDurableAudit):
    client = FakeClient([userRow()])
    client.failNextUsersUpdate = True
    service = AdminManagementService(client=client)
    patchPayload = AdminUserPatch.model_validate({"fullName": "New Name"})

    with pytest.raises(AdminApiError):
        service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert stubDurableAudit.calls[-1]["outcome"] == "failed"


def test_durable_audit_failure_never_breaks_the_request():
    client = FakeClient([userRow()])
    exploding = MagicMock()
    exploding.record.side_effect = RuntimeError("audit down")
    service = AdminManagementService(client=client, auditService=exploding)
    patchPayload = AdminUserPatch.model_validate({"fullName": "New Name"})

    updated = service.updateUser("user-1", patchPayload, ADMIN_CONTEXT)

    assert updated["fullName"] == "New Name"
    assert exploding.record.called
