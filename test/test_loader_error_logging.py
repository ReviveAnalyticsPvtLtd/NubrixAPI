"""A failed database load reports the error's type only, on every output: response, stdout, stderr, log file.

It runs in a child process because pytest replaces warning filters and captures streams, and other test files stub
the logger at collection.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
USER, FIRST, SECOND, HOST, OPTION = "usercanary", "firstcanary", "secondcanary", "hostcanary", "optioncanary"
GOOD = f"mongodb://{USER}:pw@{HOST}.invalid/d"
URIS = [
    f"mongodb://{USER}:x/{FIRST}${SECOND}@{HOST}.invalid/d",
    f"mongodb://{USER}:x/{FIRST}/{SECOND}@{HOST}.invalid/d",
    f"mongodb://{USER}:x/{FIRST} {SECOND}@{HOST}.invalid/d",
    f"mongodb://{USER}:12345/?{FIRST}={SECOND}@{HOST}.invalid",
    f"{GOOD}?readPreference={OPTION}",
    f"{GOOD}?maxPoolSize={OPTION}",
    f"{GOOD}?connectTimeoutMS={OPTION}",
    f"{GOOD}?compressors={OPTION}",
    f"{GOOD}?{OPTION}=1",
    f"{GOOD}?{OPTION}=1&{OPTION}=2",
]
CHILD = """
import json, logging, sys
import logtail

logtail.LogtailHandler = lambda *args, **kwargs: logging.NullHandler()


def offline(event, args):
    if event == "socket.getaddrinfo" and str(args[0]) not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("no network in this test")


sys.addaudithook(offline)

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.routers import dataLoader
from api.services import dataLoadService as module

uris, quoted = json.loads(sys.argv[1])


def driverFailure(*args, **kwargs):
    raise OperationalError("SELECT * FROM t", {}, Exception(quoted))


module.dataLoadService._acceptTableName = lambda *args: []
module.pd.read_sql = driverFailure
module.MongoClient.__getitem__ = driverFailure
dataLoader.requireProjectOwner = lambda *args: None
app = FastAPI()
app.include_router(dataLoader.router)
for route in dataLoader.router.routes:
    if isinstance(route, APIRoute):
        for dependency in route.dependant.dependencies:
            app.dependency_overrides[dependency.call] = lambda: type("User", (), {"userId": "owner"})()
client = TestClient(app)

sql = dict(projectId="p", user="u", password="pw", host="127.0.0.1", port=1, db="d", table="t")
requests = [("/loadMySql", sql), ("/loadPostgreSQL", sql)]
requests += [("/loadMongoDB", dict(projectId="p", connectionString=uri, db="d", collection="c")) for uri in uris]
for path, body in requests:
    response = client.post(path, json=body)
    print("ANSWER", response.status_code, response.text)
"""


@pytest.fixture(scope="module")
def output(tmp_path_factory):
    folder = tmp_path_factory.mktemp("loader_errors")
    quoted = f"access denied for {USER}:{FIRST}{SECOND} at {HOST} ({OPTION})"
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "LANG") if key in os.environ}
    env |= {
        "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8", "SECRET_KEY": "test-secret", "LOGTAIL_TOKEN": "",
        "SUPABASE_URL": "http://localhost:1", "SUPABASE_KEY": "test-key",
        "DATABASE_URL": "postgresql://test:test@127.0.0.1:1/blocked_unit_database",
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1", "REDIS_PASSWORD": "",
        "GEMINI_API_KEY": "test-key", "GROQ_API_KEY": "test-key",
    }
    child = subprocess.run([sys.executable, "-c", CHILD, json.dumps([URIS, quoted])], cwd=folder, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert child.returncode == 0, child.stderr[-3000:]
    return {"stdout": child.stdout, "stderr": child.stderr,
            "log file": (folder / "logs" / "runLogs.log").read_text(encoding="utf-8")}


def test_failedLoadsStillAnswerAndLog(output):
    answers = [line for line in output["stdout"].splitlines() if line.startswith("ANSWER")]
    assert len(answers) == len(URIS) + 2
    assert all(line.startswith("ANSWER 400") and "Unable to connect to" in line for line in answers), answers
    for kind in ("MySQL", "PostgreSQL", "MongoDB"):
        assert f"{kind} load failed (" in output["log file"]
    assert "MySQL load failed (OperationalError)" in output["log file"]


@pytest.mark.parametrize("sink", ["stdout", "stderr", "log file"])
def test_failedLoadsWriteNoConnectionDetails(output, sink):
    shown = [line for line in output[sink].splitlines()
             if any(value in line for value in (USER, FIRST, SECOND, HOST, OPTION))]
    assert shown == []
