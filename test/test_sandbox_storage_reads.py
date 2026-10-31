"""Generated code reads tables from real sandbox subprocesses.

Each launcher runs with the filtered environment its parent builds, so these
tests fail if SUPABASE_URL or SUPABASE_KEY ever leaves an allowlist.
"""

import io
import os
import subprocess
from unittest.mock import MagicMock

import polars as pl
import pytest

from nubrix.components.transformationExecutor import TransformationExecutor
from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage
from utils import codeExecutor

PROJECT = "11111111-2222-4333-8444-555555555555"
READ_EVERY_WAY = (
    f'a = fetch_data("{PROJECT}", "sales")\n'
    f'b = fetch_data_pl("{PROJECT}", "sales")\n'
    f'c = scan_data("{PROJECT}", "sales").collect()\n'
)


@pytest.fixture(autouse=True)
def _windowsSocketsNeedSystemRoot(monkeypatch):
    """Winsock can't load without SYSTEMROOT, which the Linux-only allowlists leave out."""
    if os.name != "nt":
        return
    realPopen = subprocess.Popen

    def popen(*args, env=None, **kwargs):
        return realPopen(*args, env={**env, "SYSTEMROOT": os.environ["SYSTEMROOT"]}, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    buffer = io.BytesIO()
    pl.DataFrame({"amount": [1, 2, 3]}).write_parquet(buffer)
    fake.put(PROJECT, "sales.parquet", buffer.getvalue())
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    monkeypatch.setenv("REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("REDIS_PORT", "1")
    yield fake
    fake.close()


def _assertReadWithTheServiceKey(storage):
    reads = [r for r in storage.requests if r["method"] == "GET"]
    assert reads
    assert all(r["path"] == f"/storage/v1/object/AnalyticsHub/{PROJECT}/sales.parquet" for r in reads)
    assert all(r["authorization"] == f"Bearer {SERVICE_KEY}" for r in reads)


def test_codeExecutionSandboxReadsTheBucket(storage, monkeypatch):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))
    manager = codeExecutor.REPLManager(timeoutSeconds=120)

    output = manager.run(READ_EVERY_WAY + "print(len(a), b.height, c.height)", projectId=PROJECT)

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)


def test_transformationSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    frame = executor._execute_code(PROJECT, READ_EVERY_WAY + "final_df = b")

    assert list(frame["amount"]) == [1, 2, 3]
    _assertReadWithTheServiceKey(storage)


def test_inspectionSandboxReadsTheBucket(storage):
    executor = TransformationExecutor(timeoutSeconds=120)

    output = executor.executeInspection(PROJECT, READ_EVERY_WAY + "print(len(a), b.height, c.height)")

    assert output.strip() == "3 3 3"
    _assertReadWithTheServiceKey(storage)
