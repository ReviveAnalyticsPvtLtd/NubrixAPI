import os
import inspect
import datetime
from types import SimpleNamespace

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.adminErrors import AdminApiError
from api.adminModels import (
    AdminAuditEventView,
    AdminFreeTrialExtensionResponse,
    AdminFreeTrialReductionResponse,
    AdminLoginRequest,
    AdminLoginResponse,
    AdminLogoutResponse,
    AdminSubscriptionView,
    AdminTokenUsageOverviewView,
    AdminWebsiteVisitOverviewView,
    AdminUserErasureAcceptedView,
    AdminUserSignupOverviewView,
    AdminUserView,
)
import api.adminModels as adminModels
from api.routers import admin
from api.routers.billingAdmin import verifyBillingAdmin
from api.services.adminAuthService import (
    AdminContext,
    getAdminAuthService,
    verifyAdmin,
    verifyAdminForLogout,
)
from api.services.adminAuditService import getAdminAuditService
from api.services.adminManagementService import getAdminManagementService
from api.services.adminOverviewService import AdminOverviewService, getAdminOverviewService
from api.services.adminTrialExtensionService import getAdminTrialExtensionService
from api.services.adminTrialReductionService import getAdminTrialReductionService
from api.services.userErasureService import getUserErasureService
from main import (
    admin_exception_handler,
    admin_unhandled_exception_middleware,
    app as productionApp,
    http_exception_handler,
    request_validation_handler,
)


ADMIN_CONTEXT = AdminContext(
    adminId="admin-1",
    email="admin@example.com",
    name="Admin",
    sessionId="session-1",
    token="admin-token",
)

USER_VIEW = {
    "userId": "user-1",
    "email": "user@example.com",
    "fullName": "Example User",
    "phoneNumber": None,
    "profileImage": None,
    "onboarded": True,
    "currentWorkspaceId": "workspace-1",
    "companyName": "Example Co",
    "role": "Analyst",
    "profileBio": None,
    "usage": None,
    "industryType": None,
    "companySize": None,
    "country": "IN",
    "goals": "Reporting",
    "source": "email",
    "isBanned": False,
    "bannedAt": None,
    "bannedBy": None,
    "banReason": None,
}

USER_ACCESS_VIEW = {
    "userId": "user-1",
    "isBanned": True,
    "bannedAt": "2026-08-23T00:00:00+00:00",
    "bannedBy": "admin-1",
    "banReason": None,
    "sessionsRevoked": 2,
    "supabaseAuthSynced": True,
    "warnings": [],
}

SUBSCRIPTION_VIEW = {
    "id": "subscription-1",
    "user_id": "user-1",
    "billing_mode": "subscription",
    "current_period_start": "2026-08-01T00:00:00+00:00",
    "current_period_end": "2026-09-01T00:00:00+00:00",
    "renewal_due_at": None,
    "auto_renew_enabled": True,
    "payment_collection_mode": "automatic",
    "status": "active",
    "default_currency": "INR",
    "subscribed_experts": "finance",
    "domain_count": 1,
    "pending_removals": "",
    "pending_additions": "",
    "billing_state": "current",
    "razorpay_customer_id": None,
    "razorpay_token_id": None,
    "subscription_anchor_day": 1,
    "recurring_failures": 0,
    "cancellation_reason": None,
    "version": 3,
    "plan_type": "pro",
    "created_at": "2026-07-01T00:00:00+00:00",
    "updated_at": "2026-08-01T00:00:00+00:00",
}


class FakeAdminAuthService:
    def login(self, email: str, password: str, clientIp: str) -> dict:
        if email == "rate@example.com":
            raise AdminApiError(429, "Too many login attempts. Try again later")
        return {
            "token": "admin-token",
            "admin": {
                "id": "admin-1",
                "email": email,
                "name": "Admin",
            },
        }

    def logout(self, admin: AdminContext) -> dict:
        return {"success": True, "sessionId": admin.sessionId}


