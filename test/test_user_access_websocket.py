import asyncio
import threading
import types

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
        self.incoming = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=status.WS_1000_NORMAL_CLOSURE):
        self.closeCodes.append(code)


class LeavingWebSocket(FakeWebSocket):
    """Refuses to send once the disconnect has been received, as uvicorn does."""

    def __init__(self):
        super().__init__()
        self.left = threading.Event()
        self.sendsAfterLeaving = 0

    async def accept(self):
        await super().accept()
        self.loop = asyncio.get_running_loop()

    def leave(self):
        self.loop.call_soon_threadsafe(
            self.incoming.put_nowait,
            {"type": "websocket.disconnect", "code": 1001},
        )
        self.left.wait(timeout=2)

    async def receive(self):
        message = await super().receive()
        self.left.set()
        return message

    async def send_json(self, payload):
        if self.left.is_set():
            self.sendsAfterLeaving += 1
            raise RuntimeError("Unexpected ASGI message 'websocket.send'")
        await super().send_json(payload)

    async def close(self, code=status.WS_1000_NORMAL_CLOSURE):
        if self.left.is_set():
            self.sendsAfterLeaving += 1
            raise RuntimeError("Unexpected ASGI message 'websocket.close'")
        await super().close(code)


class SessionChecks:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, credentials, checkExpiry):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return credentials.credentials


@pytest.fixture(autouse=True)
def fastPolling(monkeypatch):
    monkeypatch.setattr(utilsRouter, "TASK_STATUS_POLL_SECONDS", 0.001, raising=False)


def watch_task(websocket):
    asyncio.run(asyncio.wait_for(utilsRouter.getTaskStatus(websocket), timeout=2))


class ReadyTask:
    task_id = "task-1"
    status = "SUCCESS"

    def __init__(self, unfinishedLooks=0):
        self.unfinishedLooks = unfinishedLooks

    def ready(self):
        self.unfinishedLooks -= 1
        return self.unfinishedLooks < 0

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
    monkeypatch.setattr(
        utilsRouter.celeryApp,
        "AsyncResult",
        lambda _taskId: ReadyTask(1),
    )

    watch_task(websocket)

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

    watch_task(websocket)

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


def test_task_status_runs_blocking_calls_off_the_event_loop(monkeypatch):
    threads = []

    class RecordingTask(ReadyTask):
        def ready(self):
            threads.append(threading.get_ident())
            return super().ready()

        def get(self):
            threads.append(threading.get_ident())
            return super().get()

    def record_session_check(credentials, checkExpiry):
        threads.append(threading.get_ident())
        return credentials.credentials

    monkeypatch.setattr(commons, "_verifyTokenInternal", record_session_check)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: RecordingTask(1))

    watch_task(FakeWebSocket())

    assert len(threads) == 5
    assert threading.main_thread().ident not in threads


def test_task_status_checks_the_session_once_per_poll(monkeypatch):
    sessionChecks = SessionChecks("valid")
    monkeypatch.setattr(commons, "_verifyTokenInternal", sessionChecks)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(2))

    watch_task(FakeWebSocket())

    assert sessionChecks.calls == 3


def test_task_status_stops_watching_when_the_client_leaves(monkeypatch):
    sessionChecks = SessionChecks("valid")
    websocket = FakeWebSocket()
    websocket.incoming.put_nowait({"type": "websocket.receive", "text": "ignored"})
    websocket.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})
    monkeypatch.setattr(commons, "_verifyTokenInternal", sessionChecks)
    monkeypatch.setattr(
        utilsRouter.celeryApp,
        "AsyncResult",
        lambda _taskId: ReadyTask(10**6),
    )

    watch_task(websocket)

    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == []
    assert sessionChecks.calls == 1


def test_task_status_gives_no_verdict_at_the_watch_limit(monkeypatch):
    monkeypatch.setattr(utilsRouter, "TASK_STATUS_MAX_WATCH_SECONDS", 0, raising=False)

    websocket = run_task_status(monkeypatch, "unfinished-task")

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "unfinished-task", "status": "UNKNOWN", "taskResponseCode": None},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def test_task_status_refuses_connection_when_session_check_errors(monkeypatch):
    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", SessionChecks(ConnectionError("outage")))

    watch_task(websocket)

    assert websocket.sent == []
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]


