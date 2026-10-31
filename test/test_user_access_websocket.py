import asyncio

import pytest
from celery import Celery, states
from fastapi import HTTPException, status

from api import commons
from api.routers import utils as utilsRouter

taskResults = Celery(
    "task-status-test",
    broker="memory://",
    backend="cache+memory://",
    set_as_current=False,
)


class FakeWebSocket:
    def __init__(self):
        self.query_params = {"token": "access-token", "taskId": "task-1"}
        self.accepted = False
        self.sent = []
        self.closeCodes = []

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=status.WS_1000_NORMAL_CLOSURE):
        self.closeCodes.append(code)


class ReadyTask:
    task_id = "task-1"
    status = "SUCCESS"

    def ready(self):
        return True

    def get(self):
        return {"private": "result"}


def test_task_status_revalidates_access_before_sending_final_result(monkeypatch):
    calls = 0

    def verify_then_revoke(credentials, checkExpiry):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(status_code=403, detail="Access revoked")
        return credentials.credentials

    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", verify_then_revoke)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask())

    asyncio.run(utilsRouter.getTaskStatus(websocket))

    assert websocket.accepted is True
    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]
    assert calls == 2


def test_task_status_sends_final_result_to_valid_session(monkeypatch):
    websocket = FakeWebSocket()
    monkeypatch.setattr(
        commons,
        "_verifyTokenInternal",
        lambda credentials, checkExpiry: credentials.credentials,
    )
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask())

    asyncio.run(utilsRouter.getTaskStatus(websocket))

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "task-1", "status": "SUCCESS", "taskResponseCode": {"private": "result"}},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def test_task_status_refuses_invalid_session_before_sending_status(monkeypatch):
    def refuse_session(credentials, checkExpiry):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", refuse_session)

    asyncio.run(utilsRouter.getTaskStatus(websocket))

    assert websocket.sent == []
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]


def run_task_status(monkeypatch, taskId):
    websocket = FakeWebSocket()
    monkeypatch.setattr(
        commons,
        "_verifyTokenInternal",
        lambda credentials, checkExpiry: credentials.credentials,
    )
    monkeypatch.setattr(
        utilsRouter.celeryApp,
        "AsyncResult",
        lambda _taskId: taskResults.AsyncResult(taskId),
    )

    asyncio.run(utilsRouter.getTaskStatus(websocket))

    return websocket


def test_task_status_sends_final_failure_for_failed_task(monkeypatch):
    taskResults.backend.mark_as_failure("failed-task", ValueError("private failure detail"))

    websocket = run_task_status(monkeypatch, "failed-task")

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "failed-task", "status": "FAILURE", "taskResponseCode": None},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def test_task_status_sends_final_failure_for_revoked_task(monkeypatch):
    taskResults.backend.mark_as_revoked("revoked-task", "revoked")

    websocket = run_task_status(monkeypatch, "revoked-task")

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "revoked-task", "status": "FAILURE", "taskResponseCode": None},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


@pytest.mark.parametrize(
    ("responseCode", "finalStatus"),
    [(200, "SUCCESS"), (399, "SUCCESS"), (400, "FAILURE"), (500, "FAILURE")],
)
def test_task_status_reads_final_status_from_task_response_code(monkeypatch, responseCode, finalStatus):
    taskId = f"finished-task-{responseCode}"
    taskResults.backend.store_result(taskId, responseCode, states.SUCCESS)

    websocket = run_task_status(monkeypatch, taskId)

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": taskId, "status": finalStatus, "taskResponseCode": responseCode},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]