class FakeAdminManagementService:
    def listUsers(self) -> list[dict]:
        return [USER_VIEW]

    def updateUser(self, userId: str, payload, admin: AdminContext) -> dict:
        if userId == "missing":
            raise AdminApiError(404, "User not found")
        if userId == "duplicate":
            raise AdminApiError(409, "A user with this email already exists")
        if userId == "explode":
            raise RuntimeError("database password=must-not-leak")
        return {
            **USER_VIEW,
            "userId": userId,
            **payload.model_dump(exclude_unset=True),
        }

    def listSubscriptions(self) -> list[dict]:
        return [SUBSCRIPTION_VIEW]

    def updateSubscription(
        self, subscriptionId: str, payload, admin: AdminContext
    ) -> dict:
        if subscriptionId == "missing":
            raise AdminApiError(404, "Subscription not found")
        return {
            **SUBSCRIPTION_VIEW,
            "id": subscriptionId,
            **payload.model_dump(exclude_unset=True),
        }

    def setUserAccess(self, userId: str, payload, admin: AdminContext) -> dict:
        if userId == "missing":
            raise AdminApiError(404, "User not found")
        return {
            **USER_ACCESS_VIEW,
            "userId": userId,
            "isBanned": payload.banned,
            "banReason": payload.reason,
        }

AUDIT_EVENT_VIEW = {
    "id": "audit-1",
    "admin_id": "admin-1",
    "admin_email": "admin@example.com",
    "session_id": "session-1",
    "actor_type": "admin",
    "action": "user.update",
    "target_type": "user",
    "target_id": "user-1",
    "changed_fields": '["email"]',
    "details": "{}",
    "outcome": "success",
    "created_at": "2026-08-17T00:00:00+00:00",
}


class FakeAdminAuditService:
    calls: list[dict] = []

    def listEvents(self, limit, offset, targetType=None, outcome=None):
        FakeAdminAuditService.calls.append({
            "limit": limit,
            "offset": offset,
            "targetType": targetType,
            "outcome": outcome,
        })
        return [AUDIT_EVENT_VIEW]


class FakeUserErasureService:
    def start(self, userId, payload, idempotencyKey, admin):
        if userId == "missing":
            raise AdminApiError(404, "User not found")
        return {
            "requestId": "8cfdb150-417d-47ab-acd1-fef39d2bc14e",
            "userId": userId,
            "status": "PENDING",
            "createdAt": "2026-08-24T10:00:00+00:00",
        }

class FakeAdminTrialExtensionService:
    calls: list[dict] = []

    def extend(self, payload, idempotencyKey, admin):
        FakeAdminTrialExtensionService.calls.append({
            "payload": payload,
            "idempotencyKey": idempotencyKey,
            "admin": admin,
        })
        return {
            "extensionId": idempotencyKey,
            "userId": payload.userId,
            "outcome": "EXTENDED",
            "daysAdded": payload.days,
            "previousExpiry": "2026-08-25T10:00:00+00:00",
            "newExpiry": "2026-08-30T10:00:00+00:00",
            "creditsRefreshed": True,
            "creditSyncStatus": "SYNCED",
            "accessStillBanned": False,
            "errorCode": None,
        }


class FakeAdminTrialReductionService:
    calls: list[dict] = []

    def reduce(self, payload, idempotencyKey, admin):
        if payload.userId == "missing":
            raise AdminApiError(404, "User not found")
        if payload.userId == "erasing":
            raise AdminApiError(409, "User erasure is in progress")
        FakeAdminTrialReductionService.calls.append({
            "payload": payload,
            "idempotencyKey": idempotencyKey,
            "admin": admin,
        })
        return {
            "reductionId": idempotencyKey,
            "userId": payload.userId,
            "outcome": "REDUCED",
            "daysRemoved": payload.days,
            "previousExpiry": "2026-09-20T10:00:00+00:00",
            "newExpiry": "2026-09-17T10:00:00+00:00",
            "accessStillBanned": False,
            "errorCode": None,
        }


SIGNUP_OVERVIEW_VIEW = {
    "period": "30d",
    "granularity": "day",
    "timezone": "UTC",
    "rangeStart": "2026-07-28T00:00:00+00:00",
    "rangeEnd": "2026-08-27T00:00:00+00:00",
    "lastUpdatedAt": "2026-08-26T14:30:00+00:00",
    "totalSignups": 4,
    "chart": {
        "labels": ["2026-07-28", "2026-07-29"],
        "datasets": [{"label": "New users", "data": [1, 3]}],
    },
}

TOKEN_USAGE_OVERVIEW_VIEW = {
    "period": "30d",
    "granularity": "day",
    "timezone": "UTC",
    "rangeStart": "2026-07-28T00:00:00+00:00",
    "rangeEnd": "2026-08-27T00:00:00+00:00",
    "lastUpdatedAt": "2026-08-26T14:30:00+00:00",
    "totalTokens": 750,
    "chart": {
        "labels": ["2026-07-28", "2026-07-29"],
        "datasets": [{"label": "LLM tokens", "data": [250, 500]}],
    },
}

