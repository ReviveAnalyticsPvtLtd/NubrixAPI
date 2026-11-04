"""Project routes answer only to the project's owner."""
import ast
import asyncio
import inspect
import io

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

import api.commons as commons
from api.commons import UserContext
from api.routers import blends, dashboard, dataLoader, manager, reporting, transformations, utils


OWNER = UserContext(userId="owner-user", email="owner@test.example", token="owner-token")
OTHER = UserContext(userId="other-user", email="other@test.example", token="other-token")
PROJECT = "owned-project"
PROJECT_ROUTERS = {
    "/blends": blends,
    "/dashboard": dashboard,
    "/loaders": dataLoader,
    "/projects": manager,
    "/reportingTool": reporting,
    "/transformations": transformations,
    "/utils": utils,
}


class ProjectsTable:
    """Answers the single-row owner lookup from a fixed set of projects."""

    def __init__(self, owners=None, failure=None):
        self.owners = {PROJECT: OWNER.userId} if owners is None else owners
        self.failure = failure
        self.projectId = None
        self.lookedUpOnEventLoop = []

    def table(self, name):
        assert name == "Projects"
        return self

    def select(self, columns):
        assert columns == "ownerUserId"
        return self

    def eq(self, column, value):
        assert column == "projectId"
        self.projectId = value
        return self

    def limit(self, count):
        return self

    def execute(self):
        try:
            asyncio.get_running_loop()
            self.lookedUpOnEventLoop.append(True)
        except RuntimeError:
            self.lookedUpOnEventLoop.append(False)
        if self.failure:
            raise self.failure
        owner = self.owners.get(self.projectId)
        data = [] if owner is None else [{"ownerUserId": owner}]
        return type("Result", (), {"data": data})()


@pytest.fixture
def projects(monkeypatch):
    table = ProjectsTable()
    monkeypatch.setattr(commons, "client", table)
    return table


def appFor(user):
    """Every router's auth dependency resolves to `user`; ownership stays real."""
    app = FastAPI()
    for prefix, module in PROJECT_ROUTERS.items():
        app.include_router(module.router, prefix=prefix)
        for route in module.router.routes:
            if isinstance(route, APIRoute):
                for dependency in route.dependant.dependencies:
                    app.dependency_overrides[dependency.call] = lambda: user

    @app.exception_handler(HTTPException)
    async def flat(_, error):
        return JSONResponse(status_code=error.status_code,
            content=error.detail if isinstance(error.detail, dict) else {"message": error.detail})
    return app


def projectInput(endpoint):
    """The source expression of a route's project ID, or None for a route without one."""
    for name, parameter in inspect.signature(endpoint).parameters.items():
        if name == "projectId":
            return "projectId"
        annotation = parameter.annotation
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel) and "projectId" in annotation.model_fields:
            return f"{name}.projectId"
    return None


def projectRoutes():
    routes = []
    for prefix, module in PROJECT_ROUTERS.items():
        for route in module.router.routes:
            if isinstance(route, APIRoute) and projectInput(route.endpoint):
                routes.append(pytest.param(route.endpoint, id=f"{prefix}{route.path}:{sorted(route.methods)[0]}"))
    return routes


def test_owner_is_allowed(projects):
    # Comparing against anything but the session's own user would fail here.
    assert commons.requireProjectOwner(PROJECT, OWNER) is None


def test_another_users_project_is_denied(projects):
    with pytest.raises(HTTPException) as denied:
        commons.requireProjectOwner(PROJECT, OTHER)
    assert denied.value.status_code == 404
    assert denied.value.detail["errorCode"] == "PROJECT_NOT_FOUND"


def test_denial_does_not_reveal_that_the_project_exists(projects):
    with pytest.raises(HTTPException) as owned:
        commons.requireProjectOwner(PROJECT, OTHER)
    with pytest.raises(HTTPException) as missing:
        commons.requireProjectOwner("no-such-project", OTHER)
    # A different status or message would confirm which project IDs are real.
    assert owned.value.status_code == missing.value.status_code
    assert (owned.value.detail | {"backendLogMessage": ""}) == (missing.value.detail | {"backendLogMessage": ""})
    assert OWNER.userId not in owned.value.detail["backendLogMessage"]


def test_project_without_an_owner_is_denied(projects):
    projects.owners = {PROJECT: None}
    with pytest.raises(HTTPException) as denied:
        commons.requireProjectOwner(PROJECT, OWNER)
    assert denied.value.status_code == 404


