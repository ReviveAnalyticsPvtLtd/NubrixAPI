"""Routes that spend compute without credits answer only to accounts with access."""
import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.commons import UserContext
from api.routers import blends, dashboard, dataLoader, manager, reporting, transformations, utils
from api.services.subscriptions.entitlementService import (
    EntitlementUnavailableError,
    SubscriptionEntitlement,
)


USER = UserContext(userId="owner-user", email="owner@test.example", token="owner-token")
OTHER = UserContext(userId="other-user", email="other@test.example", token="other-token")
PROJECT = "owned-project"
PRODUCT_ROUTERS = {
    "/blends": blends,
    "/dashboard": dashboard,
    "/loaders": dataLoader,
    "/projects": manager,
    "/reportingTool": reporting,
    "/transformations": transformations,
    "/utils": utils,
}
FILTERS = [{"column": "region", "values": ["north"]}]
GATED_UP_FRONT = {
    "/blends/createDataBlend",
    "/dashboard/dashboardRefresh/{projectId}",
    "/projects/generateReport/{projectId}",
    "/reportingTool/generatePanelChart",
    "/utils/sendForecasts",
}
# Every route that runs compute for a lapsed account today, as (method, path, json body, service).
COMPUTE_ROUTES = {
    "getData-filters": ("post", "/dashboard/getData", {"projectId": PROJECT, "page": "p", "filters": FILTERS}, "getData"),
    "getData-refresh": ("post", "/dashboard/getData", {"projectId": PROJECT, "page": "p", "refresh": True}, "getData"),
    "dashboardRefresh": ("get", f"/dashboard/dashboardRefresh/{PROJECT}", None, "pullDataInParallel"),
    "generateReport": ("post", f"/projects/generateReport/{PROJECT}", None, "generateReport"),
    "generatePanelChart": ("post", "/reportingTool/generatePanelChart",
        {"projectId": PROJECT, "chartType": "bar", "dataSource": "t"}, "generatePanelChart"),
    "createDataBlend": ("post", "/blends/createDataBlend",
        {"projectId": PROJECT, "blendOn": [], "blendName": "b", "tables": [], "joinTypes": []}, "createDataBlend"),
    "sendForecasts": ("get", "/utils/sendForecasts", None, "sendForecasts"),
}
compute = pytest.mark.parametrize("name", COMPUTE_ROUTES)


def entitlement(status, trialOrAbove, activeSubscription=False):
    return SubscriptionEntitlement(
        userId=USER.userId, status=status, planType="pro", currentPeriodEnd=None,
        activeSubscription=activeSubscription, trialOrAbove=trialOrAbove,
        paidPlan=activeSubscription, topupEligible=activeSubscription,
    )


LAPSED = entitlement("expired", trialOrAbove=False)
TRIAL = entitlement("trial", trialOrAbove=True)
ACTIVE = entitlement("active", trialOrAbove=True, activeSubscription=True)


def onEventLoop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class Entitlements:
    """The entitlement service as the gates see it, counting each database read."""

    def __init__(self):
        self.current = ACTIVE
        self.reads = 0
        self.readOnEventLoop = []

    def get(self, userId):
        self.reads += 1
        self.readOnEventLoop.append(onEventLoop())
        if isinstance(self.current, Exception):
            raise self.current
        return self.current


class ProjectsTable:
    """Answers the owner and trigger lookups from one set of project rows."""

    def __init__(self):
        self.rows = [{"projectId": PROJECT, "ownerUserId": USER.userId, "triggers": "forecast"}]
        self.failure = None
        self.lookedUpOnEventLoop = []

    def table(self, name):
        assert name == "Projects"
        self.filters = {}
        return self

    def select(self, columns):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def limit(self, count):
        return self

    def execute(self):
        self.lookedUpOnEventLoop.append(onEventLoop())
        if self.failure:
            raise self.failure
        data = [row for row in self.rows if all(row.get(c) == v for c, v in self.filters.items())]
        return type("Result", (), {"data": data})()