WEBSITE_VISIT_OVERVIEW_VIEW = {
    "period": "30d", "granularity": "day", "timezone": "UTC",
    "rangeStart": "2026-07-28T00:00:00+00:00",
    "rangeEnd": "2026-08-27T00:00:00+00:00",
    "lastUpdatedAt": "2026-08-26T14:30:00+00:00",
    "totalVisits": 5,
    "chart": {
        "labels": ["2026-07-28", "2026-07-29"],
        "datasets": [{"label": "Website visits", "data": [2, 3]}],
    },
}


class FakeAdminOverviewService:
    """Records the period the route forwards without touching a backend."""

    requestedPeriods: list[str] = []
    requestedTokenPeriods: list[str] = []
    requestedWebsiteVisitPeriods: list[str] = []

    def getUserSignupOverview(self, period):
        FakeAdminOverviewService.requestedPeriods.append(period)
        return {**SIGNUP_OVERVIEW_VIEW, "period": period}

    def getTokenUsageOverview(self, period):
        FakeAdminOverviewService.requestedTokenPeriods.append(period)
        return {**TOKEN_USAGE_OVERVIEW_VIEW, "period": period}

    def getWebsiteVisitOverview(self, period):
        FakeAdminOverviewService.requestedWebsiteVisitPeriods.append(period)
        return {**WEBSITE_VISIT_OVERVIEW_VIEW, "period": period}


async def fakeVerifyAdmin(request: Request) -> AdminContext:
    if request.headers.get("Authorization") != "Bearer admin-token":
        raise AdminApiError(401, "Authentication required")
    return ADMIN_CONTEXT


async def fakeVerifyAdminForLogout(request: Request) -> AdminContext:
    return await fakeVerifyAdmin(request)


