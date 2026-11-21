"""POST /loaders/importDatabase and GET /loaders/importStatus/{projectId}."""
import socket
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import api.commons as commons
from api.commons import UserContext
from api.routers import dataLoader
from api.services import databaseSources
from api.services.importJobs import ImportJobs
from nubrix.triggers.tasks import databaseImportTask
from test.test_import_jobs import FakeRedis
from utils.connectionTargets import BLOCKED_ADDRESS_MESSAGE, BlockedAddressError

OWNER = UserContext(userId="owner-user", email="owner@test.example", token="t")
PROJECT = "owned-project"
SECRET = "s3cret-pw"
BODY = {"projectId": PROJECT, "source": "mysql", "host": "db.example.com", "port": 3306,
        "user": "u", "password": SECRET, "db": "shop"}
CONNECT_FAILURE = "Unable to connect: check host/port/credentials."


class Owners:
    def __init__(self, owner=OWNER.userId):
        self.owner = owner
        self.calls = 0

    def table(self, _):
        return self

    def select(self, *_):
        return self

    def eq(self, *_):
        return self

    def limit(self, *_):
        return self

    def execute(self):
        self.calls += 1
        return type("R", (), {"data": [{"ownerUserId": self.owner}]})()


@pytest.fixture
def env(monkeypatch):
    owners = Owners()
    monkeypatch.setattr(commons, "client", owners)
    jobs = ImportJobs(FakeRedis())
    monkeypatch.setattr(dataLoader, "importJobs", jobs)
    send = Mock()
    monkeypatch.setattr(databaseImportTask, "sendBatch", send)
    tables = [("orders", None), ("a b", None), ("a_b", None), ("users", "sales"), ("t", "public")]
    monkeypatch.setattr(dataLoader, "listTables", Mock(return_value=tables))
    monkeypatch.setattr(databaseSources, "publicAddress", lambda host, port: "192.0.2.1")
    app = FastAPI()
    app.include_router(dataLoader.router, prefix="/loaders")
    for route in dataLoader.router.routes:
        if isinstance(route, APIRoute):
            for dependency in route.dependant.dependencies:
                app.dependency_overrides[dependency.call] = lambda: OWNER

    @app.exception_handler(HTTPException)
    async def flat(_, error):
        return JSONResponse(status_code=error.status_code, content=error.detail)

    return type("Env", (), {"client": TestClient(app), "owners": owners, "jobs": jobs, "send": send})


def post(env, **changes):
    return env.client.post("/loaders/importDatabase", json={**BODY, **changes})


def test_startsJobAndAnswers202(env):
    response = post(env)
    assert response.status_code == 202
    body = response.json()
    assert body["tables"] == ["orders", "a_b", "a_b_2", "sales_users", "t"]
    record = env.jobs.get(PROJECT)
    assert body["jobId"] == record["jobId"]
    assert [(t["name"], t["storedAs"]) for t in record["tables"]] == [
        ("orders", "orders"), ("a b", "a_b"), ("a_b", "a_b_2"), ("sales.users", "sales_users"), ("t", "t")]


def test_enqueuesFirstBatchOnce(env):
    post(env)
    env.send.assert_called_once()
    args = env.send.call_args.args
    assert args[1:3] == (PROJECT, OWNER.userId)
    assert args[5][2] == ["a_b", None, "a_b_2"]
    assert env.send.call_args.kwargs["start"] == 0


def test_answerNeverEchoesThePassword(env):
    response = post(env)
    assert SECRET not in response.text
    assert SECRET not in str(env.jobs.get(PROJECT))


def test_missingFieldIs400(env):
    response = post(env, host=None)
    assert response.status_code == 400
    assert response.json()["message"] == "The host field is required."
    env.send.assert_not_called()


def test_blockedAddressIs400(env, monkeypatch):
    def blocked(host, port):
        raise BlockedAddressError("10.0.0.1")
    monkeypatch.setattr(databaseSources, "publicAddress", blocked)
    response = post(env)
    assert (response.status_code, response.json()["message"]) == (400, BLOCKED_ADDRESS_MESSAGE)


@pytest.mark.parametrize("failure", [RuntimeError(f"login failed for {SECRET}"), ConnectionError("x")])
def test_listFailureIs400WithoutDriverText(env, monkeypatch, failure):
    monkeypatch.setattr(dataLoader, "listTables", Mock(side_effect=failure))
    response = post(env)
    assert (response.status_code, response.json()["message"]) == (400, CONNECT_FAILURE)
    assert SECRET not in response.text


def test_unresolvableHostIs400(env, monkeypatch):
    def gone(host, port):
        raise socket.gaierror("nope")
    monkeypatch.setattr(databaseSources, "publicAddress", gone)
    response = post(env)
    assert (response.status_code, response.json()["message"]) == (400, CONNECT_FAILURE)


def test_noTablesIs422(env, monkeypatch):
    monkeypatch.setattr(dataLoader, "listTables", Mock(return_value=[]))
    response = post(env)
    assert (response.status_code, response.json()["message"]) == (422, "No tables found.")


def test_runningImportIs409(env):
    assert post(env).status_code == 202
    response = post(env)
    assert (response.status_code, response.json()["message"]) == (409, "An import is already running for this project.")
    assert env.send.call_count == 1


def test_nonOwnerNeverTouchesTheHost(env):
    env.owners.owner = "someone-else"
    response = post(env)
    assert response.status_code == 404
    dataLoader.listTables.assert_not_called()


def test_statusReturnsRecord(env):
    jobId = post(env).json()["jobId"]
    response = env.client.get(f"/loaders/importStatus/{PROJECT}")
    assert response.status_code == 200
    assert response.json()["jobId"] == jobId


def test_statusWithoutImportIs404(env):
    response = env.client.get(f"/loaders/importStatus/{PROJECT}")
    assert (response.status_code, response.json()["message"]) == (404, "No import found.")


def test_statusChecksOwnerFirst(env):
    env.owners.owner = "someone-else"
    assert env.client.get(f"/loaders/importStatus/{PROJECT}").status_code == 404
    assert env.owners.calls == 1