def test_task_status_holds_final_result_through_session_check_outage(monkeypatch):
    sessionChecks = SessionChecks("valid", ConnectionError("outage"), "valid")
    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", sessionChecks)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(1))

    watch_task(websocket)

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "task-1", "status": "SUCCESS", "taskResponseCode": {"private": "result"}},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]
    assert sessionChecks.calls == 3


def test_task_status_refuses_session_revoked_after_outage(monkeypatch):
    sessionChecks = SessionChecks(
        "valid",
        ConnectionError("outage"),
        HTTPException(status_code=401, detail="Invalid or expired token"),
    )
    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", sessionChecks)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(1))

    watch_task(websocket)

    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == [status.WS_1008_POLICY_VIOLATION]


def test_task_status_keeps_polling_error_detail_out_of_the_browser(monkeypatch):
    class BrokenTask(ReadyTask):
        def ready(self):
            raise ConnectionError("private connection detail")

    websocket = FakeWebSocket()
    monkeypatch.setattr(commons, "_verifyTokenInternal", SessionChecks("valid"))
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: BrokenTask())

    watch_task(websocket)

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"status": "ERROR", "errorDetail": "Task status is unavailable."},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def record_logged_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(
        utilsRouter,
        "logger",
        types.SimpleNamespace(warning=lambda *_args: None, error=lambda *args: errors.append(args)),
    )
    return errors


@pytest.mark.parametrize(
    "outcome",
    ["valid", HTTPException(status_code=401, detail="Invalid or expired token")],
)
def test_task_status_ends_quietly_when_client_leaves_during_session_check(monkeypatch, outcome):
    errors = record_logged_errors(monkeypatch)
    websocket = LeavingWebSocket()
    sessionChecks = SessionChecks("valid", outcome)

    def leave_during_poll_check(credentials, checkExpiry):
        if sessionChecks.calls == 1:
            websocket.leave()
        return sessionChecks(credentials, checkExpiry)

    monkeypatch.setattr(commons, "_verifyTokenInternal", leave_during_poll_check)
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(1))

    watch_task(websocket)

    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == []
    assert websocket.sendsAfterLeaving == 0
    assert errors == []


def test_task_status_ends_quietly_when_client_leaves_during_final_result_read(monkeypatch):
    errors = record_logged_errors(monkeypatch)
    websocket = LeavingWebSocket()

    class TaskReadAsClientLeaves(ReadyTask):
        def get(self):
            websocket.leave()
            return super().get()

    monkeypatch.setattr(commons, "_verifyTokenInternal", SessionChecks("valid"))
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: TaskReadAsClientLeaves())

    watch_task(websocket)

    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == []
    assert websocket.sendsAfterLeaving == 0
    assert errors == []


def test_task_status_ends_quietly_when_polling_fails_as_client_leaves(monkeypatch):
    errors = record_logged_errors(monkeypatch)
    websocket = LeavingWebSocket()

    class TaskFailingAsClientLeaves(ReadyTask):
        def ready(self):
            websocket.leave()
            raise ConnectionError("private connection detail")

    monkeypatch.setattr(commons, "_verifyTokenInternal", SessionChecks("valid"))
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: TaskFailingAsClientLeaves())

    watch_task(websocket)

    assert websocket.sent == [{"status": "RUNNING"}]
    assert websocket.closeCodes == []
    assert websocket.sendsAfterLeaving == 0
    assert errors == []


def test_task_status_gives_no_verdict_when_limit_passes_during_session_check_outage(monkeypatch):
    websocket = FakeWebSocket()
    monkeypatch.setattr(utilsRouter, "TASK_STATUS_MAX_WATCH_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(
        commons,
        "_verifyTokenInternal",
        SessionChecks("valid", ConnectionError("outage")),
    )
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(1))

    watch_task(websocket)

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "task-1", "status": "UNKNOWN", "taskResponseCode": None},
    ]
    assert "private" not in str(websocket.sent)
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]


def test_task_status_ignores_client_text_frames(monkeypatch):
    websocket = FakeWebSocket()
    websocket.incoming.put_nowait({"type": "websocket.receive", "text": "ignored"})
    monkeypatch.setattr(commons, "_verifyTokenInternal", SessionChecks("valid"))
    monkeypatch.setattr(utilsRouter.celeryApp, "AsyncResult", lambda _taskId: ReadyTask(1))

    watch_task(websocket)

    assert websocket.sent == [
        {"status": "RUNNING"},
        {"taskId": "task-1", "status": "SUCCESS", "taskResponseCode": {"private": "result"}},
    ]
    assert websocket.closeCodes == [status.WS_1000_NORMAL_CLOSURE]