app = FastAPI(root_path="/api/latest")
app.add_exception_handler(AdminApiError, admin_exception_handler)
app.add_exception_handler(RequestValidationError, request_validation_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.middleware("http")(admin_unhandled_exception_middleware)
app.include_router(admin.router, prefix="/admin")


@app.get("/product-test-error")
async def productTestError():
    raise HTTPException(status_code=418, detail="product failure")


@app.post("/product-test-validation")
async def productTestValidation(_payload: AdminLoginRequest):
    return {"success": True}


@pytest.fixture()
def client():
    app.dependency_overrides[getAdminAuthService] = FakeAdminAuthService
    app.dependency_overrides[getAdminManagementService] = FakeAdminManagementService
    app.dependency_overrides[getAdminAuditService] = FakeAdminAuditService
    app.dependency_overrides[getAdminOverviewService] = FakeAdminOverviewService
    app.dependency_overrides[getAdminTrialExtensionService] = (
        FakeAdminTrialExtensionService
    )
    app.dependency_overrides[getAdminTrialReductionService] = (
        FakeAdminTrialReductionService
    )
    app.dependency_overrides[getUserErasureService] = FakeUserErasureService
    app.dependency_overrides[verifyAdmin] = fakeVerifyAdmin
    app.dependency_overrides[verifyAdminForLogout] = fakeVerifyAdminForLogout
    testClient = TestClient(app, raise_server_exceptions=False)
    try:
        yield testClient
    finally:
        testClient.close()
        app.dependency_overrides.clear()


def _adminHeaders() -> dict[str, str]:
    return {"Authorization": "Bearer admin-token"}


def test_login_and_logout_contracts(client):
    login = client.post(
        "/admin/auth/login",
        json={"email": "admin@example.com", "password": "valid password"},
    )
    assert login.status_code == 200
    assert login.json() == {
        "token": "admin-token",
        "admin": {
            "id": "admin-1",
            "email": "admin@example.com",
            "name": "Admin",
        },
    }

    logout = client.post("/admin/auth/logout", headers=_adminHeaders())
    assert logout.status_code == 200
    assert logout.json() == {"success": True}


def test_user_routes_return_only_public_response_fields(client):
    listed = client.get("/admin/users", headers=_adminHeaders())
    assert listed.status_code == 200
    assert listed.json() == [USER_VIEW]

    updated = client.patch(
        "/admin/users/user-2",
        json={"fullName": "Updated User"},
        headers=_adminHeaders(),
    )
    assert updated.status_code == 200
    assert updated.json() == {
        **USER_VIEW,
        "userId": "user-2",
        "fullName": "Updated User",
    }


def test_user_access_route_bans_with_optional_reason(client):
    response = client.patch(
        "/admin/users/user-2/access",
        json={"banned": True, "reason": ""},
        headers=_adminHeaders(),
    )

    assert response.status_code == 200
    assert response.json() == {
        **USER_ACCESS_VIEW,
        "userId": "user-2",
        "banReason": None,
    }


def test_user_erasure_start_contract(client):
    requestId = "8cfdb150-417d-47ab-acd1-fef39d2bc14e"
    started = client.post(
        "/admin/users/user-2/erasure",
        json={"confirmation": "ERASE", "reason": ""},
        headers={**_adminHeaders(), "Idempotency-Key": requestId},
    )

    assert started.status_code == 202
    assert started.json() == {
        "requestId": requestId,
        "userId": "user-2",
        "status": "PENDING",
        "createdAt": "2026-08-24T10:00:00+00:00",
    }

def test_user_erasure_start_requires_confirmation_and_idempotency_header(client):
    withoutHeader = client.post(
        "/admin/users/user-1/erasure",
        json={"confirmation": "ERASE"},
        headers=_adminHeaders(),
    )
    wrongConfirmation = client.post(
        "/admin/users/user-1/erasure",
        json={"confirmation": "erase"},
        headers={
            **_adminHeaders(),
            "Idempotency-Key": "8cfdb150-417d-47ab-acd1-fef39d2bc14e",
        },
    )

    assert withoutHeader.status_code == 422
    assert withoutHeader.json()["message"] == "Validation failed"
    assert wrongConfirmation.status_code == 422
    assert wrongConfirmation.json()["message"] == "Validation failed"


def test_free_trial_extension_processes_one_user(client):
    FakeAdminTrialExtensionService.calls.clear()
    idempotencyKey = "f53b33cd-219e-4c70-b5c2-43d956591fa5"

    response = client.post(
        "/admin/free-trial/extensions",
        json={
            "userId": "free-user",
            "days": 5,
            "reason": "Customer recovery",
        },
        headers={**_adminHeaders(), "Idempotency-Key": idempotencyKey},
    )

    assert response.status_code == 200
    assert response.json()["extensionId"] == idempotencyKey
    assert response.json()["outcome"] == "EXTENDED"
    assert response.json()["userId"] == "free-user"
    call = FakeAdminTrialExtensionService.calls[-1]
    assert call["idempotencyKey"] == idempotencyKey
    assert call["payload"].days == 5
    assert call["payload"].userId == "free-user"
    assert call["admin"] == ADMIN_CONTEXT


def test_free_trial_extension_requires_auth_and_idempotency_header(client):
    body = {"userId": "free-user", "days": 4}

    withoutAuth = client.post(
        "/admin/free-trial/extensions",
        json=body,
        headers={"Idempotency-Key": "b0eb9203-836a-41da-b33c-e46bcecc12c3"},
    )
    withoutIdempotency = client.post(
        "/admin/free-trial/extensions",
        json=body,
        headers=_adminHeaders(),
    )

    assert withoutAuth.status_code == 401
    assert withoutAuth.json() == {"message": "Authentication required"}
    assert withoutIdempotency.status_code == 422
    assert withoutIdempotency.json()["message"] == "Validation failed"


def test_free_trial_reduction_processes_one_user_with_explicit_confirmation(client):
    FakeAdminTrialReductionService.calls.clear()
    idempotencyKey = "9e3d768e-9f92-45f4-b816-2a9937ec97f8"

    response = client.post(
        "/admin/free-trial/reductions",
        json={
            "userId": "free-user",
            "days": 3,
            "reason": "Abuse remediation",
            "confirmation": "REDUCE",
        },
        headers={**_adminHeaders(), "Idempotency-Key": idempotencyKey},
    )

    assert response.status_code == 200
    assert response.json() == {
        "reductionId": idempotencyKey,
        "userId": "free-user",
        "outcome": "REDUCED",
        "daysRemoved": 3,
        "previousExpiry": "2026-09-20T10:00:00+00:00",
        "newExpiry": "2026-09-17T10:00:00+00:00",
        "accessStillBanned": False,
        "errorCode": None,
    }
    call = FakeAdminTrialReductionService.calls[-1]
    assert call["idempotencyKey"] == idempotencyKey
    assert call["payload"].reason == "Abuse remediation"
    assert call["payload"].confirmation == "REDUCE"
    assert call["admin"] == ADMIN_CONTEXT


def test_free_trial_reduction_requires_all_safety_controls(client):
    body = {
        "userId": "free-user",
        "days": 3,
        "reason": "Abuse remediation",
        "confirmation": "REDUCE",
    }
    key = "9e3d768e-9f92-45f4-b816-2a9937ec97f8"

    withoutAuth = client.post(
        "/admin/free-trial/reductions",
        json=body,
        headers={"Idempotency-Key": key},
    )
    withoutKey = client.post(
        "/admin/free-trial/reductions",
        json=body,
        headers=_adminHeaders(),
    )
    withoutReason = client.post(
        "/admin/free-trial/reductions",
        json={key: value for key, value in body.items() if key != "reason"},
        headers={**_adminHeaders(), "Idempotency-Key": key},
    )
    wrongConfirmation = client.post(
        "/admin/free-trial/reductions",
        json={**body, "confirmation": "reduce"},
        headers={**_adminHeaders(), "Idempotency-Key": key},
    )

    assert withoutAuth.status_code == 401
    assert withoutKey.status_code == 422
    assert withoutReason.status_code == 422
    assert wrongConfirmation.status_code == 422


@pytest.mark.parametrize(
    ("userId", "expectedStatus", "expectedBody"),
    [
        ("missing", 404, {"message": "User not found"}),
        ("erasing", 409, {"message": "User erasure is in progress"}),
    ],
)
def test_free_trial_reduction_pre_admission_errors_are_flat(
    client, userId, expectedStatus, expectedBody
):
    response = client.post(
        "/admin/free-trial/reductions",
        json={
            "userId": userId,
            "days": 3,
            "reason": "Abuse remediation",
            "confirmation": "REDUCE",
        },
        headers={
            **_adminHeaders(),
            "Idempotency-Key": "9e3d768e-9f92-45f4-b816-2a9937ec97f8",
        },
    )

    assert response.status_code == expectedStatus
    assert response.json() == expectedBody


def test_subscription_routes_return_only_public_response_fields(client):
    listed = client.get("/admin/subscriptions", headers=_adminHeaders())
    assert listed.status_code == 200
    assert listed.json() == [SUBSCRIPTION_VIEW]

    updated = client.patch(
        "/admin/subscriptions/subscription-2",
        json={"status": "paused"},
        headers=_adminHeaders(),
    )
    assert updated.status_code == 200
    assert updated.json() == {
        **SUBSCRIPTION_VIEW,
        "id": "subscription-2",
        "status": "paused",
    }


def test_protected_route_missing_token_is_401_message_only(client):
    response = client.get("/admin/users")
    assert response.status_code == 401
    assert response.json() == {"message": "Authentication required"}


def test_admin_validation_is_flat_but_product_error_shape_is_unchanged(client):
    adminResponse = client.patch(
        "/admin/users/user-1",
        json={"password": "x"},
        headers=_adminHeaders(),
    )
    assert adminResponse.status_code == 422
    assert adminResponse.json()["message"] == "Validation failed"
    assert "errors" in adminResponse.json()
    assert "detail" not in adminResponse.json()

    productResponse = client.get("/product-test-error")
    assert productResponse.status_code == 418
    assert productResponse.json() == {"detail": "product failure"}

    productValidation = client.post(
        "/product-test-validation", json={"password": "valid password"}
    )
    assert productValidation.status_code == 422
    assert "detail" in productValidation.json()
    assert "message" not in productValidation.json()


def test_malformed_admin_json_uses_flat_validation_shape(client):
    response = client.post(
        "/admin/auth/login",
        content="{",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["message"] == "Validation failed"
    assert response.json()["errors"]
    assert "detail" not in response.json()


@pytest.mark.parametrize(
    ("method", "path", "body", "expectedStatus", "expectedBody"),
    [
        ("patch", "/admin/users/missing", {"fullName": "X"}, 404,
         {"message": "User not found"}),
        ("patch", "/admin/users/duplicate", {"email": "new@example.com"}, 409,
         {"message": "A user with this email already exists"}),
        ("patch", "/admin/subscriptions/missing", {"status": "paused"}, 404,
         {"message": "Subscription not found"}),
        ("post", "/admin/auth/login",
         {"email": "rate@example.com", "password": "valid password"}, 429,
         {"message": "Too many login attempts. Try again later"}),
    ],
)
def test_admin_service_errors_keep_exact_status_and_flat_shape(
    client, method, path, body, expectedStatus, expectedBody
):
    response = client.request(
        method,
        path,
        json=body,
        headers=_adminHeaders(),
    )
    assert response.status_code == expectedStatus
    assert response.json() == expectedBody


def test_unexpected_admin_failure_is_generic_and_does_not_leak(client):
    response = client.patch(
        "/admin/users/explode",
        json={"fullName": "X"},
        headers=_adminHeaders(),
    )
    assert response.status_code == 500
    assert response.json() == {"message": "Internal server error"}
    assert "password" not in response.text


@pytest.mark.parametrize(
    "path",
    ["/admin/does-not-exist", "/api/latest/admin/does-not-exist"],
)
def test_unknown_admin_path_uses_admin_error_shape(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"message": "Not Found"}


def test_admin_routes_declare_strict_response_allowlists():
    accessView = getattr(adminModels, "AdminUserAccessView")
    expectedModels = {
        ("/admin/auth/login", "POST"): AdminLoginResponse,
        ("/admin/auth/logout", "POST"): AdminLogoutResponse,
        ("/admin/audit", "GET"): list[AdminAuditEventView],
        ("/admin/overview/user-signups", "GET"): AdminUserSignupOverviewView,
            ("/admin/overview/token-usage", "GET"): AdminTokenUsageOverviewView,
            ("/admin/overview/token-cost", "GET"): adminModels.AdminTokenCostOverviewView,
            ("/admin/overview/website-visits", "GET"):
                AdminWebsiteVisitOverviewView,
        ("/admin/users", "GET"): list[AdminUserView],
        ("/admin/users/{userId}", "PATCH"): AdminUserView,
        ("/admin/users/{userId}/access", "PATCH"): accessView,
        ("/admin/users/{userId}/erasure", "POST"): AdminUserErasureAcceptedView,
        ("/admin/free-trial/extensions", "POST"):
            AdminFreeTrialExtensionResponse,
        ("/admin/free-trial/reductions", "POST"):
            AdminFreeTrialReductionResponse,
        ("/admin/subscriptions", "GET"): list[AdminSubscriptionView],
        ("/admin/subscriptions/{subscriptionId}", "PATCH"): AdminSubscriptionView,
    }
    observedModels = {
        (route.path, method): route.response_model
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/admin/")
        for method in route.methods
    }
    assert observedModels == expectedModels


def test_admin_does_not_expose_single_user_erasure_status_route():
    routes = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/admin/user-erasure-requests/{requestId}", "GET") not in routes
    assert ("/admin/users/{userId}/erasure", "POST") in routes


def test_free_trial_extension_route_runs_blocking_work_in_threadpool():
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/admin/free-trial/extensions"
    )

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_free_trial_reduction_route_runs_blocking_work_in_threadpool():
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/admin/free-trial/reductions"
    )

    assert inspect.iscoroutinefunction(route.endpoint) is False


