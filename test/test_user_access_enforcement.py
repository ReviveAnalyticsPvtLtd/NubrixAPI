import datetime
import hashlib
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from api.models import Login, LoginWithProvider
from api.services.authenticationService import AuthenticationService, DEFAULT_PROFILE_IMAGE
from utils.exceptionHandler import CustomException


ACCESS_MESSAGE = (
    "Access to this account has been revoked. "
    "Please contact NubrixAI Support."
)


class FakeQuery:
    def __init__(self, client, tableName):
        self.client = client
        self.tableName = tableName
        self.operation = "select"
        self.payload = None
        self.filters = []
        self.limitCount = None

    def select(self, _fields):
        self.operation = "select"
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, count):
        self.limitCount = count
        return self

    def order(self, _field, desc=False):
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = dict(payload)
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = dict(payload)
        return self

    def execute(self):
        rows = [
            row for row in self.client.rows.get(self.tableName, [])
            if all(row.get(field) == value for field, value in self.filters)
        ]
        if self.limitCount is not None:
            rows = rows[:self.limitCount]
        if self.operation == "update":
            for row in rows:
                row.update(self.payload)
        elif self.operation == "insert":
            self.client.rows.setdefault(self.tableName, []).append(self.payload)
            rows = [self.payload]
        return SimpleNamespace(data=[dict(row) for row in rows])


class FakeAuthAdmin:
    def __init__(self, user):
        self.user = user
        self.calls = 0

    def list_users(self, page, per_page):
        self.calls += 1
        return [self.user] if self.calls == 1 else []


class FakeClient:
    def __init__(self, users, authUser=None, sessions=None):
        self.rows = {
            "Users": list(users),
            "Sessions": list(sessions or []),
            "subscriptions": [],
        }
        self.auth = SimpleNamespace(admin=FakeAuthAdmin(authUser))

    def table(self, tableName):
        return FakeQuery(self, tableName)


def authenticationService(client):
    service = AuthenticationService.__new__(AuthenticationService)
    service.client = client
    return service


def successfulAuthenticationService(client):
    service = authenticationService(client)
    subscription = {
        "status": "active",
        "billing_mode": "monthly",
        "current_period_start": "2026-09-01T00:00:00+00:00",
        "current_period_end": "2026-10-01T00:00:00+00:00",
    }
    service._ensureSubscriptionSnapshot = lambda _userId: subscription
    service._refreshLifecycleSnapshot = lambda _userId, _subscription: 2
    service._getCreditSnapshot = lambda _userId: {
        "monthlyQuota": 100,
        "monthlyUsed": 0,
        "monthlyRemaining": 100,
        "topupRemaining": 0,
        "totalRemaining": 100,
    }
    return service


def assertTokenExpiresWithStoredSession(accessToken, session):
    payload = jwt.decode(
        accessToken,
        os.environ["SECRET_KEY"],
        algorithms=["HS256"],
    )
    sessionStart = datetime.datetime.fromisoformat(payload["sessionStartTime"])
    storedExpiry = datetime.datetime.fromisoformat(session["expiresAt"])

    assert isinstance(payload["exp"], int)
    assert payload["exp"] == int(storedExpiry.timestamp())
    assert payload["exp"] - int(sessionStart.timestamp()) == 24 * 60 * 60


def test_password_login_jwt_expires_with_stored_session():
    email = "active@example.com"
    password = "correct-password"
    hashed = hashlib.md5(
        (password + os.environ["SECRET_KEY"]).encode("utf-8")
    ).hexdigest()
    authUser = SimpleNamespace(
        id="user-1",
        email=email,
        email_confirmed_at="2026-01-01T00:00:00+00:00",
        confirmed_at="2026-01-01T00:00:00+00:00",
    )
    client = FakeClient([{
        "userId": "user-1",
        "email": email,
        "password": hashed,
        "onboarded": True,
        "currentWorkspaceId": "workspace-1",
        "profileImage": None,
        "isBanned": False,
    }], authUser=authUser)

    response = successfulAuthenticationService(client).login(
        Login(email=email, password=password)
    )

    assertTokenExpiresWithStoredSession(response["accessToken"], client.rows["Sessions"][0])


def test_provider_login_jwt_expires_with_stored_session():
    email = "provider@example.com"
    client = FakeClient([{
        "userId": "user-1",
        "email": email,
        "password": "provider-password",
        "onboarded": True,
        "currentWorkspaceId": "workspace-1",
        "profileImage": DEFAULT_PROFILE_IMAGE,
        "isBanned": False,
    }])

    response = successfulAuthenticationService(client).loginWithProvider(
        LoginWithProvider(
            email=email,
            provider="google",
            sub="provider-subject",
        )
    )

    assertTokenExpiresWithStoredSession(response["accessToken"], client.rows["Sessions"][0])