class Compute:
    """Records which service method a route reached."""

    def __init__(self, monkeypatch):
        self.calls = []
        record = lambda name, result: lambda *args, **kwargs: self.calls.append(name) or result

        async def generatePanelChart(*args, **kwargs):
            self.calls.append("generatePanelChart")
            return {}

        task = type("Task", (), {"task_id": "task-1", "status": "PENDING"})()
        monkeypatch.setattr(dashboard.dashboardService, "getData", record("getData", {}))
        monkeypatch.setattr(dashboard.dashboardService, "pullDataInParallel", record("pullDataInParallel", {}))
        monkeypatch.setattr(manager.managementService, "generateReport", record("generateReport", ""))
        monkeypatch.setattr(reporting.reportingService, "generatePanelChart", generatePanelChart)
        monkeypatch.setattr(blends.blendService, "createDataBlend", record("createDataBlend", None))
        monkeypatch.setattr(utils.utilityService, "sendForecasts", record("sendForecasts", task))


def dependencyCalls(route):
    pending, calls = [route.dependant], []
    while pending:
        current = pending.pop()
        calls.append(current.call)
        pending += current.dependencies
    return calls


def productRoutes():
    for prefix, module in PRODUCT_ROUTERS.items():
        for route in module.router.routes:
            if isinstance(route, APIRoute):
                yield prefix + route.path, route


@pytest.fixture
def entitlements(monkeypatch):
    service = Entitlements()
    # Read from the route's own gate so a sibling test's module reload cannot split the two.
    monkeypatch.setitem(dashboard.requireProjectOwner.__globals__, "subscriptionEntitlementService", service)
    return service


@pytest.fixture
def projects(monkeypatch):
    table = ProjectsTable()
    monkeypatch.setitem(dashboard.requireProjectOwner.__globals__, "client", table)
    return table


@pytest.fixture
def work(monkeypatch):
    return Compute(monkeypatch)


def clientFor(user=USER):
    """Session checks resolve to `user`; ownership and the subscription gate stay real."""
    app = FastAPI()
    for prefix, module in PRODUCT_ROUTERS.items():
        app.include_router(module.router, prefix=prefix)
    for _, route in productRoutes():
        for call in dependencyCalls(route):
            if getattr(call, "__name__", "") == "verifyUser":
                app.dependency_overrides[call] = lambda: user

    @app.exception_handler(HTTPException)
    async def flat(_, error):
        return JSONResponse(status_code=error.status_code,
            content=error.detail if isinstance(error.detail, dict) else {"message": error.detail})
    return TestClient(app)


def call(client, name):
    method, path, body, _ = COMPUTE_ROUTES[name]
    return client.request(method, path, json=body)


@compute
def test_lapsed_account_is_refused_before_any_compute(entitlements, projects, work, name):
    entitlements.current = LAPSED
    response = call(clientFor(), name)
    assert response.status_code == 403
    assert response.json()["errorCode"] == "FEATURE_BLOCKED"
    assert work.calls == []


@compute
def test_trial_account_reaches_compute(entitlements, projects, work, name):
    # requireActiveSubscription is false for trials, so using it here would fail this.
    entitlements.current = TRIAL
    assert call(clientFor(), name).status_code == 200
    assert work.calls == [COMPUTE_ROUTES[name][3]]


@compute
def test_paying_account_reaches_compute(entitlements, projects, work, name):
    assert call(clientFor(), name).status_code == 200
    assert work.calls == [COMPUTE_ROUTES[name][3]]


@compute
def test_unavailable_entitlement_is_retryable_and_runs_nothing(entitlements, projects, work, name):
    entitlements.current = EntitlementUnavailableError("database unavailable")
    # A fail-open gate would run the compute; a 403 would send a paying user to Manage Plan.
    response = call(clientFor(), name)
    assert response.status_code == 503
    assert response.json()["errorCode"] == "ENTITLEMENT_UNAVAILABLE"
    assert work.calls == []