def test_billing_admin_routes_still_use_billing_admin_dependency():
    route = next(
        route
        for route in productionApp.routes
        if isinstance(route, APIRoute) and route.path == "/billing-admin/metrics"
    )
    assert any(
        dependency.call is verifyBillingAdmin
        for dependency in route.dependant.dependencies
    )


def test_audit_requires_authentication(client):
    response = client.get("/admin/audit")

    assert response.status_code == 401
    assert response.json() == {"message": "Authentication required"}


def test_audit_returns_events(client):
    FakeAdminAuditService.calls.clear()

    response = client.get("/admin/audit", headers=_adminHeaders())

    assert response.status_code == 200
    assert response.json() == [AUDIT_EVENT_VIEW]
    assert FakeAdminAuditService.calls[-1]["limit"] == 50
    assert FakeAdminAuditService.calls[-1]["offset"] == 0


def test_audit_passes_pagination_and_filters(client):
    FakeAdminAuditService.calls.clear()

    response = client.get(
        "/admin/audit?limit=25&offset=50&targetType=user&outcome=failed",
        headers=_adminHeaders(),
    )

    assert response.status_code == 200
    assert FakeAdminAuditService.calls[-1] == {
        "limit": 25,
        "offset": 50,
        "targetType": "user",
        "outcome": "failed",
    }


