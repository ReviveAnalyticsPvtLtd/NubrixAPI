import asyncio

from fastapi import HTTPException, status

from api import commons
from api.routers import utils as utilsRouter


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
