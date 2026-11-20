"""The getData route runs the (blocking) service off the event loop and maps its errors as before."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import dashboard
from utils.exceptionHandler import CustomException
from utils.sandboxSlots import BUSY_CODE, BUSY_MESSAGE

BODY = {"projectId": "p1", "page": "pg", "filters": [{"region": "north"}]}


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[dashboard.requireProductAccess] = lambda: object()
    monkeypatch.setattr(dashboard, "requireProjectOwner", lambda projectId, user: None)
    return TestClient(app)


def _post(client):
    return client.post("/getData", json=BODY)


def test_successReturnsPageDataAndRunsOffTheEventLoop(client, monkeypatch):
    seen = {}

    def getData(details):
        try:
            asyncio.get_running_loop()
            seen["onLoop"] = True
        except RuntimeError:
            seen["onLoop"] = False
        seen["details"] = details
        return {"id": "pg", "widgets": []}

    monkeypatch.setattr(dashboard.dashboardService, "getData", getData)

    response = _post(client)

    assert response.status_code == 200
    assert response.json() == {"status": "SUCCESS", "pageData": {"id": "pg", "widgets": []}}
    assert seen["onLoop"] is False
    assert seen["details"].projectId == "p1" and seen["details"].filters == BODY["filters"]


def test_busyStill503WithItsErrorCodeAndMessage(client, monkeypatch):
    def getData(details):
        raise CustomException(RuntimeError("busy"), statusCode=503, uiMessage=BUSY_MESSAGE, errorCode=BUSY_CODE)

    monkeypatch.setattr(dashboard.dashboardService, "getData", getData)

    response = _post(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["errorCode"] == BUSY_CODE and detail["message"] == BUSY_MESSAGE and detail["status"] == 503


def test_otherServiceErrorsStillMapToTheirStatus(client, monkeypatch):
    def getData(details):
        raise CustomException(ValueError("nope"))

    monkeypatch.setattr(dashboard.dashboardService, "getData", getData)

    response = _post(client)

    assert response.status_code == 500
    assert response.json()["detail"]["status"] == 500