def test_audit_rejects_out_of_range_limit(client):
    response = client.get("/admin/audit?limit=0", headers=_adminHeaders())

    assert response.status_code == 422
    assert response.json()["message"] == "Validation failed"
    assert "limit" in response.json()["errors"]


def test_audit_rejects_limit_above_maximum(client):
    response = client.get("/admin/audit?limit=201", headers=_adminHeaders())

    assert response.status_code == 422
    assert "limit" in response.json()["errors"]


def test_audit_rejects_negative_offset(client):
    response = client.get("/admin/audit?offset=-1", headers=_adminHeaders())

    assert response.status_code == 422
    assert "offset" in response.json()["errors"]


def test_signup_overview_returns_chart_payload(client):
    FakeAdminOverviewService.requestedPeriods = []

    response = client.get(
        "/admin/overview/user-signups", headers=_adminHeaders()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["chart"]["labels"] == ["2026-07-28", "2026-07-29"]
    assert body["chart"]["datasets"] == [
        {"label": "New users", "data": [1, 3]}
    ]
    assert body["totalSignups"] == 4


def test_signup_overview_defaults_to_thirty_days(client):
    FakeAdminOverviewService.requestedPeriods = []

    client.get("/admin/overview/user-signups", headers=_adminHeaders())

    assert FakeAdminOverviewService.requestedPeriods == ["30d"]


def test_signup_overview_forwards_the_requested_period(client):
    FakeAdminOverviewService.requestedPeriods = []

    response = client.get(
        "/admin/overview/user-signups?period=1y", headers=_adminHeaders()
    )

    assert response.status_code == 200
    assert FakeAdminOverviewService.requestedPeriods == ["1y"]


def test_signup_overview_rejects_an_unsupported_period(client):
    response = client.get(
        "/admin/overview/user-signups?period=3w", headers=_adminHeaders()
    )

    assert response.status_code == 422


def test_signup_overview_requires_admin_authentication(client):
    response = client.get("/admin/overview/user-signups")

    assert response.status_code == 401


def test_signup_overview_view_rejects_unknown_fields():
    with pytest.raises(Exception):
        AdminUserSignupOverviewView(
            **{**SIGNUP_OVERVIEW_VIEW, "unexpected": "value"}
        )


def test_token_usage_overview_returns_chart_payload(client):
    FakeAdminOverviewService.requestedTokenPeriods = []

    response = client.get(
        "/admin/overview/token-usage", headers=_adminHeaders()
    )

    assert response.status_code == 200
    assert response.json() == TOKEN_USAGE_OVERVIEW_VIEW
    assert FakeAdminOverviewService.requestedTokenPeriods == ["30d"]


def test_token_usage_overview_forwards_the_requested_period(client):
    FakeAdminOverviewService.requestedTokenPeriods = []

    response = client.get(
        "/admin/overview/token-usage?period=1y", headers=_adminHeaders()
    )

    assert response.status_code == 200
    assert response.json()["period"] == "1y"
    assert FakeAdminOverviewService.requestedTokenPeriods == ["1y"]


def test_token_usage_overview_rejects_an_unsupported_period(client):
    response = client.get(
        "/admin/overview/token-usage?period=3w", headers=_adminHeaders()
    )

    assert response.status_code == 422


def test_token_usage_overview_requires_admin_authentication(client):
    response = client.get("/admin/overview/token-usage")

    assert response.status_code == 401


def test_token_usage_overview_view_rejects_unknown_fields():
    with pytest.raises(Exception):
        AdminTokenUsageOverviewView(
            **{**TOKEN_USAGE_OVERVIEW_VIEW, "unexpected": "value"}
        )


def test_website_visit_overview_defaults_to_thirty_days_and_returns_allowlisted_chart(client):
    FakeAdminOverviewService.requestedWebsiteVisitPeriods = []
    response = client.get("/admin/overview/website-visits", headers=_adminHeaders())

    assert response.status_code == 200
    assert response.json() == WEBSITE_VISIT_OVERVIEW_VIEW
    assert FakeAdminOverviewService.requestedWebsiteVisitPeriods == ["30d"]
    assert set(response.json()) == {
        "period", "granularity", "timezone", "rangeStart", "rangeEnd",
        "lastUpdatedAt", "totalVisits", "chart",
    }


def test_website_visit_overview_requires_authentication_and_rejects_invalid_period(client):
    assert client.get("/admin/overview/website-visits").status_code == 401
    response = client.get(
        "/admin/overview/website-visits?period=3w", headers=_adminHeaders()
    )
    assert response.status_code == 422


def test_website_visit_overview_view_rejects_unknown_fields():
    with pytest.raises(Exception):
        AdminWebsiteVisitOverviewView(
            **{**WEBSITE_VISIT_OVERVIEW_VIEW, "unexpected": "value"}
        )


@pytest.fixture()
def costClient(client):
    # Keep the service and response serialization real; replace only remote I/O.
    metrics = SimpleNamespace(metrics=lambda **kwargs: SimpleNamespace(data=[
        {"time_dimension": "2026-08-26T00:00:00Z", "sum_totalCost": "0.00000125"},
    ]))
    service = AdminOverviewService(
        langfuseClient=SimpleNamespace(api=SimpleNamespace(metrics=metrics)),
        now=lambda: datetime.datetime(2026, 8, 26, 14, 30, tzinfo=datetime.timezone.utc),
    )
    app.dependency_overrides[getAdminOverviewService] = lambda: service
    return client, service


def test_token_cost_route_defaults_to_thirty_days_and_serializes_decimal_chart(costClient):
    client, _service = costClient
    response = client.get("/admin/overview/token-cost", headers=_adminHeaders())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "period", "granularity", "timezone", "rangeStart", "rangeEnd",
        "lastUpdatedAt", "totalCost", "currency", "chart",
    }
    assert body["period"] == "30d"
    assert body["currency"] == "USD"
    assert body["totalCost"] == 0.00000125
    assert len(body["chart"]["labels"]) == 30
    assert body["chart"]["datasets"] == [
        {"label": "LLM cost (USD)", "data": [0] * 29 + [0.00000125]},
    ]