def test_password_login_rejects_banned_user_after_valid_credentials():
    email = "banned@example.com"
    password = "correct-password"
    hashed = hashlib.md5(
        (password + os.environ["SECRET_KEY"]).encode("utf-8")
    ).hexdigest()
    authUser = SimpleNamespace(
        id="user-1",
        email=email,
        email_confirmed_at="2026-01-01T00:00:00+00:00",
        confirmed_at="2026-01-01T00:00:00+00:00",
    )
    client = FakeClient([{
        "userId": "user-1",
        "email": email,
        "password": hashed,
        "onboarded": True,
        "currentWorkspaceId": "workspace-1",
        "profileImage": None,
        "isBanned": True,
    }], authUser=authUser)

    with pytest.raises(CustomException) as captured:
        authenticationService(client).login(Login(email=email, password=password))

    assert captured.value.statusCode == 403
    assert captured.value.uiMessage == ACCESS_MESSAGE
    assert captured.value.errorCode == "ACCOUNT_ACCESS_REVOKED"
    assert client.rows["Sessions"] == []


def test_provider_login_rejects_existing_banned_user():
    client = FakeClient([{
        "userId": "user-1",
        "email": "banned@example.com",
        "isBanned": True,
    }])

    with pytest.raises(CustomException) as captured:
        authenticationService(client).loginWithProvider(LoginWithProvider(
            email="banned@example.com",
            provider="google",
            sub="provider-subject",
        ))

    assert captured.value.statusCode == 403
    assert captured.value.uiMessage == ACCESS_MESSAGE
    assert captured.value.errorCode == "ACCOUNT_ACCESS_REVOKED"
    assert client.rows["Sessions"] == []


def test_live_product_token_returns_support_message_for_banned_user(monkeypatch):
    from api import commons

    token = jwt.encode(
        {
            "userId": "user-1",
            "email": "banned@example.com",
            "sessionStartTime": str(datetime.datetime.now(datetime.timezone.utc)),
        },
        os.environ["SECRET_KEY"],
        "HS256",
    )
    fakeClient = FakeClient(
        [{"userId": "user-1", "email": "banned@example.com", "isBanned": True}],
        sessions=[],
    )
    monkeypatch.setattr(commons, "client", fakeClient)

    with pytest.raises(HTTPException) as captured:
        commons._verifyTokenInternal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            checkExpiry=False,
        )

    assert captured.value.status_code == 403
    assert captured.value.detail["message"] == ACCESS_MESSAGE
    assert captured.value.detail["errorCode"] == "ACCOUNT_ACCESS_REVOKED"


def test_active_user_token_still_passes_session_validation(monkeypatch):
    from api import commons

    email = "active@example.com"
    token = jwt.encode(
        {
            "userId": "user-1",
            "email": email,
            "sessionStartTime": str(datetime.datetime.now(datetime.timezone.utc)),
        },
        os.environ["SECRET_KEY"],
        "HS256",
    )
    fakeClient = FakeClient(
        [{"userId": "user-1", "email": email, "isBanned": False}],
        sessions=[{
            "userId": "user-1",
            "email": email,
            "accessToken": token,
            "expiresAt": "2099-01-01T00:00:00+00:00",
        }],
    )
    monkeypatch.setattr(commons, "client", fakeClient)

    verified = commons._verifyTokenInternal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        checkExpiry=False,
    )

    assert verified == token


def test_product_token_rejects_session_owned_by_another_user(monkeypatch):
    from api import commons

    email = "shared@example.com"
    token = jwt.encode(
        {
            "userId": "user-1",
            "email": email,
            "sessionStartTime": str(datetime.datetime.now(datetime.timezone.utc)),
        },
        os.environ["SECRET_KEY"],
        "HS256",
    )
    fakeClient = FakeClient(
        [{"userId": "user-1", "email": email, "isBanned": False}],
        sessions=[{
            "userId": "user-2",
            "email": email,
            "accessToken": token,
            "expiresAt": "2099-01-01T00:00:00+00:00",
        }],
    )
    monkeypatch.setattr(commons, "client", fakeClient)

    with pytest.raises(HTTPException) as captured:
        commons._verifyTokenInternal(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
            checkExpiry=False,
        )

    assert captured.value.status_code == 401
    assert captured.value.detail["message"] == (
        "Token validation failed: identity mismatch"
    )
