"""The task status socket answers only to the user who started the task."""
import asyncio
import os
import uuid

import pytest
from fastapi import FastAPI, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jose import jwt

import api.commons as commons
from api.commons import UserContext
from api.routers import utils
from api.services import utilityService as utilityServiceModule


OWNER = UserContext(userId="owner-user", email="owner@test.example", token="owner-token")
OTHER = UserContext(userId="other-user", email="other@test.example", token="other-token")


def clientFor(user):
    """The router's auth dependencies resolve to `user`."""
    app = FastAPI()
    app.include_router(utils.router, prefix="/utils")
    for route in utils.router.routes:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                app.dependency_overrides[dependency.call] = lambda: user
    return TestClient(app)


def sessionToken(user):
    return jwt.encode({"userId": user.userId, "email": user.email}, os.environ["SECRET_KEY"], algorithm="HS256")


def test_issued_task_belongs_to_its_user():
    assert commons.sessionOwnsTask(sessionToken(OWNER), commons.issueTaskId(OWNER.userId)) is True


def test_issued_task_does_not_belong_to_another_user():
    assert commons.sessionOwnsTask(sessionToken(OTHER), commons.issueTaskId(OWNER.userId)) is False


def test_each_issued_task_id_is_new():
    # Reused IDs would point two runs at one Celery result.
    assert commons.issueTaskId(OWNER.userId) != commons.issueTaskId(OWNER.userId)


def test_task_id_does_not_carry_the_user_id():
    assert OWNER.userId not in commons.issueTaskId(OWNER.userId)


@pytest.mark.parametrize("taskId", [
    None,
    "",
    "-",
    str(uuid.uuid4()),  # What Celery gives its scheduled jobs.
    "task-1",
    "ünicode-signature",
], ids=["missing", "empty", "separator-only", "celery-uuid", "plain", "non-ascii"])
def test_task_nobody_issued_is_refused(taskId):
    assert commons.sessionOwnsTask(sessionToken(OWNER), taskId) is False


def test_altered_task_id_is_refused():
    nonce, _, signature = commons.issueTaskId(OWNER.userId).partition("-")
    forged = f"{uuid.uuid4().hex}-{signature}"
    assert commons.sessionOwnsTask(sessionToken(OWNER), forged) is False
    assert commons.sessionOwnsTask(sessionToken(OWNER), f"{nonce}-{'0' * len(signature)}") is False


def test_forecast_task_is_queued_under_the_sessions_user(monkeypatch):
    queued = {}

    def sendTask(name, task_id=None):
        queued.update(name=name, taskId=task_id)
        return type("Result", (), {"task_id": task_id, "status": "PENDING"})()
    monkeypatch.setattr(utilityServiceModule.celeryApp, "send_task", sendTask)
    # A trigger check on this route, where there is one, is not what this test covers.
    monkeypatch.setattr(utils, "requireTriggerOwner", lambda triggerName, user: None, raising=False)

    response = clientFor(OWNER).get("/utils/sendForecasts")

    assert response.status_code == 200
    assert queued["name"] == "NubrixAI.generateForecasts"
    assert response.json()["taskId"] == queued["taskId"]
    assert commons.sessionOwnsTask(sessionToken(OWNER), queued["taskId"]) is True
    assert commons.sessionOwnsTask(sessionToken(OTHER), queued["taskId"]) is False


class FakeWebSocket:
    def __init__(self, token, taskId):
        self.query_params = {"token": token, "taskId": taskId}
        self.sent = []
        self.closeCodes = []

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=status.WS_1000_NORMAL_CLOSURE):
        self.closeCodes.append(code)


class ReadyTask:
    task_id = "any-task"
    status = "SUCCESS"

    def ready(self):
        return True

    def get(self):
        return 200


@pytest.fixture
def taskLookups(monkeypatch):
    """The session itself is valid; only the task's owner decides the outcome."""
    lookups = []
    monkeypatch.setattr(utils, "_websocketTokenIsValid", lambda credentials: True)
    monkeypatch.setattr(utils.celeryApp, "AsyncResult", lambda taskId: lookups.append(taskId) or ReadyTask())
    return lookups


def test_socket_sends_status_to_the_task_owner(taskLookups):
    taskId = commons.issueTaskId(OWNER.userId)
    websocket = FakeWebSocket(sessionToken(OWNER), taskId)

    asyncio.run(utils.getTaskStatus(websocket))

    assert websocket.sent[0] == {"status": "RUNNING"}
    assert len(websocket.sent) == 2
    assert taskLookups == [taskId]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def test_socket_refuses_another_users_task(taskLookups):
    websocket = FakeWebSocket(sessionToken(OTHER), commons.issueTaskId(OWNER.userId))

    asyncio.run(utils.getTaskStatus(websocket))

    assert websocket.sent == []
    assert taskLookups == []
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]


@pytest.mark.parametrize("taskId", [str(uuid.uuid4()), None], ids=["unissued", "missing"])
def test_socket_refuses_a_task_nobody_issued(taskLookups, taskId):
    # Plain Celery IDs belong to scheduled jobs, which no account owns.
    websocket = FakeWebSocket(sessionToken(OWNER), taskId)

    asyncio.run(utils.getTaskStatus(websocket))

    assert websocket.sent == []
    assert taskLookups == []
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]


def test_socket_refuses_every_unowned_task_the_same_way(taskLookups):
    # Telling these apart would confirm which task IDs are real.
    someoneElses = FakeWebSocket(sessionToken(OTHER), commons.issueTaskId(OWNER.userId))
    unissued = FakeWebSocket(sessionToken(OTHER), str(uuid.uuid4()))

    asyncio.run(utils.getTaskStatus(someoneElses))
    asyncio.run(utils.getTaskStatus(unissued))

    assert someoneElses.sent == unissued.sent
    assert someoneElses.closeCodes == unissued.closeCodes