@pytest.mark.parametrize("period,count", [
    ("7d", 7), ("14d", 14), ("30d", 30), ("90d", 13), ("6m", 26), ("1y", 12),
])
def test_token_cost_route_accepts_all_token_chart_periods(costClient, period, count):
    client, _service = costClient
    response = client.get(f"/admin/overview/token-cost?period={period}", headers=_adminHeaders())
    assert response.status_code == 200
    assert response.json()["period"] == period
    assert len(response.json()["chart"]["labels"]) == count


def test_token_cost_route_rejects_invalid_period(costClient):
    client, _service = costClient
    response = client.get("/admin/overview/token-cost?period=3w", headers=_adminHeaders())
    assert response.status_code == 422
    assert response.json()["message"] == "Validation failed"
    assert "period" in response.json()["errors"]


def test_token_cost_route_requires_admin_authentication(costClient):
    client, _service = costClient
    response = client.get("/admin/overview/token-cost")
    assert response.status_code == 401


def test_token_cost_route_returns_unavailable_instead_of_zero(costClient):
    client, service = costClient
    service._langfuseClient = None
    response = client.get("/admin/overview/token-cost", headers=_adminHeaders())
    assert response.status_code == 503
    assert response.json() == {"message": "LLM usage analytics is unavailable"}