def test_failed_lookup_denies_instead_of_allowing(projects):
    projects.failure = ConnectionError("database API unavailable")
    # A fail-open except branch would let this return and reach project data.
    with pytest.raises(HTTPException) as denied:
        commons.requireProjectOwner(PROJECT, OWNER)
    assert denied.value.status_code == 503
    assert denied.value.detail["errorCode"] == "PROJECT_ACCESS_UNAVAILABLE"


@pytest.mark.parametrize("endpoint", projectRoutes())
def test_every_project_route_checks_ownership_before_doing_anything(endpoint):
    # Deleting the check from any route, or checking after its work, fails here.
    # New routes that take a project ID are picked up automatically.
    body = ast.parse(inspect.getsource(endpoint)).body[0].body
    statements = body[1:] if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else body
    first = ast.unparse(statements[0])
    assert first == f"await asyncio.to_thread(requireProjectOwner, {projectInput(endpoint)}, user)"


def test_route_inventory_covers_the_known_project_routes():
    # A broken inventory would let the check above pass by finding nothing.
    assert len(projectRoutes()) == 51


@pytest.mark.parametrize("user,status,called", [(OWNER, 200, True), (OTHER, 404, False)])
def test_project_id_from_the_path(projects, monkeypatch, user, status, called):
    calls = []
    monkeypatch.setattr(dashboard.dashboardService, "getAllColumns", lambda projectId: calls.append(projectId) or {})
    response = TestClient(appFor(user)).get(f"/dashboard/getAllColumns/{PROJECT}")
    assert response.status_code == status
    assert bool(calls) is called


@pytest.mark.parametrize("user,status,called", [(OWNER, 200, True), (OTHER, 404, False)])
def test_project_id_from_the_query(projects, monkeypatch, user, status, called):
    calls = []
    monkeypatch.setattr(manager.managementService, "deleteProject", lambda projectId: calls.append(projectId))
    response = TestClient(appFor(user)).delete("/projects/deleteProject", params={"projectId": PROJECT})
    assert response.status_code == status
    assert bool(calls) is called


@pytest.mark.parametrize("user,status,called", [(OWNER, 200, True), (OTHER, 404, False)])
def test_project_id_from_a_json_body(projects, monkeypatch, user, status, called):
    calls = []
    monkeypatch.setattr(dashboard.dashboardService, "getData", lambda details: calls.append(details) or {})
    response = TestClient(appFor(user)).post("/dashboard/getData", json={"projectId": PROJECT, "page": "page-1"})
    assert response.status_code == status
    assert bool(calls) is called


@pytest.mark.parametrize("user,status,called", [(OWNER, 200, True), (OTHER, 404, False)])
def test_project_id_from_a_multipart_form(projects, monkeypatch, user, status, called):
    calls = []

    async def loadCsvData(**kwargs):
        calls.append(kwargs)
        return {}
    monkeypatch.setattr(dataLoader.dataLoadService, "loadCsvData", loadCsvData)
    response = TestClient(appFor(user)).post("/loaders/loadCsvData", data={"projectId": PROJECT},
        files=[("files", ("table.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv"))])
    assert response.status_code == status
    assert bool(calls) is called


def test_streaming_route_is_denied_before_the_stream_starts(projects, monkeypatch):
    def streamMustNotStart(**kwargs):
        pytest.fail("Another user's project reached the transformation stream")
    monkeypatch.setattr(transformations.transformationService, "sendMessageStream", streamMustNotStart)
    response = TestClient(appFor(OTHER)).post("/transformations/t-1/messages",
        params={"projectId": PROJECT}, json={"content": "hello"})
    assert response.status_code == 404


def test_lookup_runs_off_the_event_loop(projects, monkeypatch):
    monkeypatch.setattr(dashboard.dashboardService, "getAllColumns", lambda projectId: {})
    TestClient(appFor(OWNER)).get(f"/dashboard/getAllColumns/{PROJECT}")
    # Calling the blocking lookup directly in an async route would record True.
    assert projects.lookedUpOnEventLoop == [False]


def test_rename_still_receives_the_session_token(projects, monkeypatch):
    tokens = []
    monkeypatch.setattr(manager.managementService, "renameProject",
        lambda renameDetails, token: tokens.append(token))
    response = TestClient(appFor(OWNER)).patch("/projects/renameProject",
        json={"projectId": PROJECT, "newProjectName": "Renamed"})
    assert response.status_code == 200
    assert tokens == [OWNER.token]
