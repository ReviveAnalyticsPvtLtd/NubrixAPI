"""Sign-in returns the same plan snapshot as /auth/verify, so the frontend needs no second call."""
import hashlib
import os
from types import SimpleNamespace

import pytest

import api.services.subscriptions.subscriptionSnapshotService as snapshotServiceModule
from api.models import Login, LoginWithProvider
from api.services.billing.manualBillingPresentation import serializeSubscriptionSnapshot
from api.services.subscriptions.subscriptionModel import evaluateSubscription
from test.subscriptionModelFixtures import NOW, USER, factsFor
from test.test_user_access_enforcement import FakeClient, authenticationService
from utils.exceptionHandler import CustomException

EMAIL = "member@example.com"
PASSWORD = "correct-password"


def _client():
    hashed = hashlib.md5((PASSWORD + os.environ["SECRET_KEY"]).encode("utf-8")).hexdigest()
    authUser = SimpleNamespace(id=USER, email=EMAIL, email_confirmed_at="2026-01-01T00:00:00+00:00",
                               confirmed_at="2026-01-01T00:00:00+00:00")
    return FakeClient([{
        "userId": USER, "email": EMAIL, "password": hashed, "onboarded": True,
        "currentWorkspaceId": "workspace-1", "profileImage": None, "isBanned": False,
    }], authUser=authUser)


def _service(client, projectedSnapshot=None):
    """The login path with its database-backed helpers replaced; projectedSnapshot is what the
    unified-phase projection leaves on the row (None in the legacy phase)."""
    service = authenticationService(client)
    subscription = {"status": "active", "billing_mode": "monthly"}
    service._ensureSubscriptionSnapshot = lambda _userId: subscription

    def refresh(_userId, row):
        if projectedSnapshot is not None:
            row["_subscriptionSnapshot"] = projectedSnapshot
        return 30

    service._refreshLifecycleSnapshot = refresh
    service._getCreditSnapshot = lambda _userId: {}
    return service


def _signIn(service, kind):
    if kind == "password":
        return service.login(Login(email=EMAIL, password=PASSWORD))
    return service.loginWithProvider(LoginWithProvider(email=EMAIL, provider="google", sub="provider-subject"))


def _snapshotServiceReturning(monkeypatch, result):
    calls = []

    class Stub:
        def get(self, userId):
            calls.append(userId)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(snapshotServiceModule, "getSubscriptionSnapshotService", lambda: Stub())
    return calls


@pytest.mark.parametrize("kind", ["password", "provider"])
def test_login_returns_the_projected_plan_snapshot(monkeypatch, kind):
    snapshot = evaluateSubscription(factsFor("annual"), NOW)
    calls = _snapshotServiceReturning(monkeypatch, RuntimeError("must reuse the projected snapshot"))

    response = _signIn(_service(_client(), projectedSnapshot=snapshot), kind)

    assert response["subscription"] == serializeSubscriptionSnapshot(snapshot)
    assert calls == []


@pytest.mark.parametrize("kind", ["password", "provider"])
def test_login_loads_the_plan_snapshot_when_none_was_projected(monkeypatch, kind):
    snapshot = evaluateSubscription(factsFor("expired"), NOW)
    calls = _snapshotServiceReturning(monkeypatch, snapshot)

    response = _signIn(_service(_client()), kind)

    assert response["subscription"] == serializeSubscriptionSnapshot(snapshot)
    assert calls == [USER]


@pytest.mark.parametrize("kind", ["password", "provider"])
def test_login_fails_retryably_when_the_plan_cannot_be_read(monkeypatch, kind):
    _snapshotServiceReturning(monkeypatch, RuntimeError("database unavailable"))
    client = _client()

    with pytest.raises(CustomException) as caught:
        _signIn(_service(client), kind)

    assert caught.value.statusCode == 503
    assert caught.value.errorCode == "SUBSCRIPTION_AUTHORITY_UNAVAILABLE"
    assert client.rows["Sessions"] == []