def test_token_cost_route_sanitizes_upstream_errors(costClient):
    client, service = costClient

    def fail(**kwargs):
        raise RuntimeError("secret upstream details")

    service.langfuseClient.api.metrics.metrics = fail
    response = client.get("/admin/overview/token-cost", headers=_adminHeaders())
    assert response.status_code == 500
    assert response.json() == {"message": "Failed to load LLM token cost overview"}


def test_token_cost_schema_preserves_numbers_and_rejects_unknown_fields():
    payload = {
        **{key: value for key, value in TOKEN_USAGE_OVERVIEW_VIEW.items() if key != "totalTokens"},
        "totalCost": 0.00000125, "currency": "USD",
        "chart": {"labels": ["2026-08-26"], "datasets": [
            {"label": "LLM cost (USD)", "data": [0.00000125]},
        ]},
    }
    model = adminModels.AdminTokenCostOverviewView
    assert model(**payload).model_dump(mode="json") == payload
    with pytest.raises(ValidationError):
        model(**{**payload, "unexpected": "value"})


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf")])
def test_token_cost_schema_rejects_invalid_totals_and_chart_values(amount):
    model = adminModels.AdminTokenCostOverviewView
    base = {
        **{key: value for key, value in TOKEN_USAGE_OVERVIEW_VIEW.items() if key != "totalTokens"},
        "totalCost": 0, "currency": "USD",
    }
    with pytest.raises(ValidationError):
        model(**{**base, "totalCost": amount})
    with pytest.raises(ValidationError):
        model(**{**base, "chart": {"labels": ["2026-08-26"], "datasets": [
            {"label": "LLM cost (USD)", "data": [amount]},
        ]}})