@compute
def test_each_request_reads_the_entitlement_once(entitlements, projects, work, name):
    call(clientFor(), name)
    assert entitlements.reads == 1


@compute
def test_entitlement_read_stays_off_the_event_loop(entitlements, projects, work, name):
    call(clientFor(), name)
    assert entitlements.readOnEventLoop == [False]


@pytest.mark.parametrize("body", [
    {},
    {"filters": None},
    {"filters": []},
    {"filters": [], "refresh": False},
])
def test_plain_dashboard_read_costs_no_entitlement_read(entitlements, projects, work, body):
    # The dashboard sends filters: [] on every page load, so this is the hot path.
    entitlements.current = LAPSED
    response = clientFor().post("/dashboard/getData", json={"projectId": PROJECT, "page": "p", **body})
    assert response.status_code == 200
    assert work.calls == ["getData"]
    assert entitlements.reads == 0


@pytest.mark.parametrize("name", ["getData-filters", "getData-refresh", "dashboardRefresh", "generateReport"])
def test_another_users_project_still_reads_as_missing(entitlements, projects, work, name):
    response = call(clientFor(OTHER), name)
    assert response.status_code == 404
    assert response.json()["errorCode"] == "PROJECT_NOT_FOUND"
    assert work.calls == []


def test_only_the_compute_routes_are_gated_up_front():
    # getData is left out because its gate depends on the request body. A plain read
    # picking up this gate here would be the rejected per-request entitlement read.
    gated = {path for path, route in productRoutes()
        if any(getattr(call, "__name__", "") == "requireTrialOrAbove" for call in dependencyCalls(route))}
    assert gated == GATED_UP_FRONT


@pytest.mark.parametrize("path", sorted(GATED_UP_FRONT))
def test_gated_routes_run_the_session_checks_once(path):
    route = dict(productRoutes())[path]
    names = [getattr(call, "__name__", "") for call in dependencyCalls(route)]
    # verifyToken beside the gate would repeat the three session lookups on every call.
    assert names.count("verifyUser") == 1
    assert "verifyToken" not in names


@pytest.mark.parametrize("triggers", [
    "forecast",
    "Forecast",
    "Daily Report, forecast",
    "Weekly Forecasts,Daily Report",
])
def test_owner_of_the_forecast_trigger_can_start_it(entitlements, projects, work, triggers):
    projects.rows[0]["triggers"] = triggers
    response = clientFor().get("/utils/sendForecasts")
    assert response.status_code == 200
    assert response.json() == {"taskId": "task-1", "triggerName": "forecast", "taskStatus": "PENDING"}
    assert work.calls == ["sendForecasts"]


@pytest.mark.parametrize("rows", [
    [],
    [{"projectId": PROJECT, "ownerUserId": USER.userId, "triggers": None}],
    [{"projectId": PROJECT, "ownerUserId": USER.userId, "triggers": ""}],
    [{"projectId": PROJECT, "ownerUserId": USER.userId, "triggers": "Daily Report"}],
    [{"projectId": PROJECT, "ownerUserId": OTHER.userId, "triggers": "forecast"}],
])
def test_account_without_the_forecast_trigger_is_refused(entitlements, projects, work, rows):
    projects.rows = rows
    response = clientFor().get("/utils/sendForecasts")
    assert response.status_code == 403
    # FEATURE_BLOCKED would send a paying user through the subscription re-check for nothing.
    assert response.json()["errorCode"] == "TRIGGER_NOT_ASSIGNED"
    assert work.calls == []


def test_failed_trigger_lookup_denies_instead_of_allowing(entitlements, projects, work):
    projects.failure = ConnectionError("database API unavailable")
    response = clientFor().get("/utils/sendForecasts")
    assert response.status_code == 503
    assert response.json()["errorCode"] == "PROJECT_ACCESS_UNAVAILABLE"
    assert work.calls == []


def test_trigger_lookup_runs_off_the_event_loop(entitlements, projects, work):
    clientFor().get("/utils/sendForecasts")
    assert projects.lookedUpOnEventLoop == [False]
