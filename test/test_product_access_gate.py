"""Every product-data route checks the plan in the backend; the frontend no longer has to."""
import pytest
from fastapi import HTTPException

from api.routers import dashboard
from api.services.subscriptions.entitlementService import EntitlementUnavailableError
from test.test_compute_subscription_gate import (  # noqa: F401  (fixtures)
    ACTIVE, LAPSED, PROJECT, TRIAL, USER, clientFor, dependencyCalls, entitlements, productRoutes, projects, work,
)

# A lapsed account still manages its profile; plans, payments, credits and sign-in live in other routers.
OPEN_ROUTES = {"/projects/getUserProfile", "/projects/editUserProfile"}
GATES = {
    "requireProductAccess", "requireProductAccessToken", "requireTrialOrAbove",
    "requireActiveSubscription", "requirePaidPlan", "requireCredits.<locals>._dependency",
}
PLAIN_READ = {"projectId": PROJECT, "page": "p"}


def commons():
    # Read from the routes' own module so a sibling test's module reload cannot split the two.
    return dashboard.requireProjectOwner.__globals__


def gatesOf(route):
    return {getattr(call, "__qualname__", "") for call in dependencyCalls(route)} & GATES


def productAccessRoutes():
    return [(path, route) for path, route in productRoutes()
            if gatesOf(route) & {"requireProductAccess", "requireProductAccessToken"}]


@pytest.mark.parametrize("path,route", list(productRoutes()), ids=lambda value: value if isinstance(value, str) else "")
def test_every_product_route_checks_the_plan(path, route):
    if path in OPEN_ROUTES:
        assert gatesOf(route) == set()
    else:
        assert gatesOf(route), f"{path} serves product data without a plan check"


def test_product_access_routes_run_the_session_checks_once():
    for path, route in productAccessRoutes():
        names = [getattr(call, "__name__", "") for call in dependencyCalls(route)]
        assert names.count("verifyUser") == 1, path
        assert "verifyToken" not in names, path


def test_lapsed_account_is_refused_a_plain_dashboard_read(entitlements, projects, work):
    entitlements.current = LAPSED
    response = clientFor().post("/dashboard/getData", json=PLAIN_READ)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "SUBSCRIPTION_INACTIVE"
    assert work.calls == []


def test_dashboard_reads_share_one_plan_lookup_a_minute(entitlements, projects, work):
    client = clientFor()
    for _ in range(5):
        assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 200
    assert entitlements.reads == 1
    assert work.calls == ["getData"] * 5


def test_unreadable_plan_with_no_earlier_answer_is_retryable(entitlements, projects, work):
    entitlements.current = EntitlementUnavailableError("database unavailable")
    response = clientFor().post("/dashboard/getData", json=PLAIN_READ)
    assert response.status_code == 503
    assert response.json()["errorCode"] == "ENTITLEMENT_UNAVAILABLE"
    assert work.calls == []


def test_unreadable_plan_keeps_serving_a_recent_yes(entitlements, projects, work, monkeypatch):
    client = clientFor()
    assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 200
    entitlements.current = EntitlementUnavailableError("database unavailable")
    commons()["productAccessCache"].clear()
    response = client.post("/dashboard/getData", json=PLAIN_READ)
    # Cleared, so this is the same as never having checked: retryable, not a refusal.
    assert response.status_code == 503
    entitlements.current = ACTIVE
    assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 200
    entitlements.current = EntitlementUnavailableError("database unavailable")
    assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 200


def test_a_new_payment_takes_effect_on_the_next_request(entitlements, projects, work):
    client = clientFor()
    entitlements.current = LAPSED
    assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 403
    entitlements.current = ACTIVE
    assert client.post("/dashboard/getData", json=PLAIN_READ).status_code == 200


def test_trial_account_reads_its_dashboards(entitlements, projects, work):
    entitlements.current = TRIAL
    assert clientFor().post("/dashboard/getData", json=PLAIN_READ).status_code == 200


def test_token_routes_receive_the_session_token():
    assert commons()["requireProductAccessToken"](USER) == USER.token


@pytest.mark.parametrize("gate", [
    "requireActiveSubscription", "requireTrialOrAbove", "requirePaidPlan", "requireCredits",
])
def test_paid_feature_gates_tell_a_lapsed_account_its_plan_ended(entitlements, gate):
    entitlements.current = LAPSED
    dependency = commons()[gate]("reporting_query") if gate == "requireCredits" else commons()[gate]
    with pytest.raises(HTTPException) as raised:
        dependency(USER)
    assert raised.value.status_code == 403
    assert raised.value.detail["errorCode"] == "SUBSCRIPTION_INACTIVE"


@pytest.mark.parametrize("gate", ["requireActiveSubscription", "requirePaidPlan"])
def test_paid_only_features_still_refuse_a_trial_as_feature_blocked(entitlements, gate):
    entitlements.current = TRIAL
    with pytest.raises(HTTPException) as raised:
        commons()[gate](USER)
    assert raised.value.status_code == 403
    assert raised.value.detail["errorCode"] == "FEATURE_BLOCKED"
