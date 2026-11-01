"""createProject answers only to the workspace's owner."""
import ast
import asyncio
import inspect

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api.commons as commons
from api.commons import UserContext
from api.routers import manager


OWNER = UserContext(userId="owner-user", email="owner@test.example", token="owner-token")
OTHER = UserContext(userId="other-user", email="other@test.example", token="other-token")
WORKSPACE = "owned-workspace"
PROJECT_DETAILS = {"projectName": "Sales", "workspaceId": WORKSPACE, "domainExpert": "banking"}


class WorkspacesTable:
    """Answers the single-row owner lookup from a fixed set of workspaces."""

    def __init__(self):
        self.owners = {WORKSPACE: OWNER.userId}
        self.failure = None
        self.workspaceId = None
        self.lookedUpOnEventLoop = []

    def table(self, name):
        assert name == "Workspaces"
        return self

    def select(self, columns):
        assert columns == "ownerId"
        return self

    def eq(self, column, value):
        assert column == "id"
        self.workspaceId = value
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
        owner = self.owners.get(self.workspaceId)
        data = [] if owner is None else [{"ownerId": owner}]
        return type("Result", (), {"data": data})()


@pytest.fixture
def workspaces(monkeypatch):
    table = WorkspacesTable()
    monkeypatch.setattr(commons, "client", table)
    return table


def appFor(user):
    """The router's auth dependency resolves to `user`; ownership stays real."""
    app = FastAPI()
    app.include_router(manager.router, prefix="/projects")
    for route in manager.router.routes:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                app.dependency_overrides[dependency.call] = lambda: user

    @app.exception_handler(HTTPException)
    async def flat(_, error):
        return JSONResponse(status_code=error.status_code,
            content=error.detail if isinstance(error.detail, dict) else {"message": error.detail})
    return app


def test_workspace_owner_is_allowed(workspaces):
    # Comparing against anything but the session's own user would fail here.
    assert commons.requireWorkspaceOwner(WORKSPACE, OWNER) is None


def test_another_users_workspace_is_denied(workspaces):
    with pytest.raises(HTTPException) as denied:
        commons.requireWorkspaceOwner(WORKSPACE, OTHER)
    assert denied.value.status_code == 404
    assert denied.value.detail["errorCode"] == "WORKSPACE_NOT_FOUND"


def test_denial_does_not_reveal_that_the_workspace_exists(workspaces):
    with pytest.raises(HTTPException) as owned:
        commons.requireWorkspaceOwner(WORKSPACE, OTHER)
    with pytest.raises(HTTPException) as missing:
        commons.requireWorkspaceOwner("no-such-workspace", OTHER)
    # A different status or message would confirm which workspace IDs are real.
    assert owned.value.status_code == missing.value.status_code
    assert (owned.value.detail | {"backendLogMessage": ""}) == (missing.value.detail | {"backendLogMessage": ""})
    assert OWNER.userId not in owned.value.detail["backendLogMessage"]


def test_failed_workspace_lookup_denies_instead_of_allowing(workspaces):
    workspaces.failure = ConnectionError("database API unavailable")
    # A fail-open except branch would let this return and reach the insert.
    with pytest.raises(HTTPException) as denied:
        commons.requireWorkspaceOwner(WORKSPACE, OWNER)
    assert denied.value.status_code == 503
    assert denied.value.detail["errorCode"] == "WORKSPACE_ACCESS_UNAVAILABLE"


def test_create_project_checks_the_workspace_before_doing_anything():
    # Moving the check below the service call would let the name lookup and insert run first.
    body = ast.parse(inspect.getsource(manager.createProject)).body[0].body
    statements = body[1:] if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) else body
    first = ast.unparse(statements[0])
    assert first == "await asyncio.to_thread(requireWorkspaceOwner, projectDetails.workspaceId, user)"


@pytest.mark.parametrize("user,expected,called", [(OWNER, 200, True), (OTHER, 404, False)])
def test_create_project_needs_the_workspace_owner(workspaces, monkeypatch, user, expected, called):
    calls = []
    monkeypatch.setattr(manager.managementService, "createProject",
        lambda projectDetails, token: calls.append(token) or "new-project")
    response = TestClient(appFor(user)).post("/projects/createProject", json=PROJECT_DETAILS)
    assert response.status_code == expected
    assert bool(calls) is called


def test_create_project_keeps_the_session_token(workspaces, monkeypatch):
    # The service decodes the token for the new project's owner fields.
    tokens = []
    monkeypatch.setattr(manager.managementService, "createProject",
        lambda projectDetails, token: tokens.append(token) or "new-project")
    TestClient(appFor(OWNER)).post("/projects/createProject", json=PROJECT_DETAILS)
    assert tokens == [OWNER.token]


def test_workspace_lookup_runs_off_the_event_loop(workspaces, monkeypatch):
    monkeypatch.setattr(manager.managementService, "createProject", lambda projectDetails, token: "new-project")
    TestClient(appFor(OWNER)).post("/projects/createProject", json=PROJECT_DETAILS)
    # Calling the blocking lookup directly in an async route would record True.
    assert workspaces.lookedUpOnEventLoop == [False]
