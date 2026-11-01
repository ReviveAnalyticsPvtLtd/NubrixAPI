import asyncio

from fastapi import HTTPException, status

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


sessionOwnsTask = utilsRouter.sessionOwnsTask


def setup_function():
    # Task ownership has its own tests in test_task_ownership.py.
    utilsRouter.sessionOwnsTask = lambda token, taskId: True


def teardown_function():
    utilsRouter.sessionOwnsTask = sessionOwnsTask


def test_task_status_revalidates_access_before_sending_final_result(monkeypatch):
    calls = 0

    def verify_then_revoke(token):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise HTTPException(status_code=403, detail="Access revoked")
        return token.credentials

    websocket = FakeWebSocket()
    monkeypatch.setattr(utilsRouter, "verifyToken", verify_then_revoke)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask())

    asyncio.run(utilsRouter.getTaskStatus(websocket))

    assert websocket.accepted is True
    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]
    assert calls == 2
